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
]
