"""Spotify binding of the generic token accessor (durable_sync.auth.oauth.token).

The default token_provider for SpotifySource: query the OAuthTokenWorkflow running
under config.AUTH_WORKFLOW_ID for a fresh access token. The query result is used
inside the fetch activity and never returned from it, so the token stays out of
Temporal event history.
"""
from __future__ import annotations

from durable_sync.auth.oauth.token import current_access_token as _current

from durable_sync_contrib.spotify import config


async def current_access_token() -> str:
    return await _current(config.AUTH_WORKFLOW_ID)
