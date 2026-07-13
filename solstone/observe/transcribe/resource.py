# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pure resource-aware STT backend selection helpers."""

from __future__ import annotations

import platform

STT_SURFACE = "surface"
STT_LOCAL_FLOOR_LINUX_BYTES = int(4 * 1024**3)
STT_LOCAL_FLOOR_DARWIN_BYTES = int(2 * 1024**3)
CONFIDENTIAL_STT_MAX_AUDIO_SECONDS = 300.0


def stt_local_floor_bytes() -> int | None:
    """Return the local transcription memory floor for this platform."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin" and machine == "arm64":
        return STT_LOCAL_FLOOR_DARWIN_BYTES
    if system == "linux" and machine in {"x86_64", "aarch64", "arm64"}:
        return STT_LOCAL_FLOOR_LINUX_BYTES
    return None


def local_stt_backend() -> str | None:
    """Return the local STT backend for this platform, or None if unsupported."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin" and machine == "arm64":
        return "parakeet"
    if system == "linux" and machine in {"x86_64", "aarch64", "arm64"}:
        return "parakeet"
    return None


def resolve_stt_backend_choice(
    explicit_backend: str | None,
    available_bytes: int | None,
    *,
    google_key_present: bool,
    floor_bytes: int | None,
    local_backend: str | None,
    confidential_lane_active: bool,
    confidential_audio_enabled: bool,
) -> str:
    """Resolve STT backend choice without reading config, env, or machine state."""
    if explicit_backend in {"parakeet", "parakeet-cpp"}:
        return explicit_backend
    if explicit_backend == "confidential":
        if confidential_lane_active and confidential_audio_enabled:
            return "confidential"
        return local_backend if local_backend is not None else STT_SURFACE
    if explicit_backend in {"gemini", "revai"}:
        return explicit_backend

    if confidential_lane_active and confidential_audio_enabled:
        return "confidential"
    if confidential_lane_active:
        return local_backend if local_backend is not None else STT_SURFACE

    local_fits = (
        local_backend is not None
        and floor_bytes is not None
        and available_bytes is not None
        and available_bytes >= floor_bytes
    )
    if local_fits:
        return local_backend
    if google_key_present:
        return "gemini"
    return STT_SURFACE


__all__ = [
    "CONFIDENTIAL_STT_MAX_AUDIO_SECONDS",
    "STT_LOCAL_FLOOR_DARWIN_BYTES",
    "STT_LOCAL_FLOOR_LINUX_BYTES",
    "STT_SURFACE",
    "local_stt_backend",
    "resolve_stt_backend_choice",
    "stt_local_floor_bytes",
]
