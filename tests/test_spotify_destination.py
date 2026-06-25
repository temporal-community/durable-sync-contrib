"""Spotify destination tests: pure search-result parsing, the ISRC->id existing
map, and create/update upsert against a stubbed Spotify Web API (no network)."""
from __future__ import annotations

import asyncio
import datetime as dt

from durable_sync.core import Record

from durable_sync_contrib.spotify import api
from durable_sync_contrib.spotify.destination import SpotifyDestination

NOW = dt.datetime(2026, 6, 21, 12, 0, tzinfo=dt.timezone.utc)


async def _fake_token() -> str:
    return "fake-token"


def _dest():
    return SpotifyDestination(token_provider=_fake_token)


def _saved_item(isrc, tid):
    return {"track": {"id": tid, "external_ids": {"isrc": isrc}}}


# --- pure parse -------------------------------------------------------------

def test_first_track_id():
    assert api.first_track_id({"tracks": {"items": [{"id": "t1"}, {"id": "t2"}]}}) == "t1"
    assert api.first_track_id({"tracks": {"items": []}}) is None
    assert api.first_track_id({}) is None


# --- existing map: ISRC -> spotify track id, by paging the library ----------

def test_query_existing_ids_maps_isrc_to_id(monkeypatch):
    pages = {
        0: ([_saved_item("ISRCA", "ta"), _saved_item("ISRCB", "tb")], 2),
        2: ([_saved_item("ISRCC", "tc")], None),
    }

    async def fake_page(client, token, *, offset=0, limit=50):
        return pages[offset]

    monkeypatch.setattr(api, "list_saved_tracks_page", fake_page)

    async def run():
        async with _dest().connect() as s:
            assert await s.query_existing_ids() == {"ISRCA": "ta", "ISRCB": "tb", "ISRCC": "tc"}
    asyncio.run(run())


# --- create / update --------------------------------------------------------

def test_create_resolves_isrc_then_saves(monkeypatch):
    saved: list[str] = []

    async def fake_search(client, token, isrc):
        return {"ISRCA": "ta"}.get(isrc)

    async def fake_save(client, token, ids):
        saved.extend(ids)

    monkeypatch.setattr(api, "search_track_id_by_isrc", fake_search)
    monkeypatch.setattr(api, "save_tracks", fake_save)

    async def run():
        async with _dest().connect() as s:
            wrote = await s.create(Record(primary_key="ISRCA", properties={"Name": "A"}), NOW)
            assert wrote is True
            assert saved == ["ta"]
    asyncio.run(run())


def test_create_falls_back_to_name_when_isrc_misses(monkeypatch):
    # MB's ISRC differs from Spotify's for the same recording -> ISRC search misses,
    # but a name+artist search finds it (the Blinding Lights case).
    saved: list[str] = []
    calls: list[str] = []

    async def fake_isrc(client, token, isrc):
        calls.append("isrc")
        return None  # miss

    async def fake_name(client, token, title, artist=""):
        calls.append(f"name:{title}/{artist}")
        return "ta" if title == "Blinding Lights" else None

    async def fake_save(client, token, ids):
        saved.extend(ids)

    monkeypatch.setattr(api, "search_track_id_by_isrc", fake_isrc)
    monkeypatch.setattr(api, "search_track_id_by_name", fake_name)
    monkeypatch.setattr(api, "save_tracks", fake_save)

    async def run():
        async with _dest().connect() as s:
            rec = Record(primary_key="US23A8017264",
                         properties={"Name": "Blinding Lights", "Author": "The Weeknd"})
            assert await s.create(rec, NOW) is True
            assert saved == ["ta"]
            assert calls == ["isrc", "name:Blinding Lights/The Weeknd"]  # isrc first, then fallback
    asyncio.run(run())


def test_create_unresolvable_isrc_is_skipped_no_save(monkeypatch):
    saved: list[str] = []

    async def fake_search(client, token, isrc):
        return None  # Spotify has no track for this ISRC

    async def fake_name(client, token, title, artist=""):
        return None  # ...and the name fallback also finds nothing

    async def fake_save(client, token, ids):
        saved.extend(ids)

    monkeypatch.setattr(api, "search_track_id_by_isrc", fake_search)
    monkeypatch.setattr(api, "search_track_id_by_name", fake_name)
    monkeypatch.setattr(api, "save_tracks", fake_save)

    async def run():
        async with _dest().connect() as s:
            wrote = await s.create(Record(primary_key="ISRCX", properties={"Name": "?"}), NOW)
            assert wrote is False          # skipped, not created
            assert saved == []             # nothing saved
    asyncio.run(run())


def test_update_is_noop():
    async def run():
        async with _dest().connect() as s:
            assert await s.update("ta", Record(primary_key="ISRCA", properties={}), NOW) is False
    asyncio.run(run())
