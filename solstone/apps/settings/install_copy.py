# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

INSTALL_PHASE_IDLE = "Not installed"
INSTALL_PHASE_RESOLVING = "Resolving dependencies…"
INSTALL_PHASE_DOWNLOADING = "Downloading…"
INSTALL_PHASE_VERIFYING = "Verifying…"
INSTALL_PHASE_INSTALLING = "Installing…"
INSTALL_PHASE_INSTALLED = "Installed"
INSTALL_PHASE_FAILED_PREFIX = "Install failed — "

INSTALL_FAILED_FALLBACK = "try again"
INSTALL_FAILED_NO_PROGRESS = "no progress for 60 seconds — try again"
INSTALL_FAILED_UV_MISSING = (
    "uv not found — install uv (https://github.com/astral-sh/uv) and retry"
)

INSTALL_BUTTON_INSTALL = "Install"
INSTALL_BUTTON_INSTALLING = "Installing…"
INSTALL_BUTTON_RETRY = "Try again"

LOCAL_REQUIREMENTS_TEMPLATE = (
    "Needs {ram_gb} GB available memory and a {download_size} download. "
    "One local VLM handles both vision and thinking, so this does not double "
    "the memory requirement."
)
LOCAL_DETECTED_MEMORY_TEMPLATE = "{available_gb} GB available memory detected."
LOCAL_DETECTED_MEMORY_UNKNOWN = "Available memory could not be detected."
LOCAL_PATHS_FRAMING = (
    "Hosted key: light path, fastest setup. Local model: maximum-privacy path, "
    "heavier install and hardware needs, with model work staying on this machine."
)
LOCAL_EXPERIMENTAL_NOTE = "Local is experimental and may need hands-on setup or tuning."
LOCAL_RECOVERY_NO_HOSTED_KEY = (
    "Add a hosted key to use the light path now; you can revisit local after "
    "memory or hardware changes."
)
LOCAL_RECOVERY_HOSTED_KEY_SET = (
    "Your hosted key already covers generate and cogitate; keep using the light "
    "path and revisit local after memory or hardware changes."
)
LOCAL_MEMORY_WARNING_LOW_TEMPLATE = (
    "Available memory is below {ram_gb} GB. The local model can still install, "
    "but the rest of the system may be slow or unstable while it runs."
)
LOCAL_MEMORY_WARNING_UNKNOWN = (
    "Available memory could not be verified. Setup can continue, but local "
    "performance may depend on what else is running."
)
LOCAL_MLX_MEMORY_WARNING_UNKNOWN = (
    "Available memory could not be verified. Setup can continue, but this Mac "
    "may still need more free memory when the local model starts."
)

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
    "INSTALL_PHASE_IDLE",
    "INSTALL_PHASE_RESOLVING",
    "INSTALL_PHASE_DOWNLOADING",
    "INSTALL_PHASE_VERIFYING",
    "INSTALL_PHASE_INSTALLING",
    "INSTALL_PHASE_INSTALLED",
    "INSTALL_PHASE_FAILED_PREFIX",
    "INSTALL_FAILED_FALLBACK",
    "INSTALL_FAILED_NO_PROGRESS",
    "INSTALL_FAILED_UV_MISSING",
    "INSTALL_BUTTON_INSTALL",
    "INSTALL_BUTTON_INSTALLING",
    "INSTALL_BUTTON_RETRY",
    "LOCAL_REQUIREMENTS_TEMPLATE",
    "LOCAL_DETECTED_MEMORY_TEMPLATE",
    "LOCAL_DETECTED_MEMORY_UNKNOWN",
    "LOCAL_PATHS_FRAMING",
    "LOCAL_EXPERIMENTAL_NOTE",
    "LOCAL_RECOVERY_NO_HOSTED_KEY",
    "LOCAL_RECOVERY_HOSTED_KEY_SET",
    "LOCAL_MEMORY_WARNING_LOW_TEMPLATE",
    "LOCAL_MEMORY_WARNING_UNKNOWN",
    "LOCAL_MLX_MEMORY_WARNING_UNKNOWN",
    "STT_LOCAL_REQUIREMENTS_TEMPLATE",
    "STT_LOCAL_UNSUPPORTED",
    "STT_DETECTED_MEMORY_TEMPLATE",
    "STT_DETECTED_MEMORY_UNKNOWN",
    "STT_AUTO_SWITCH_NOTICE",
    "STT_FORCE_LOCAL_HINT",
    "STT_NO_KEY_RECOVERY",
    "STT_EXPLICIT_LOCAL_LOW_TEMPLATE",
]
