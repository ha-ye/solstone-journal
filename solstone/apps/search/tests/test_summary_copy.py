# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from pathlib import Path


def test_summary_pluralizes_match_and_day():
    html = (Path(__file__).resolve().parents[1] / "workspace.html").read_text(
        encoding="utf-8"
    )

    assert "match${data.total !== 1 ? 'es' : ''}" in html
    assert "day${data.total_days !== 1 ? 's' : ''}" in html


def test_workspace_search_controls_use_folded_copy():
    html = (Path(__file__).resolve().parents[1] / "workspace.html").read_text(
        encoding="utf-8"
    )

    assert "load more days" in html
    assert "window.SurfaceState.loading({ text: 'searching…' })" in html
    assert "`show ${dayData.total - dayData.showing} more`" in html
    assert "`show ${remaining} more`" in html
    assert '"couldn\'t load more — click to retry"' in html
    assert "`load more days (${shownDays}/${totalDays})`" in html
    assert "loadMoreDaysBtn.textContent = `Load more days" not in html
    assert ">\n        Load more days\n      </button>" not in html
    assert "Searching..." not in html
    assert "error - click to retry" not in html
