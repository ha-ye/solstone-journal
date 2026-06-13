# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Settings payload assembly for resource-aware transcription defaults."""

from __future__ import annotations

from solstone.apps.settings.install_copy import (
    STT_AUTO_SWITCH_NOTICE,
    STT_DETECTED_MEMORY_TEMPLATE,
    STT_DETECTED_MEMORY_UNKNOWN,
    STT_FORCE_LOCAL_HINT,
    STT_LOCAL_REQUIREMENTS_TEMPLATE,
    STT_LOCAL_UNSUPPORTED,
    STT_NO_KEY_RECOVERY,
)
from solstone.observe.transcribe.resource import (
    STT_SURFACE,
    local_stt_backend,
    select_stt_backend,
    stt_local_floor_bytes,
)
from solstone.think.providers.memory import gb, read_available_bytes


def get_transcribe_resource_payload(
    *, google_key_present: bool, configured_backend: str | None
) -> dict[str, bool | float | int | str | None]:
    """Return the resource display payload for Settings transcription."""
    available_bytes = read_available_bytes()
    floor_bytes = stt_local_floor_bytes()
    local_backend = local_stt_backend()
    selected_backend = ""
    if not configured_backend:
        selected_backend = select_stt_backend(
            available_bytes,
            google_key_present=google_key_present,
            floor_bytes=floor_bytes,
            local_backend=local_backend,
        )
    auto_switched = selected_backend == "gemini"
    needs_setup = selected_backend == STT_SURFACE
    notice = ""
    force_local_hint = ""
    if auto_switched:
        notice = STT_AUTO_SWITCH_NOTICE
        force_local_hint = STT_FORCE_LOCAL_HINT
    elif needs_setup:
        notice = STT_NO_KEY_RECOVERY

    return {
        "min_ram_gb": None if floor_bytes is None else floor_bytes // 1024**3,
        "available_memory_gb": gb(available_bytes),
        "requirement": _requirement_text(floor_bytes),
        "detected": _detected_text(available_bytes),
        "auto_switched": auto_switched,
        "needs_setup": needs_setup,
        "notice": notice,
        "force_local_hint": force_local_hint,
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
        "auto_switched": False,
        "needs_setup": False,
        "notice": "",
        "force_local_hint": "",
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
