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


def test_ignored_scratch_dir_is_not_shipped_source(tmp_path):
    """scratch/ is git-ignored working space; only its .gitignore is tracked.

    Agent session transcripts land there and quote historical dependency lists
    verbatim, so a filesystem walk that includes scratch/ reds the release gate
    on a developer clone while the shipped source is clean. The exclusion must
    be narrow: a real caller sitting beside the scratch file still blocks.
    """
    scratch = tmp_path / "scratch" / "codex_stream_events.jsonl"
    scratch.parent.mkdir(parents=True)
    scratch.write_text('{"aggregated_output": "timefhuman"}\n', encoding="utf-8")

    assert find_blockers(tmp_path) == []

    caller = tmp_path / "solstone" / "think" / "live_caller.py"
    caller.parent.mkdir(parents=True)
    caller.write_text(
        "from solstone.think.utils import parse_time_range\n", encoding="utf-8"
    )

    blockers = find_blockers(tmp_path)
    assert [b.path for b in blockers] == [caller]
