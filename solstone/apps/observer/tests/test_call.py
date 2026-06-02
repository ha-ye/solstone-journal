# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for observer CLI commands."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from solstone.think.call import call_app
from solstone.think.streams import update_stream, write_segment_stream

runner = CliRunner()


def test_delete_source_cli_runs_same_operation(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    seg_dir = journal / "chronicle" / "20260107" / "import.share" / "090000_300"
    seg_dir.mkdir(parents=True)
    (seg_dir / "doc.pdf").write_bytes(b"pdf")
    (seg_dir / "doc.jsonl").write_text('{"text": "derived"}\n', encoding="utf-8")
    (seg_dir / "item.json").write_text("{}\n", encoding="utf-8")
    state = update_stream("import.share", "20260107", "090000_300", type="import")
    write_segment_stream(
        seg_dir,
        "import.share",
        state["prev_day"],
        state["prev_segment"],
        state["seq"],
    )

    result = runner.invoke(call_app, ["observer", "delete-source"])

    assert result.exit_code == 0
    receipt = json.loads(result.stdout)
    assert receipt["target"]["stream"] == "import.share"
    assert receipt["removed"]["segments"] == 1
    assert receipt["removed"]["originals"] == 1
    assert receipt["removed"]["in_segment_derived"] == 1
    assert not seg_dir.exists()
