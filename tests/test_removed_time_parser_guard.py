# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from scripts.check_removed_time_parser_ready import find_blockers


def test_synthetic_production_caller_makes_removed_time_parser_retain(tmp_path):
    caller = tmp_path / "solstone" / "think" / "new_caller.py"
    caller.parent.mkdir(parents=True)
    caller.write_text(
        "from solstone.think.utils import parse_time_range\n",
        encoding="utf-8",
    )

    blockers = find_blockers(tmp_path)

    assert len(blockers) == 1
    assert blockers[0].path == caller
    assert blockers[0].line_number == 1
