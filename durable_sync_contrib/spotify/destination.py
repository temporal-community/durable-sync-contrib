"""SpotifyDestination — save records as tracks in the user's "Liked Songs".

The write half of Spotify (the inverse of SpotifySource): given Records keyed on
ISRC (the cross-service identity — see SpotifySource / ListenBrainzSource), save
the matching tracks to the authenticated user's library.

Two ISRC↔id resolutions, mirror images of the source:
- `query_existing_ids` pages the user's saved tracks and maps **ISRC → Spotify
  track id** for everything already saved (reusing the source's `extract_isrc`),
  so the spine only `create`s ISRCs not already in the library — idempotent with
  no external store needed (the library itself is the dedupe surface).
- `create` resolves **ISRC → track id** via search, then PUT /me/tracks. "Saved"
  is binary, so `update` is a no-op (and re-saving is idempotent on Spotify too).

Auth is the same workflow-owned OAuth token the source uses, but the destination
needs the `user-library-modify` scope (oauth.py requests it alongside read).
"""
from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

import httpx

from durable_sync.core import Record, auth_error_in_chain

from durable_sync_contrib.spotify import api
from durable_sync_contrib.spotify.token import current_access_token

log = logging.getLogger("durable_sync_contrib.spotify")

# async () -> access token. Injectable so the destination is testable without Temporal.
TokenProvider = Callable[[], Awaitable[str]]


class SpotifyDestination:
    name = "spotify"

    def __init__(self, *, token_provider: TokenProvider | None = None):
        self._token_provider = token_provider or current_access_token
        self.create_only_properties: set[str] = set()  # saving carries no properties

    @property
    def configured(self) -> bool:
        # The target is always "the authenticated user's own library" — there is no
        # per-deployment target to configure. A missing/invalid token surfaces as an
        # auth error at write time (pausing the workflow), not as misconfiguration.
        return True

    @property
    def config_hint(self) -> str:
        return ("authorize Spotify with the user-library-modify scope: "
                "python -m durable_sync_contrib.spotify.bootstrap (then .start)")

    @asynccontextmanager
    async def connect(self) -> AsyncIterator["_SpotifySession"]:
        token = await self._token_provider()
        async with httpx.AsyncClient(timeout=30) as client:
            yield _SpotifySession(client, token)

    # Same self-contained-auth pattern as SpotifySource: a worker running this
    # destination hosts its own OAuthTokenWorkflow + refresh activity, so it can
    # serve the access-token query on its own task queue. (The token workflow is a
    # single shared instance per Spotify account — durable-sync:spotify-auth — so
    # when source and destination run on separate queues, only the worker on the
    # queue where that instance lives serves it; registering the class here keeps
    # the destination self-contained when it's the one hosting it.)
    def aux_workflows(self) -> list:
        from durable_sync.auth.oauth.workflow import OAuthTokenWorkflow
        return [OAuthTokenWorkflow]

    def aux_activities(self) -> list:
        from durable_sync.auth.oauth.refresh import refresh_oauth_token
        return [refresh_oauth_token]

    @staticmethod
    def is_auth_error(err: BaseException) -> bool:
        """A lapsed/revoked token or a missing scope (401/403). Shared matcher."""
        return auth_error_in_chain(err)


class _SpotifySession:
    def __init__(self, client: httpx.AsyncClient, token: str):
        self._client = client
        self._token = token

    async def query_existing_ids(self) -> dict[str, str]:
        """ISRC -> Spotify track id for every already-saved track, by paging the
        user's library. The library is its own dedupe surface, so no LinkStore."""
        out: dict[str, str] = {}
        offset = 0
        while True:
            items, next_offset = await api.list_saved_tracks_page(
                self._client, self._token, offset=offset
            )
            for item in items:
                track = item.get("track") or {}
                isrc = api.extract_isrc(track)
                tid = track.get("id")
                if isrc and tid:
                    out[isrc] = tid
            if next_offset is None:
                return out
            offset = next_offset

    async def create(self, record: Record, synced_at: dt.datetime) -> bool:
        isrc = record.primary_key
        track_id = await api.search_track_id_by_isrc(self._client, self._token, isrc)
        if not track_id:
            log.warning("No Spotify track for ISRC %s (%r) — skipping",
                        isrc, record.properties.get("Name", ""))
            return False  # SKIPPED — unresolvable on Spotify; tallied as skipped.
        await api.save_tracks(self._client, self._token, [track_id])
        return True

    async def update(self, existing_id: str, record: Record, synced_at: dt.datetime) -> bool:
        # "Saved" is binary; nothing to refresh once the track is in the library.
        return False
