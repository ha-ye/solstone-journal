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
]
