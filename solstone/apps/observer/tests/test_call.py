# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for observer CLI commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solstone.think.call import call_app
from solstone.think.convey_client import ConveyClient
from solstone.think.streams import update_stream, write_segment_stream
from tests._baseline_harness import make_test_client, mark_setup_complete


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    mark_setup_complete(tmp_path)
    return tmp_path


@pytest.fixture
def runner(journal, monkeypatch):
    client = ConveyClient(session=make_test_client(journal), base_url="")
    monkeypatch.setattr("solstone.apps.observer.call.get_client", lambda: client)
    return CliRunner()


def _seed_share_segment(journal):
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
    return seg_dir


def test_delete_source_cli_runs_same_operation(runner, journal):
    seg_dir = _seed_share_segment(journal)

    result = runner.invoke(call_app, ["observer", "delete-source"])

    assert result.exit_code == 0
    receipt = json.loads(result.stdout)
    assert receipt["target"]["stream"] == "import.share"
    assert receipt["removed"]["segments"] == 1
    assert receipt["removed"]["originals"] == 1
    assert receipt["removed"]["in_segment_derived"] == 1
    assert not seg_dir.exists()


def test_convey_down_prints_require_solstone_message(journal, monkeypatch) -> None:
    class DownSession:
        def get(self, _url):
            raise requests.exceptions.ConnectionError()

        def post(self, _url, json=None):
            raise requests.exceptions.ConnectionError()

    client = ConveyClient(session=DownSession(), base_url="http://localhost:5015")
    monkeypatch.setattr("solstone.apps.observer.call.get_client", lambda: client)
    monkeypatch.delenv("SOL_SKIP_SUPERVISOR_CHECK", raising=False)
    monkeypatch.delenv("SOL_SUPERVISOR_SPAWNED", raising=False)

    result = CliRunner().invoke(call_app, ["observer", "delete-source"])

    assert result.exit_code == 1
    assert (
        result.stderr
        == "sol: solstone isn't running. Start it with 'journal up' and retry.\n"
    )
    assert result.stdout == ""
