# durable-sync-contrib

Off-domain / experimental connectors for
[**durable-sync**](https://github.com/temporal-community/durable-sync) — kept out
of the core repo so its narrative stays focused on the traditional martech/devrel
stack (GitHub, Notion, Asana, Contentful, Luma, YouTube, Jira), while these ride
the exact same `Source` / `Destination` contract.

| Connector | Kind | Notes |
|---|---|---|
| **Spotify** | source | Liked Songs, keyed on ISRC (the cross-service identity). Workflow-owned OAuth (PKCE, no admin token). |
| **ListenBrainz** | destination | "Loved recordings." Resolves ISRC → MusicBrainz recording MBID (cached in a `LinkStore`), submits feedback. |

## Why a separate package?

Every durable-sync connector falls into exactly one bucket: **core** (in the main
repo), **contrib** (here), or **not available**. A connector is just a class
implementing the protocol plus an entry point, so nothing requires it to live in
core. Splitting the music connectors out keeps core focused and lets these
release on their own cadence — without changing a single line of any app's
wiring, because apps resolve connectors **by name** through durable-sync's
registry, not by import path. See core's
[`CONTRACT.md`](https://github.com/temporal-community/durable-sync/blob/main/CONTRACT.md).

## Install

```bash
pip install durable-sync-contrib        # pulls durable-sync (>=0.3) as a dependency

# local dev against a source checkout of durable-sync:
pip install -e ../durable-sync
pip install -e ".[dev]"
```

Confirm discovery (lists every connector grouped by providing package):

```bash
python -m durable_sync.registry
# durable-sync 0.3.0            github, notion, jira, ...
# durable-sync-contrib 0.1.0    spotify (source), listenbrainz (destination)
```

## Use

Resolve by name — identical to wiring a core connector:

```python
from durable_sync.registry import load_source, load_destination
from durable_sync.linkstore import SqliteLinkStore
from durable_sync.bootstrap import start_sources
from durable_sync.worker import run_worker
from durable_sync_contrib.listenbrainz.config import ListenBrainzConfig

source = load_source("spotify")()
destination = load_destination("listenbrainz")(
    ListenBrainzConfig(user_name="your-lb-handle"),
    link_store=SqliteLinkStore("listenbrainz_links.db", route="spotify-liked"),
)
await start_sources(source)
await run_worker(source, destination)
```

A runnable end-to-end wiring is in [`examples/spotify_to_listenbrainz.py`](examples/spotify_to_listenbrainz.py)
(includes the one-time Spotify OAuth bootstrap + ListenBrainz token setup).

## Test

```bash
python -m pytest          # unit tests, no network
```

## Writing your own connector

You don't need a PR to this repo — publish `durable-sync-<yourthing>` with its
own entry points and it Just Works. See core's `CONTRIBUTING.md` ("Register a
connector for discovery") and `CONTRACT.md` (the versioned import surface you may
depend on).

## License

MIT — see [LICENSE](LICENSE).
