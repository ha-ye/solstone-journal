# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pure resource-aware STT backend selection helpers."""

from __future__ import annotations

import platform

STT_SURFACE = "surface"
STT_LOCAL_FLOOR_LINUX_BYTES = int(4 * 1024**3)
STT_LOCAL_FLOOR_DARWIN_BYTES = int(2 * 1024**3)


def stt_local_floor_bytes() -> int | None:
    """Return the local transcription memory floor for this platform."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin" and machine == "arm64":
        return STT_LOCAL_FLOOR_DARWIN_BYTES
    if system == "linux" and machine in {"x86_64", "aarch64"}:
        return STT_LOCAL_FLOOR_LINUX_BYTES
    return None


def local_stt_backend() -> str | None:
    """Return the local STT backend for this platform, or None if unsupported."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin" and machine == "arm64":
        return "parakeet"
    if system == "linux" and machine == "x86_64":
        return "parakeet"
    if system == "linux" and machine == "aarch64":
        return "whisper"
    return None


def select_stt_backend(
    available_bytes: int | None,
    *,
    google_key_present: bool,
    floor_bytes: int | None,
    local_backend: str | None,
) -> str:
    """Resolve the unset/default STT backend without reading machine state."""
    local_fits = (
        floor_bytes is not None
        and available_bytes is not None
        and available_bytes >= floor_bytes
    )
    if local_fits:
        return local_backend
    if google_key_present:
        return "gemini"
    return STT_SURFACE


__all__ = [
    "STT_LOCAL_FLOOR_DARWIN_BYTES",
    "STT_LOCAL_FLOOR_LINUX_BYTES",
    "STT_SURFACE",
    "local_stt_backend",
    "select_stt_backend",
    "stt_local_floor_bytes",
]
