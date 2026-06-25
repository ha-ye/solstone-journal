# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Media format registry - single source of truth for extensions, MIME types, and kind."""

from pathlib import Path

FORMATS = [
    (".flac", "audio/flac", "audio"),
    (".opus", "audio/opus", "audio"),
    (".ogg", "audio/ogg", "audio"),
    (".m4a", "audio/mp4", "audio"),
    (".mp3", "audio/mpeg", "audio"),
    (".wav", "audio/wav", "audio"),
    (".webm", "video/webm", "video"),
    (".mp4", "video/mp4", "video"),
    (".mov", "video/quicktime", "video"),
]

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    ext for ext, _, kind in FORMATS if kind == "audio"
)
VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    ext for ext, _, kind in FORMATS if kind == "video"
)
MEDIA_EXTENSIONS: frozenset[str] = frozenset(ext for ext, _, _ in FORMATS)
MIME_TYPES: dict[str, str] = {ext: mime for ext, mime, _ in FORMATS}
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".heic", ".heif", ".gif", ".webp", ".tiff"}
)
PDF_EXTENSIONS: frozenset[str] = frozenset({".pdf"})


def canonical_source(
    *, filename: str | None = None, content_type: str | None = None
) -> str:
    """Infer the canonical import source category from content signals.

    Returns one of "audio", "image", "document", "text". This is a protocol
    metadata category describing WHAT the content is; it does not drive importer
    routing.
    """
    suffix = Path(filename).suffix.lower() if filename else ""
    if suffix in AUDIO_EXTENSIONS or suffix in VIDEO_EXTENSIONS:
        return "audio"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in PDF_EXTENSIONS:
        return "document"

    media_type = content_type.lower() if content_type else ""
    if media_type.startswith(("audio/", "video/")):
        return "audio"
    if media_type.startswith("image/"):
        return "image"
    if media_type == "application/pdf":
        return "document"

    return "text"


def canonical_source_signal(
    *, filename: str | None = None, content_type: str | None = None
) -> str:
    """Return the signal that determined the canonical source."""
    suffix = Path(filename).suffix.lower() if filename else ""
    if (
        suffix in AUDIO_EXTENSIONS
        or suffix in VIDEO_EXTENSIONS
        or suffix in IMAGE_EXTENSIONS
        or suffix in PDF_EXTENSIONS
    ):
        return "extension"

    media_type = content_type.lower() if content_type else ""
    if (
        media_type.startswith(("audio/", "video/"))
        or media_type.startswith("image/")
        or media_type == "application/pdf"
    ):
        return "content_type"

    return "default"
