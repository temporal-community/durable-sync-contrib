"""ListenBrainz + MusicBrainz API helpers — pure async HTTP + pure parse helpers.
No Temporal, no config globals. All HTTP goes through `http.request_with_retry`
(honors Retry-After / 429 backoff).

Two services:
- **MusicBrainz** maps `ISRC → recording MBID` (the deterministic resolution that
  lets us key on ISRC but address ListenBrainz by MBID). It requires a descriptive
  User-Agent and rate-limits anonymous callers to ~1 req/sec — so the destination
  caches results in a LinkStore and paces these calls.
  Docs: https://musicbrainz.org/doc/MusicBrainz_API
- **ListenBrainz** stores per-user "feedback" (loved/neutral/hated recordings).
  Docs: https://listenbrainz.readthedocs.io/en/latest/users/api/core.html
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from durable_sync.core import DestinationHTTPError
from durable_sync.http import request_with_retry

from durable_sync_contrib.isrc import normalize_isrc

LB_BASE_URL = "https://api.listenbrainz.org"
MB_BASE_URL = "https://musicbrainz.org/ws/2"
LOVED = 1  # ListenBrainz feedback score for "loved"
log = logging.getLogger("durable_sync_contrib.listenbrainz")


# --- MusicBrainz: ISRC -> recording MBID ------------------------------------

def first_recording_mbid(payload: dict[str, Any]) -> str | None:
    """Pure: pull the first recording MBID from a MusicBrainz `/isrc/{isrc}`
    response, or None when the ISRC matched nothing. Several recordings can share
    an ISRC (we take the first; MusicBrainz orders by relevance), and several
    ISRCs can map to one recording (so this collapses re-releases — the desired
    dedupe)."""
    recordings = payload.get("recordings") or []
    for rec in recordings:
        mbid = rec.get("id")
        if mbid:
            return mbid
    return None


async def resolve_isrc_to_mbid(
    client: httpx.AsyncClient, isrc: str, *, user_agent: str
) -> str | None:
    """Resolve an ISRC to a recording MBID via MusicBrainz, or None if unknown /
    unusable. `user_agent` is REQUIRED by MusicBrainz (a generic/blank UA gets
    403-blocked)."""
    isrc = normalize_isrc(isrc)
    if not isrc:
        return None
    r = await request_with_retry(
        client, "GET", f"{MB_BASE_URL}/isrc/{isrc}",
        headers={"User-Agent": user_agent, "Accept": "application/json"},
        params={"fmt": "json"},
    )
    # 404 = ISRC unknown to MusicBrainz; 400 = MusicBrainz rejected it as malformed.
    # Either way THIS record can't resolve — skip it (return None) so one bad ISRC
    # never fails the whole batch. (Normalization above fixes the common cause —
    # Spotify's lowercase ISRCs — but a genuinely malformed one still 400s.)
    if r.status_code in (400, 404):
        if r.status_code == 400:
            log.warning("MusicBrainz rejected ISRC %r as invalid (400) — skipping", isrc)
        return None
    if r.status_code >= 400:
        raise DestinationHTTPError(
            r.status_code, f"MusicBrainz GET /isrc/{isrc} -> {r.status_code}: {r.text[:600]}"
        )
    return first_recording_mbid(r.json())


# --- ListenBrainz: loved-recording feedback ---------------------------------

def feedback_payload(recording_mbid: str, *, score: int = LOVED) -> dict[str, Any]:
    """Pure: the body for POST /1/feedback/recording-feedback."""
    return {"recording_mbid": recording_mbid, "score": score}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Token {token}", "Accept": "application/json"}


async def submit_feedback(
    client: httpx.AsyncClient, token: str, recording_mbid: str, *, score: int = LOVED
) -> None:
    """Set this user's feedback for a recording. Idempotent on ListenBrainz's side
    (re-loving an already-loved recording just re-asserts score=1)."""
    r = await request_with_retry(
        client, "POST", f"{LB_BASE_URL}/1/feedback/recording-feedback",
        headers=_auth(token), json=feedback_payload(recording_mbid, score=score),
    )
    if r.status_code >= 400:
        raise DestinationHTTPError(
            r.status_code, f"ListenBrainz POST feedback -> {r.status_code}: {r.text[:600]}"
        )


async def get_loved_mbids(client: httpx.AsyncClient, token: str, user_name: str) -> set[str]:
    """All recording MBIDs this user has loved (score=1), paged. Used as a
    secondary safety net so we never re-submit feedback the user already has."""
    out: set[str] = set()
    offset = 0
    while True:
        r = await request_with_retry(
            client, "GET", f"{LB_BASE_URL}/1/feedback/user/{user_name}/get-feedback",
            headers=_auth(token), params={"score": LOVED, "count": 100, "offset": offset},
        )
        if r.status_code >= 400:
            raise DestinationHTTPError(
                r.status_code, f"ListenBrainz GET feedback -> {r.status_code}: {r.text[:600]}"
            )
        feedback = r.json().get("feedback") or []
        for fb in feedback:
            mbid = fb.get("recording_mbid")
            if mbid:
                out.add(mbid)
        if len(feedback) < 100:
            return out
        offset += len(feedback)


# --- read path: a page of loved feedback (used by ListenBrainzSource) --------

LOVED_PAGE = 100  # ListenBrainz get-feedback max count per page.


async def list_loved_page(
    client: httpx.AsyncClient, user_name: str, *, token: str = "",
    offset: int = 0, count: int = LOVED_PAGE,
) -> tuple[list[dict[str, Any]], int | None]:
    """ONE page of this user's loved feedback. Returns (items, next_offset) where
    each item is ListenBrainz's feedback object ({recording_mbid, track_metadata,
    …}) and next_offset is None on the last page. Reading feedback is public, so
    `token` is optional (sent only when provided)."""
    r = await request_with_retry(
        client, "GET", f"{LB_BASE_URL}/1/feedback/user/{user_name}/get-feedback",
        headers=_auth(token) if token else {"Accept": "application/json"},
        params={"score": LOVED, "count": count, "offset": offset},
    )
    if r.status_code >= 400:
        raise DestinationHTTPError(
            r.status_code, f"ListenBrainz GET feedback -> {r.status_code}: {r.text[:600]}"
        )
    feedback = r.json().get("feedback") or []
    next_offset = offset + len(feedback) if len(feedback) == count else None
    return feedback, next_offset


# --- MusicBrainz: recording MBID -> ISRC (the inverse of resolve_isrc_to_mbid) --

def first_isrc(payload: dict[str, Any]) -> str:
    """Pure: the first ISRC from a MusicBrainz `/recording/{mbid}?inc=isrcs`
    response, or "" when the recording has none (caller drops it — an ISRC-less
    recording has no stable cross-service key)."""
    for isrc in payload.get("isrcs") or []:
        if isrc:
            return isrc
    return ""


async def recording_isrc(
    client: httpx.AsyncClient, mbid: str, *, user_agent: str
) -> str:
    """Resolve a recording MBID to an ISRC via MusicBrainz, or "" if it has none.
    `user_agent` is REQUIRED by MusicBrainz (a generic/blank UA gets 403-blocked)."""
    if not mbid:
        return ""
    r = await request_with_retry(
        client, "GET", f"{MB_BASE_URL}/recording/{mbid}",
        headers={"User-Agent": user_agent, "Accept": "application/json"},
        params={"inc": "isrcs", "fmt": "json"},
    )
    if r.status_code == 404:
        return ""
    if r.status_code >= 400:
        raise DestinationHTTPError(
            r.status_code, f"MusicBrainz GET /recording/{mbid} -> {r.status_code}: {r.text[:600]}"
        )
    return first_isrc(r.json())
