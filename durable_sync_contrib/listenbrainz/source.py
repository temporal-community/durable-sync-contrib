"""ListenBrainzSource — a user's loved recordings -> neutral Records.

The read half of ListenBrainz (the inverse of ListenBrainzDestination). Loved
feedback is keyed on a MusicBrainz recording MBID; to emit Records on the same
cross-service identity every other music connector uses, this resolves
**MBID -> ISRC** via MusicBrainz (the inverse of the destination's ISRC -> MBID)
and keys each Record on that ISRC. Recordings with no ISRC are dropped + counted
in a warning — never emit a record whose primary_key can't dedupe.

Reading feedback is public, so a token is optional (sent only if `token_env` is
set). MusicBrainz rate-limits anonymous callers (~1 req/sec), so MBID resolution
is paced; a descriptive User-Agent is required.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from dataclasses import dataclass

import httpx
from temporalio import activity

from durable_sync.core import Record, SourceSpec
from durable_sync.connectors import content

from durable_sync_contrib.listenbrainz import api

log = logging.getLogger("durable_sync_contrib.listenbrainz")

_MAX_TEXT = 2000


@dataclass
class ListenBrainzSourceConfig:
    """Everything the source needs. `user_name` is the ListenBrainz handle whose
    loved recordings to read; `token_env` is optional (reading feedback is public)."""
    user_name: str
    token_env: str = "LISTENBRAINZ_TOKEN"
    user_agent: str = "durable-sync-contrib/0.1 ( https://github.com/temporal-community/durable-sync-contrib )"
    mb_pacing_seconds: float = 1.1
    interval_minutes: int = 120
    title_property: str = "Name"
    item_type: str = "Track"


def _heartbeat(detail: str) -> None:
    if activity.in_activity():
        activity.heartbeat(detail)


def _iso(created: object) -> str | None:
    """Best-effort ISO-8601 (UTC) from ListenBrainz's epoch-seconds `created`."""
    if isinstance(created, (int, float)):
        return dt.datetime.fromtimestamp(created, tz=dt.timezone.utc).isoformat()
    return None


class ListenBrainzSource:
    name = "listenbrainz"

    def __init__(self, config: ListenBrainzSourceConfig):
        self._config = config

    def specs(self) -> list[SourceSpec]:
        # One user's loved recordings = a single unit of work / entity workflow.
        return [SourceSpec(key="loved", interval_minutes=self._config.interval_minutes)]

    async def fetch_page(
        self, spec: SourceSpec, only_items: list[str] | None, cursor: str | None
    ) -> tuple[list[Record], str | None]:
        """ONE page of loved recordings + next cursor (None on the last page). The
        cursor is ListenBrainz's integer feedback offset, packed as the spine's
        opaque string. `only_items` (targeted by ISRC) is unsupported — feedback
        has no ISRC filter — so it's ignored and we page normally."""
        cfg = self._config
        token = os.environ.get(cfg.token_env, "")
        offset = 0 if cursor is None else content.unpack_cursor(cursor)["offset"]

        dropped = 0
        out: list[Record] = []
        async with httpx.AsyncClient(timeout=30) as client:
            items, next_offset = await api.list_loved_page(
                client, cfg.user_name, token=token, offset=offset
            )
            for fb in items:
                record = await self._to_record(client, fb)
                if record is None:
                    dropped += 1
                    continue
                out.append(record)
                _heartbeat(record.primary_key)

        if dropped:
            log.warning("Dropped %d loved recording(s) with no ISRC (offset=%d)", dropped, offset)
        next_cursor = content.pack_cursor(offset=next_offset) if next_offset is not None else None
        log.info("Fetched %d loved recordings for %s (offset=%d)", len(out), spec.key, offset)
        return out, next_cursor

    async def fetch(self, spec: SourceSpec, only_items: list[str] | None = None) -> list[Record]:
        """Whole loved list as one list — drains fetch_page (standalone/non-Temporal)."""
        records: list[Record] = []
        cursor: str | None = None
        while True:
            page, cursor = await self.fetch_page(spec, only_items, cursor)
            records.extend(page)
            if cursor is None:
                return records

    async def _to_record(self, client: httpx.AsyncClient, fb: dict) -> Record | None:
        """Map one loved-feedback item to a neutral Record keyed on ISRC, or None
        when the recording has no MBID or no resolvable ISRC. NOT pure — it resolves
        MBID -> ISRC via MusicBrainz (paced); the field mapping below is pure."""
        cfg = self._config
        mbid = fb.get("recording_mbid")
        if not mbid:
            return None
        if cfg.mb_pacing_seconds > 0:                 # respect MusicBrainz's ~1 req/sec
            await asyncio.sleep(cfg.mb_pacing_seconds)
        isrc = await api.recording_isrc(client, mbid, user_agent=cfg.user_agent)
        if not isrc:
            return None

        meta = fb.get("track_metadata") or {}
        artist = meta.get("artist_name") or ""
        return content.content_record(
            primary_key=isrc,
            title_property=cfg.title_property,
            title=meta.get("track_name") or "(untitled recording)",
            item_type=cfg.item_type,
            source="ListenBrainz",
            url=f"https://listenbrainz.org/player/?recording_mbids={mbid}",
            date=_iso(fb.get("created")),
            status="Published",
            author=artist,
            authors=[artist] if artist else [],
            extra={
                "Album": (meta.get("release_name") or "")[:_MAX_TEXT],
                "Recording MBID": mbid,
            },
        )
