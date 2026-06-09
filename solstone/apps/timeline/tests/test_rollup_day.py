# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for timeline day rollup command."""

from __future__ import annotations

import asyncio
import json

from solstone.apps.timeline.maintenance import _rollup_day, run_rollup_day
from solstone.apps.timeline.tests.conftest import write_json
from solstone.think.models import GEMINI_FLASH, GEMINI_LITE

DAY = "20260512"


def _write_segment(journal, day, segment, title, hour_stream="archon"):
    write_json(
        journal / "chronicle" / day / hour_stream / segment / "timeline.json",
        {
            "title": title,
            "description": f"{title} description.",
            "origin": f"{day}/{hour_stream}/{segment}",
            "model": GEMINI_LITE,
            "generated_at": 1770000000,
        },
    )


def test_rollup_day_run_wrapper_routes_args(timeline_journal, monkeypatch):
    """AC#5."""
    seen = {}

    async def fake_rollup(journal, day, top, jobs, dry_run, force):
        seen.update(
            {
                "journal": journal,
                "day": day,
                "top": top,
                "jobs": jobs,
                "dry_run": dry_run,
                "force": force,
            }
        )
        return 0

    monkeypatch.setattr("solstone.apps.timeline.maintenance._rollup_day", fake_rollup)

    result = run_rollup_day(
        ["20260512", "--top", "2", "--jobs", "3", "--dry-run", "--force"]
    )

    assert result == 0
    assert seen == {
        "journal": timeline_journal,
        "day": DAY,
        "top": 2,
        "jobs": 3,
        "dry_run": True,
        "force": True,
    }


def test_rollup_day_empty_input_exits_empty_sentinel(timeline_journal):
    from solstone.think.utils import EXIT_EMPTY

    result = asyncio.run(
        _rollup_day(timeline_journal, DAY, top=4, jobs=5, dry_run=False, force=False)
    )

    assert result == EXIT_EMPTY
    assert not (timeline_journal / "chronicle" / DAY / "timeline.json").exists()


def test_rollup_day_dry_run_no_llm_calls(timeline_journal, mock_agenerate):
    """AC#7."""
    mock = mock_agenerate({"picks": [0], "rationale": "unused"})
    _write_segment(timeline_journal, DAY, "120000_60", "One")

    asyncio.run(
        _rollup_day(timeline_journal, DAY, top=4, jobs=5, dry_run=True, force=False)
    )

    assert mock.call_count == 0


def test_rollup_day_writes_seed_shape(timeline_journal, mock_agenerate):
    """AC#6."""
    for i in range(5):
        title = "Café Event" if i == 0 else f"Event {i}"
        _write_segment(timeline_journal, DAY, f"12000{i}_60", title)
    mock = mock_agenerate({"picks": [0, 1, 2, 3], "rationale": "highest consequence"})

    asyncio.run(
        _rollup_day(timeline_journal, DAY, top=4, jobs=5, dry_run=False, force=False)
    )

    timeline_path = timeline_journal / "chronicle" / DAY / "timeline.json"
    payload = json.loads(timeline_path.read_text())
    assert payload["day"] == DAY
    assert payload["model"] == GEMINI_FLASH
    assert payload["segment_count"] == 5
    assert payload["hour_count"] == 1
    assert len(payload["day_top"]) == 4
    assert payload["hours"]["12"]["rationale"] == "highest consequence"
    assert mock.call_args.kwargs["model"] == GEMINI_FLASH
    raw = timeline_path.read_bytes()
    assert b"Caf\xc3\xa9 Event" in raw
    assert b"\\u00e9" not in raw
    assert raw.endswith(b"\n")


def test_rollup_day_skip_when_exists_without_force(timeline_journal, mock_agenerate):
    """AC#6, AC#7."""
    write_json(
        timeline_journal / "chronicle" / DAY / "timeline.json", {"existing": True}
    )
    mock = mock_agenerate({"picks": [0], "rationale": "unused"})
    _write_segment(timeline_journal, DAY, "120000_60", "One")

    asyncio.run(
        _rollup_day(timeline_journal, DAY, top=4, jobs=5, dry_run=False, force=False)
    )

    assert json.loads(
        (timeline_journal / "chronicle" / DAY / "timeline.json").read_text()
    ) == {"existing": True}
    assert mock.call_count == 0


def test_rollup_day_force_overwrites_atomically(timeline_journal):
    """AC#6."""
    write_json(
        timeline_journal / "chronicle" / DAY / "timeline.json", {"existing": True}
    )
    _write_segment(timeline_journal, DAY, "120000_60", "One")

    asyncio.run(
        _rollup_day(timeline_journal, DAY, top=4, jobs=5, dry_run=False, force=True)
    )

    timeline_path = timeline_journal / "chronicle" / DAY / "timeline.json"
    payload = json.loads(timeline_path.read_text())
    assert payload["day"] == DAY
    assert payload["day_top"][0]["title"] == "One"
    assert not list(timeline_path.parent.glob("*.tmp"))


def test_rollup_day_hour_error_continues_picks_empty_with_error_field(
    timeline_journal,
    mock_agenerate,
):
    """AC#20."""
    for i in range(5):
        _write_segment(timeline_journal, DAY, f"12000{i}_60", f"Noon {i}")
        _write_segment(timeline_journal, DAY, f"13000{i}_60", f"One {i}")
    mock_agenerate(
        RuntimeError("hour backend down"),
        {"picks": [0, 1, 2, 3], "rationale": "one pm"},
    )

    asyncio.run(
        _rollup_day(timeline_journal, DAY, top=4, jobs=5, dry_run=False, force=False)
    )

    payload = json.loads(
        (timeline_journal / "chronicle" / DAY / "timeline.json").read_text()
    )
    assert payload["hours"]["12"]["picks"] == []
    assert "hour backend down" in payload["hours"]["12"]["error"]
    assert len(payload["day_top"]) == 4


def test_rollup_day_final_error_skips_write_exits_zero(
    timeline_journal, mock_agenerate
):
    """AC#21."""
    for i in range(2):
        _write_segment(timeline_journal, DAY, f"12000{i}_60", f"Noon {i}")
        _write_segment(timeline_journal, DAY, f"13000{i}_60", f"One {i}")
    mock_agenerate(
        {"picks": [0], "rationale": "noon"},
        {"picks": [0], "rationale": "one"},
        RuntimeError("day backend down"),
    )

    result = asyncio.run(
        _rollup_day(timeline_journal, DAY, top=1, jobs=5, dry_run=False, force=False)
    )

    assert result == 0
    assert not (timeline_journal / "chronicle" / DAY / "timeline.json").exists()
