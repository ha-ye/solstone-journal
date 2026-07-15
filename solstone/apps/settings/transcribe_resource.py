# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Settings payload assembly for resource-aware transcription defaults."""

from __future__ import annotations

from solstone.apps.settings.install_copy import (
    STT_DETECTED_MEMORY_TEMPLATE,
    STT_DETECTED_MEMORY_UNKNOWN,
    STT_LOCAL_REQUIREMENTS_TEMPLATE,
    STT_LOCAL_UNSUPPORTED,
    STT_NO_LOCAL_STT_RECOVERY,
)
from solstone.observe.transcribe.resource import (
    STT_SURFACE,
    local_stt_backend,
    resolve_stt_backend_choice,
    stt_local_floor_bytes,
)
from solstone.think.providers.memory import gb, read_available_bytes


def get_transcribe_resource_payload(
    *,
    configured_backend: str | None,
    confidential_lane_active: bool,
    confidential_audio: bool,
) -> dict[str, bool | float | int | str | None]:
    """Return the resource display payload for Settings transcription."""
    available_bytes = read_available_bytes()
    floor_bytes = stt_local_floor_bytes()
    local_backend = local_stt_backend()
    selected_backend = resolve_stt_backend_choice(
        configured_backend,
        available_bytes,
        floor_bytes=floor_bytes,
        local_backend=local_backend,
        confidential_lane_active=confidential_lane_active,
        confidential_audio_enabled=confidential_audio,
    )
    needs_setup = selected_backend == STT_SURFACE
    notice = STT_NO_LOCAL_STT_RECOVERY if needs_setup else ""

    return {
        "min_ram_gb": None if floor_bytes is None else floor_bytes // 1024**3,
        "available_memory_gb": gb(available_bytes),
        "requirement": _requirement_text(floor_bytes),
        "detected": _detected_text(available_bytes),
        "needs_setup": needs_setup,
        "notice": notice,
    }


def fallback_transcribe_resource_payload() -> dict[
    str, bool | float | int | str | None
]:
    """Return a type-stable fallback block without reading machine state."""
    return {
        "min_ram_gb": None,
        "available_memory_gb": None,
        "requirement": STT_LOCAL_UNSUPPORTED,
        "detected": STT_DETECTED_MEMORY_UNKNOWN,
        "needs_setup": False,
        "notice": "",
    }


def _requirement_text(floor_bytes: int | None) -> str:
    if floor_bytes is None:
        return STT_LOCAL_UNSUPPORTED
    return STT_LOCAL_REQUIREMENTS_TEMPLATE.format(ram_gb=floor_bytes // 1024**3)


def _detected_text(available_bytes: int | None) -> str:
    available_gb = gb(available_bytes)
    if available_gb is None:
        return STT_DETECTED_MEMORY_UNKNOWN
    return STT_DETECTED_MEMORY_TEMPLATE.format(available_gb=available_gb)


__all__ = [
    "fallback_transcribe_resource_payload",
    "get_transcribe_resource_payload",
]
