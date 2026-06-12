# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from pathlib import Path


def test_summary_pluralizes_result_and_day():
    html = (Path(__file__).resolve().parents[1] / "workspace.html").read_text(
        encoding="utf-8"
    )

    assert "result${data.total !== 1 ? 's' : ''}" in html
    assert "day${data.total_days !== 1 ? 's' : ''}" in html
