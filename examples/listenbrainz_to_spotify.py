"""Example wiring: mirror your ListenBrainz **loved recordings** into your Spotify
**Liked Songs** — the inverse of spotify_to_listenbrainz.py.

Each loved recording is keyed on its ISRC (resolved from the MusicBrainz recording
MBID); the Spotify destination searches by ISRC and saves the match to your
library. Re-runs are idempotent (already-saved tracks are skipped), and a loved
recording with no Spotify match is skipped + logged.

Run this as a SECOND route, on its own task queue, alongside the forward worker:
two routes that both touch Spotify must NOT share a queue (each registers a
`sync_records` activity writing to a different destination).

────────────────────────────────────────────────────────────────────────────
SETUP (run once)
────────────────────────────────────────────────────────────────────────────
1. Temporal dev server (separate terminal):
       temporal server start-dev

2. Authorize Spotify with the user-library-modify scope (the bootstrap already
   requests it). The token workflow (durable-sync:spotify-auth) is shared with the
   forward route — whichever worker hosts that instance serves the token to both.

3. A ListenBrainz username (reading loved feedback is public; LISTENBRAINZ_TOKEN
   is optional here).

────────────────────────────────────────────────────────────────────────────
RUN (dedicated queue!)
────────────────────────────────────────────────────────────────────────────
    DURABLE_SYNC_TASK_QUEUE=listenbrainz-spotify PYTHONPATH=. \
        python examples/listenbrainz_to_spotify.py YOUR_LB_USERNAME

Then trigger a sync immediately instead of waiting for the interval:
    temporal workflow signal --workflow-id "durable-sync:loved" --name sync_now --input '[]'
    temporal workflow query  --workflow-id "durable-sync:loved" --type status
"""
from __future__ import annotations

import asyncio
import sys

from durable_sync.bootstrap import start_sources
from durable_sync.worker import run_worker

from durable_sync_contrib.listenbrainz.source import ListenBrainzSource, ListenBrainzSourceConfig
from durable_sync_contrib.spotify.destination import SpotifyDestination


def build(user_name: str) -> tuple[ListenBrainzSource, SpotifyDestination]:
    source = ListenBrainzSource(ListenBrainzSourceConfig(user_name=user_name))
    destination = SpotifyDestination()  # saves to the authed user's Liked Songs
    return source, destination


async def main(user_name: str) -> None:
    source, destination = build(user_name)
    await start_sources(source)            # one entity workflow per spec (idempotent)
    await run_worker(source, destination)  # host workflow + activities; runs forever


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python examples/listenbrainz_to_spotify.py YOUR_LB_USERNAME")
    asyncio.run(main(sys.argv[1]))
