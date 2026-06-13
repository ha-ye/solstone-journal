# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

from solstone.apps.settings import install_copy

WORKSPACE = Path(__file__).resolve().parents[1] / "workspace.html"
LEGACY_TERMS = (
    "Downloading...",
    "Verifying...",
    "Installing...",
    "Validating key...",
    "Install may be stuck",
    "Retry?",
    "'enabling'",
    "'key-validating'",
    "'install-failed'",
    "'installed-no-key'",
    "'invalid-key'",
    "'not-enabled'",
    "stuck_enabling",
    "state.state",
)
UNIQUE_INSTALL_COPY_NAMES = (
    "STT_LOCAL_REQUIREMENTS_TEMPLATE",
    "STT_LOCAL_UNSUPPORTED",
    "STT_DETECTED_MEMORY_TEMPLATE",
    "STT_DETECTED_MEMORY_UNKNOWN",
    "STT_AUTO_SWITCH_NOTICE",
    "STT_FORCE_LOCAL_HINT",
    "STT_NO_KEY_RECOVERY",
    "STT_EXPLICIT_LOCAL_LOW_TEMPLATE",
)


def _workspace_text() -> str:
    return WORKSPACE.read_text(encoding="utf-8")


def _without_install_copy_declaration(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if "const INSTALL_COPY = " not in line
    )


def test_workspace_has_no_legacy_install_state_terms():
    text = _workspace_text()

    for term in LEGACY_TERMS:
        assert term not in text


def test_workspace_does_not_duplicate_install_copy_strings():
    text = _without_install_copy_declaration(_workspace_text())

    for name in UNIQUE_INSTALL_COPY_NAMES:
        value = getattr(install_copy, name)
        assert text.count(value) == 0
