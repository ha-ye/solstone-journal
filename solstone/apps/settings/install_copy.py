# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

STT_LOCAL_REQUIREMENTS_TEMPLATE = (
    "Local transcription needs about {ram_gb} GB of free memory for the on-device "
    "model (transcription, speaker labels, and overlap detection)."
)
STT_LOCAL_UNSUPPORTED = "Local transcription is not available on this platform."
STT_DETECTED_MEMORY_TEMPLATE = (
    "{available_gb} GB of free memory detected on this machine."
)
STT_DETECTED_MEMORY_UNKNOWN = "Free memory on this machine could not be detected."
STT_AUTO_SWITCH_NOTICE = (
    "Local transcription is not available with the current free memory or platform, "
    "so hosted Google transcription is in use for now."
)
STT_FORCE_LOCAL_HINT = (
    "To force local transcription on a supported platform, choose Parakeet above; "
    "it may run slowly or strain this machine until more memory is free."
)
STT_NO_KEY_RECOVERY = (
    "Add a hosted Google key to transcribe now, or use a supported platform with "
    "enough free memory to run locally."
)
STT_EXPLICIT_LOCAL_LOW_TEMPLATE = (
    "Free memory is below {ram_gb} GB. Local transcription can still run, but this "
    "machine may be slow or unstable while it does."
)


__all__ = [
    "STT_LOCAL_REQUIREMENTS_TEMPLATE",
    "STT_LOCAL_UNSUPPORTED",
    "STT_DETECTED_MEMORY_TEMPLATE",
    "STT_DETECTED_MEMORY_UNKNOWN",
    "STT_AUTO_SWITCH_NOTICE",
    "STT_FORCE_LOCAL_HINT",
    "STT_NO_KEY_RECOVERY",
    "STT_EXPLICIT_LOCAL_LOW_TEMPLATE",
]
