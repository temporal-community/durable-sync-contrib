"""MusicBrainz resolution robustness (durable_sync_contrib.listenbrainz.api),
exercised against a stubbed httpx transport — no network.

The load-bearing behavior: a single unresolvable/malformed ISRC must SKIP that
record (return None), never raise and fail the whole sync batch."""
from __future__ import annotations

import asyncio

import httpx

from durable_sync_contrib.listenbrainz import api


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_resolve_isrc_400_and_404_skip_not_raise():
    def handler(request):
        url = str(request.url)
        if "isrc/BADISRC" in url:
            return httpx.Response(400, json={"error": "Invalid isrc."})
        if "isrc/UNKNOWNISRC1" in url:
            return httpx.Response(404, json={"error": "Not Found"})
        return httpx.Response(200, json={"recordings": [{"id": "mbid-x"}]})

    async def run():
        async with _client(handler) as c:
            assert await api.resolve_isrc_to_mbid(c, "BADISRC", user_agent="ua") is None      # 400 -> skip
            assert await api.resolve_isrc_to_mbid(c, "UNKNOWNISRC1", user_agent="ua") is None  # 404 -> skip
            assert await api.resolve_isrc_to_mbid(c, "GOODISRC00001", user_agent="ua") == "mbid-x"

    asyncio.run(run())


def test_resolve_normalizes_isrc_before_request():
    seen: dict[str, str] = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"recordings": [{"id": "m"}]})

    async def run():
        async with _client(handler) as c:
            await api.resolve_isrc_to_mbid(c, "usl4q1981736", user_agent="ua")
    asyncio.run(run())
    assert "USL4Q1981736" in seen["url"]  # lowercase normalized before the request
