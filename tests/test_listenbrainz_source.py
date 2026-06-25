"""ListenBrainz source tests: pure ISRC extraction + loved-feedback -> Record
normalization, and the fetch_page offset/drop contract, with MusicBrainz +
ListenBrainz stubbed (no network)."""
from __future__ import annotations

import asyncio

from durable_sync_contrib.listenbrainz import api
from durable_sync_contrib.listenbrainz.source import ListenBrainzSource, ListenBrainzSourceConfig


def _fb(mbid="mbid-1", **meta):
    base = {"track_name": "Mr. Blue Sky", "artist_name": "ELO", "release_name": "Out of the Blue"}
    base.update(meta)
    return {"recording_mbid": mbid, "created": 1709294400, "track_metadata": base}


def _src(**kw):
    return ListenBrainzSource(ListenBrainzSourceConfig(user_name="me", mb_pacing_seconds=0, **kw))


# --- pure parse -------------------------------------------------------------

def test_first_isrc_picks_first_or_empty():
    assert api.first_isrc({"isrcs": ["GBARL7800123", "USXXX"]}) == "GBARL7800123"
    assert api.first_isrc({"isrcs": []}) == ""
    assert api.first_isrc({}) == ""


# --- normalization (MBID->ISRC stubbed) -------------------------------------

def test_to_record_keys_on_resolved_isrc(monkeypatch):
    async def fake_isrc(client, mbid, *, user_agent):
        return "GBARL7800123"
    monkeypatch.setattr(api, "recording_isrc", fake_isrc)

    async def run():
        rec = await _src()._to_record(None, _fb())
        assert rec is not None
        assert rec.primary_key == "GBARL7800123"      # ISRC, not the MBID
        p = rec.properties
        assert p["Name"] == "Mr. Blue Sky"
        assert p["Type"] == "Track" and p["Source"] == "ListenBrainz"
        assert p["Source ID"] == "GBARL7800123"
        assert p["Author"] == "ELO" and p["Authors"] == ["ELO"]
        assert p["Album"] == "Out of the Blue"
        assert p["Recording MBID"] == "mbid-1"
        assert p["Date"] == "2024-03-01T12:00:00+00:00"  # epoch -> ISO UTC
    asyncio.run(run())


def test_no_mbid_or_no_isrc_is_dropped(monkeypatch):
    async def no_isrc(client, mbid, *, user_agent):
        return ""
    monkeypatch.setattr(api, "recording_isrc", no_isrc)

    async def run():
        assert await _src()._to_record(None, {"track_metadata": {}}) is None      # no mbid
        assert await _src()._to_record(None, _fb()) is None                        # mbid, no isrc
    asyncio.run(run())


def test_fetch_page_threads_offset_cursor_and_drops(monkeypatch):
    # Page 1: two loved items (one resolves to no ISRC -> dropped), more remain.
    # Page 2: one item, last page.
    pages = {
        0: ([_fb("m1"), _fb("m-noisrc")], 2),
        2: ([_fb("m3")], None),
    }
    isrcs = {"m1": "ISRC-1", "m-noisrc": "", "m3": "ISRC-3"}

    async def fake_loved(client, user_name, *, token="", offset=0, count=100):
        return pages[offset]

    async def fake_isrc(client, mbid, *, user_agent):
        return isrcs[mbid]

    monkeypatch.setattr(api, "list_loved_page", fake_loved)
    monkeypatch.setattr(api, "recording_isrc", fake_isrc)
    src = _src()

    async def run():
        spec = src.specs()[0]
        page1, cur1 = await src.fetch_page(spec, None, None)
        assert [r.primary_key for r in page1] == ["ISRC-1"]    # m-noisrc dropped
        assert cur1 is not None
        page2, cur2 = await src.fetch_page(spec, None, cur1)    # cursor threaded
        assert [r.primary_key for r in page2] == ["ISRC-3"]
        assert cur2 is None
        allrecs = await src.fetch(spec)
        assert [r.primary_key for r in allrecs] == ["ISRC-1", "ISRC-3"]
    asyncio.run(run())
