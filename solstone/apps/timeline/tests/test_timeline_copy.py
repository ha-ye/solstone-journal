# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

TIMELINE_JS_PATH = Path(__file__).resolve().parents[1] / "static" / "timeline.js"


def _timeline_js() -> str:
    return TIMELINE_JS_PATH.read_text(encoding="utf-8")


def test_timeline_empty_copy_is_pinned_byte_for_byte():
    source = _timeline_js()

    assert "`nothing in your journal for ${month.name}`," in source
    assert "`nothing in your journal for ${dateLabel}`," in source
    assert '"nothing in this hour",' in source
    assert "`sol kept nothing for ${formatTime(hour, 0)}.`," in source
    assert ': "nothing kept";' in source
    assert '<div class="segment-empty">nothing kept in this slice</div>' in source
    assert (
        '<section class="segment-panel" aria-label="${month.name} ${day}, '
        '${month.year || ""} ${focusLabel} — what sol kept">' in source
    )
    assert '<div class="river-screen" aria-label="screen frames sol kept">' in source


def test_timeline_segment_structure_tokens_survive_copy_migration():
    source = _timeline_js()

    assert (
        '<section class="segment-panel" aria-label="${month.name} ${day}, '
        '${month.year || ""} ${focusLabel} — what sol kept">' in source
    )
    assert "segment-cell" in source
    assert "segmentCount" in source
