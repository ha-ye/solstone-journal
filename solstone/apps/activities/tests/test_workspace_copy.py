# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1] / "workspace.html"


def test_activities_workspace_inline_copy_is_folded():
    text = WORKSPACE.read_text(encoding="utf-8")

    expected_literals = (
        "loading activities…",
        "<dt>time</dt>",
        "<dt>duration</dt>",
        "<dt>engagement</dt>",
        "<dt>segments</dt>",
        "<dt>entities</dt>",
        '<div class="ad-output-loading">loading…</div>',
    )
    for literal in expected_literals:
        assert literal in text

    retired_literals = (
        "Loading activities...",
        "<dt>Time</dt>",
        "<dt>Duration</dt>",
        "<dt>Engagement</dt>",
        "<dt>Segments</dt>",
        "<dt>Entities</dt>",
        '<div class="ad-output-loading">Loading...</div>',
    )
    for literal in retired_literals:
        assert literal not in text
