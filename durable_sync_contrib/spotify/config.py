"""Spotify connector config. The auth workflow id lives here (not in durable-sync's
core `config.py`) now that the connector ships out-of-repo — a contrib connector
reads its OWN env var rather than depending on a core config constant. Default
matches the historical `durable-sync:spotify-auth` id so existing deployments are
unaffected."""
from __future__ import annotations

import os

# Workflow id of the OAuthTokenWorkflow that owns Spotify's rotating refresh token.
AUTH_WORKFLOW_ID = os.environ.get(
    "DURABLE_SYNC_SPOTIFY_AUTH_WORKFLOW_ID", "durable-sync:spotify-auth"
)
