# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Audio file resolution helpers for the speakers app."""

from __future__ import annotations

from pathlib import Path

from solstone.think.media import FORMATS
from solstone.think.utils import segment_path

__all__ = ["audio_serve_url", "resolve_audio_file", "resolve_audio_url"]

_ORDERED_AUDIO_EXTENSIONS: tuple[str, ...] = tuple(
    ext for ext, _mime, kind in FORMATS if kind == "audio"
)


def resolve_audio_file(segment_dir: Path, source: str) -> Path | None:
    """Return the registered audio file for source in segment_dir, if present.

    Only checks {source}.<registered-audio-extension> files directly inside
    segment_dir, in solstone.think.media.FORMATS order.
    """
    for suffix in _ORDERED_AUDIO_EXTENSIONS:
        path = segment_dir / f"{source}{suffix}"
        if path.is_file():
            return path
    return None


def audio_serve_url(day: str, stream: str, segment_key: str, filename: str) -> str:
    """Return the speakers audio-serving URL for a segment audio filename."""
    return f"/app/speakers/api/serve_audio/{day}/{stream}/{segment_key}/{filename}"


def resolve_audio_url(
    day: str,
    stream: str,
    segment_key: str,
    source: str,
) -> str | None:
    """Resolve the speakers audio-serving URL for a segment source, if present."""
    segment_dir = segment_path(day, segment_key, stream, create=False)
    audio_path = resolve_audio_file(segment_dir, source)
    if audio_path is None:
        return None
    return audio_serve_url(day, stream, segment_key, audio_path.name)
