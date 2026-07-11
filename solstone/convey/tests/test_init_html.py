# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

INIT_HTML = Path(__file__).resolve().parents[1] / "templates" / "init.html"


def _init_text() -> str:
    return INIT_HTML.read_text(encoding="utf-8")


def test_provider_key_ui_removed():
    text = _init_text()

    for deleted in (
        "gemini-key",
        "gemini-validate",
        "provider-key-block",
        "password-toggle",
        "validate-provider",
    ):
        assert deleted not in text


def test_result_display_ms_constant_present():
    text = _init_text()

    assert text.count("RESULT_DISPLAY_MS = 1200") == 1


def test_scout_setup_deep_link_removed():
    text = _init_text()

    assert "scout-setup" not in text


def test_wizard_self_contained():
    text = _init_text()

    assert text.count('<link rel="stylesheet"') == 1
    assert '<link rel="stylesheet" href="/static/tokens.css">' in text
    assert text.count("<script src=") == 2
