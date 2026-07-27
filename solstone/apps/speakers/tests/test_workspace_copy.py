# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

WORKSPACE_HTML = Path(__file__).resolve().parents[1] / "workspace.html"


def test_workspace_copy_uses_sol_voice_copy() -> None:
    source = WORKSPACE_HTML.read_text(encoding="utf-8")

    new_strings = (
        "sol found ${clusters.length} recurring voice pattern"
        "${clusters.length > 1 ? 's' : ''} that "
        """${clusters.length > 1 ? "don't" : "doesn't"} match anyone you've """
        "named. listen to a few samples and name them.",
        '<div class="spk-discovery-title">new speakers found</div>',
        ": 'sol hasn\\'t found any speaker segments for this day yet';",
        "sol found a voice pattern that's likely yours, from "
        "${escapeHtml(data.cluster_size || 0)} voice samples. review a few "
        "examples before confirming.",
    )
    old_strings = (
        "We found ${clusters.length} recurring voice pattern"
        "${clusters.length > 1 ? 's' : ''} that "
        """${clusters.length > 1 ? "don't" : "doesn't"} match any kn"""
        + "own speaker. Listen to samples and name them.",
        '<div class="spk-discovery-title">New spea' + "kers found</div>",
        ": 'sol" + "stone hasn\\'t found any speaker segments for this day yet';",
        "we found a li"
        + "kely owner voice pattern from ${escapeHtml(data.cluster_size || 0)} "
        "voice samples. review a few examples before confirming.",
    )

    for expected in new_strings:
        assert expected in source
    for stale in old_strings:
        assert stale not in source
    assert source.count("solstone") == 0
    assert "heading: 'no speakers found'" in source
