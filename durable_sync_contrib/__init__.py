"""durable-sync-contrib: off-domain / experimental connectors for durable-sync.

These ride the same `Source` / `Destination` contract as the in-repo (core)
connectors — they just live outside the core repo to keep its narrative focused
on the traditional martech/devrel stack. Discovered by name via durable-sync's
entry-point registry; an app wires them exactly like a core connector:

    from durable_sync.registry import load_source, load_destination
    source = load_source("spotify")()
    destination = load_destination("listenbrainz")(...)

See the core repo's CONTRACT.md for the curation model (core / contrib /
not-available) and the versioned import surface these connectors depend on.
"""
