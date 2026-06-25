"""ISRC normalization, shared by the music connectors.

The ISRC (International Standard Recording Code) is the cross-service primary_key
every connector here keys on, so it must be byte-identical no matter which side
emitted it. Spotify occasionally returns ISRCs lowercase (or hyphenated), which
MusicBrainz rejects as invalid (400) and which would also fail to dedupe against
an uppercase copy of the same track. Canonical form is 12 chars, uppercase, no
separators.
"""
from __future__ import annotations


def normalize_isrc(isrc: str | None) -> str:
    """Uppercase, separator-free ISRC (or "" if falsy)."""
    return (isrc or "").strip().upper().replace("-", "").replace(" ", "")
