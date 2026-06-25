"""Offline end-to-end integration of BOTH directions through the real durable-sync
spine — `make_activities` (fetch_source -> sync_records) driven via Temporal's
`ActivityEnvironment`, with every Spotify / ListenBrainz / MusicBrainz HTTP call
stubbed. No network, no credentials, no Temporal server.

Proves the thing a unit test can't: the connectors compose with the spine, both
routes key on the cross-service **ISRC**, and a second pass is **idempotent**
(updates/skips, never re-writes). This is the "test it" proof for the two-way
favourites sync; the live run is the same wiring with real tokens.
"""
from __future__ import annotations

import asyncio

from temporalio.testing import ActivityEnvironment

from durable_sync.activities import make_activities
from durable_sync.linkstore import InMemoryLinkStore

from durable_sync_contrib.spotify import api as sapi
from durable_sync_contrib.spotify.source import SpotifySource, SpotifyConfig
from durable_sync_contrib.spotify.destination import SpotifyDestination
from durable_sync_contrib.listenbrainz import api as lapi
from durable_sync_contrib.listenbrainz.source import ListenBrainzSource, ListenBrainzSourceConfig
from durable_sync_contrib.listenbrainz.config import ListenBrainzConfig
from durable_sync_contrib.listenbrainz.destination import ListenBrainzDestination


async def _token() -> str:
    return "tok"


async def _drive(source, destination) -> dict:
    """Run the spine over the source's single spec: fetch each page, upsert it,
    accumulate stats — exactly what SourceSyncWorkflow does, inline and offline."""
    fetch, sync = make_activities(source, destination)
    env = ActivityEnvironment()
    spec = source.specs()[0]
    totals = {"created": 0, "updated": 0, "skipped": 0}
    cursor = None
    while True:
        page = await env.run(fetch, spec, None, cursor)
        stats = await env.run(sync, page.records)
        for k in totals:
            totals[k] += stats[k]
        cursor = page.next_cursor
        if cursor is None:
            return totals


def _liked(isrc, tid, name):
    return {"added_at": "2024-03-01T12:00:00Z",
            "track": {"id": tid, "name": name, "external_ids": {"isrc": isrc},
                      "artists": [{"name": "Artist"}], "album": {"name": "Alb"},
                      "external_urls": {"spotify": f"https://open.spotify.com/track/{tid}"}}}


def test_spotify_to_listenbrainz_creates_then_idempotent(monkeypatch):
    monkeypatch.setenv("LISTENBRAINZ_TOKEN", "lb-token")  # makes the dest `configured`

    # Spotify SOURCE: a fixed liked library (one page).
    async def liked_page(client, token, *, offset=0, limit=50):
        return [_liked("ISRCA", "ta", "Alpha"), _liked("ISRCB", "tb", "Beta")], None
    monkeypatch.setattr(sapi, "list_saved_tracks_page", liked_page)

    # ListenBrainz DESTINATION: stub MusicBrainz resolve + feedback submit/read.
    mb = {"ISRCA": "mbid-a", "ISRCB": "mbid-b"}
    submitted: list[str] = []
    async def resolve(client, isrc, *, user_agent): return mb.get(isrc)
    async def submit(client, token, mbid, *, score=1): submitted.append(mbid)
    async def get_loved(client, token, user_name): return set()
    monkeypatch.setattr(lapi, "resolve_isrc_to_mbid", resolve)
    monkeypatch.setattr(lapi, "submit_feedback", submit)
    monkeypatch.setattr(lapi, "get_loved_mbids", get_loved)

    source = SpotifySource(SpotifyConfig(), token_provider=_token)
    link_store = InMemoryLinkStore()  # shared across passes -> durable idempotency
    dest = ListenBrainzDestination(
        ListenBrainzConfig(user_name="me", mb_pacing_seconds=0), link_store=link_store
    )

    first = asyncio.run(_drive(source, dest))
    assert first == {"created": 2, "updated": 0, "skipped": 0}
    assert sorted(submitted) == ["mbid-a", "mbid-b"]

    # Second pass: both ISRCs are already linked, so the spine takes the update
    # path; "loved" is binary so update is a no-op -> skipped, and NO new submits.
    second = asyncio.run(_drive(source, dest))
    assert second == {"created": 0, "updated": 0, "skipped": 2}
    assert sorted(submitted) == ["mbid-a", "mbid-b"]  # unchanged


def test_listenbrainz_to_spotify_creates_then_idempotent(monkeypatch):
    # ListenBrainz SOURCE: a fixed loved list + MusicBrainz MBID->ISRC.
    loved = [
        {"recording_mbid": "m-a", "created": 1709294400,
         "track_metadata": {"track_name": "Alpha", "artist_name": "Artist"}},
        {"recording_mbid": "m-b", "created": 1709294400,
         "track_metadata": {"track_name": "Beta", "artist_name": "Artist"}},
    ]
    mbid_isrc = {"m-a": "ISRCA", "m-b": "ISRCB"}
    async def list_loved(client, user_name, *, token="", offset=0, count=100):
        return loved, None
    async def rec_isrc(client, mbid, *, user_agent): return mbid_isrc.get(mbid, "")
    monkeypatch.setattr(lapi, "list_loved_page", list_loved)
    monkeypatch.setattr(lapi, "recording_isrc", rec_isrc)

    # Spotify DESTINATION: a MUTABLE fake library so idempotency is real across passes.
    library: dict[str, str] = {}                 # isrc -> track id (already saved)
    search = {"ISRCA": "ta", "ISRCB": "tb"}    # isrc -> resolvable track id
    saves: list[str] = []
    async def saved_page(client, token, *, offset=0, limit=50):
        items = [{"track": {"id": tid, "external_ids": {"isrc": isrc}}}
                 for isrc, tid in library.items()]
        return items, None
    async def search_fn(client, token, isrc): return search.get(isrc)
    async def save_fn(client, token, ids):
        for tid in ids:
            saves.append(tid)
            for isrc, t in search.items():
                if t == tid:
                    library[isrc] = tid
    monkeypatch.setattr(sapi, "list_saved_tracks_page", saved_page)
    monkeypatch.setattr(sapi, "search_track_id_by_isrc", search_fn)
    monkeypatch.setattr(sapi, "save_tracks", save_fn)

    source = ListenBrainzSource(ListenBrainzSourceConfig(user_name="me", mb_pacing_seconds=0))
    dest = SpotifyDestination(token_provider=_token)

    first = asyncio.run(_drive(source, dest))
    assert first == {"created": 2, "updated": 0, "skipped": 0}
    assert sorted(saves) == ["ta", "tb"]
    assert library == {"ISRCA": "ta", "ISRCB": "tb"}

    # Second pass: the library now contains both, so the spine updates (no-op) and
    # NO new saves happen — idempotent against the user's real Spotify library.
    second = asyncio.run(_drive(source, dest))
    assert second == {"created": 0, "updated": 0, "skipped": 2}
    assert sorted(saves) == ["ta", "tb"]  # unchanged
