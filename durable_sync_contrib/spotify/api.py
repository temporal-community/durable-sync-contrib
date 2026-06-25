"""Spotify Web API helpers — pure async HTTP + small pure transforms. No Temporal,
no config globals: every call takes its bearer `token`. All HTTP goes through
`http.request_with_retry` so 429/Retry-After backoff is inherited.

Docs: https://developer.spotify.com/documentation/web-api/reference/get-users-saved-tracks
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from durable_sync.core import DestinationHTTPError
from durable_sync.http import request_with_retry

BASE_URL = "https://api.spotify.com/v1"
SAVED_TRACKS_PATH = "/me/tracks"
PAGE_LIMIT = 50  # Spotify's max page size for this endpoint.
log = logging.getLogger("durable_sync_contrib.spotify")


def build_headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token or ''}", "Accept": "application/json"}


async def list_saved_tracks_page(
    client: httpx.AsyncClient, token: str, *, offset: int = 0, limit: int = PAGE_LIMIT
) -> tuple[list[dict[str, Any]], int | None]:
    """ONE page of the user's Liked Songs. Returns (items, next_offset) where each
    item is Spotify's saved-track wrapper ({added_at, track}) and next_offset is the
    offset to request next, or None when the library is exhausted. Offset paging is
    used (not the `next` URL) so the cursor the spine threads is a simple integer.

    Raises DestinationHTTPError on a non-2xx so the spine's auth/transient
    classification sees the status code (a 401 here means the access token lapsed)."""
    r = await request_with_retry(
        client, "GET", f"{BASE_URL}{SAVED_TRACKS_PATH}",
        headers=build_headers(token), params={"limit": limit, "offset": offset},
    )
    if r.status_code >= 400:
        raise DestinationHTTPError(
            r.status_code, f"Spotify GET {SAVED_TRACKS_PATH} -> {r.status_code}: {r.text[:600]}"
        )
    data = r.json()
    items = data.get("items", [])
    # `next` is null on the last page; otherwise advance by the page we just read.
    next_offset = offset + len(items) if data.get("next") else None
    return items, next_offset


def extract_isrc(track: dict[str, Any]) -> str:
    """The track's ISRC (International Standard Recording Code) — the cross-service
    identity used as the Record primary_key — or "" when absent (local files and
    some releases have none; the source drops those)."""
    return (track.get("external_ids") or {}).get("isrc", "") or ""


def artist_names(track: dict[str, Any]) -> list[str]:
    return [a.get("name", "") for a in track.get("artists", []) if a.get("name")]


# --- write path: save tracks to "Liked Songs" (used by SpotifyDestination) ----

SEARCH_PATH = "/search"
SAVE_LIMIT = 50  # Spotify's max ids per PUT /me/tracks call.


def first_track_id(payload: dict[str, Any]) -> str | None:
    """Pure: the first track id from a /search response, or None if no match."""
    items = ((payload.get("tracks") or {}).get("items")) or []
    for item in items:
        tid = item.get("id")
        if tid:
            return tid
    return None


async def search_track_id_by_isrc(
    client: httpx.AsyncClient, token: str, isrc: str
) -> str | None:
    """Resolve an ISRC to a Spotify track id via search, or None if unknown. The
    write side needs this because a Record carries the cross-service ISRC, but
    Spotify saves by its own track id (the inverse of `extract_isrc` on read)."""
    if not isrc:
        return None
    r = await request_with_retry(
        client, "GET", f"{BASE_URL}{SEARCH_PATH}",
        headers=build_headers(token),
        params={"q": f"isrc:{isrc}", "type": "track", "limit": 1},
    )
    if r.status_code >= 400:
        raise DestinationHTTPError(
            r.status_code, f"Spotify GET {SEARCH_PATH} (isrc:{isrc}) -> {r.status_code}: {r.text[:600]}"
        )
    return first_track_id(r.json())


async def save_tracks(client: httpx.AsyncClient, token: str, ids: list[str]) -> None:
    """Save track ids to the user's Liked Songs (`PUT /me/tracks`). Idempotent on
    Spotify's side (re-saving an already-saved track is a no-op). Needs the
    `user-library-modify` scope."""
    if not ids:
        return
    r = await request_with_retry(
        client, "PUT", f"{BASE_URL}{SAVED_TRACKS_PATH}",
        headers={**build_headers(token), "Content-Type": "application/json"},
        json={"ids": ids[:SAVE_LIMIT]},
    )
    if r.status_code >= 400:
        raise DestinationHTTPError(
            r.status_code, f"Spotify PUT {SAVED_TRACKS_PATH} -> {r.status_code}: {r.text[:600]}"
        )
