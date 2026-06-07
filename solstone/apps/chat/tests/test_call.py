# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import solstone.convey.sol_initiated.start as sol_start
from solstone.apps.chat.call import app
from solstone.convey.chat_stream import read_chat_events
from solstone.think.convey_client import ConveyClient
from tests._baseline_harness import make_logged_in_test_client

FROZEN_MS = 1_700_000_000_000
FROZEN_DAY = datetime.fromtimestamp(FROZEN_MS / 1000).strftime("%Y%m%d")
VALID_SINCE_TS = FROZEN_MS - 60_000


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config_path = tmp_path / "config" / "journal.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"sol_voice": {"rate_floor_minutes": 0}}) + "\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def runner(journal, monkeypatch):
    monkeypatch.setattr(sol_start, "now_ms", lambda: FROZEN_MS)
    client = ConveyClient(session=make_logged_in_test_client(journal), base_url="")
    monkeypatch.setattr("solstone.apps.chat.call.get_client", lambda: client)
    return CliRunner()


def _args(
    *,
    summary: str = "summary",
    message: str | None = None,
    category: str = "briefing",
    dedupe: str = "k",
    dedupe_window: str | None = None,
    since_ts: int = VALID_SINCE_TS,
    trigger_talent: str = "reflection",
) -> list[str]:
    args = [
        "--summary",
        summary,
        "--category",
        category,
        "--dedupe",
        dedupe,
        "--since-ts",
        str(since_ts),
        "--trigger-talent",
        trigger_talent,
    ]
    if message is not None:
        args.extend(["--message", message])
    if dedupe_window is not None:
        args.extend(["--dedupe-window", dedupe_window])
    return args


def test_start_writes_request(runner):
    result = runner.invoke(app, _args(message="body"))

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["written"] is True
    assert data["deduped"] is False
    assert data["throttled"] is None
    assert isinstance(data["request_id"], str) and len(data["request_id"]) == 32


def test_start_dedupes_within_window(runner):
    first = runner.invoke(app, _args(dedupe="same"))
    second = runner.invoke(app, _args(dedupe="same"))

    assert first.exit_code == 0
    data = json.loads(second.stdout)
    assert second.exit_code == 0
    assert data["deduped"] is True
    assert data["written"] is False


@pytest.mark.parametrize(
    ("args", "stderr"),
    [
        (_args(summary=""), "Error: summary is required\n"),
        (_args(summary="x" * 81), "Error: summary must be 80 characters or fewer\n"),
        (_args(dedupe=""), "Error: dedupe is required\n"),
        (_args(trigger_talent=""), "Error: trigger_talent is required\n"),
        (_args(since_ts=0), "Error: since_ts must be positive\n"),
        (
            _args(message="x" * 501),
            "Error: message must be 500 characters or fewer\n",
        ),
    ],
)
def test_start_cli_side_validation_errors_before_post(runner, args, stderr):
    result = runner.invoke(app, args)

    assert result.exit_code == 1
    assert result.stderr == stderr
    assert result.stdout == ""
    assert read_chat_events(FROZEN_DAY) == []


def test_start_bad_category_uses_route_internal_owner_voice(runner):
    result = runner.invoke(app, _args(category="nope"))

    assert result.exit_code == 1
    assert result.stderr == "I couldn't use one of those values.\n"
    assert result.stdout == ""


def test_convey_down_prints_require_solstone_message(journal, monkeypatch):
    class DownSession:
        def get(self, _url):
            raise requests.exceptions.ConnectionError()

        def post(self, _url, json=None):
            raise requests.exceptions.ConnectionError()

    client = ConveyClient(session=DownSession(), base_url="http://localhost:5015")
    monkeypatch.setattr("solstone.apps.chat.call.get_client", lambda: client)
    monkeypatch.delenv("SOL_SKIP_SUPERVISOR_CHECK", raising=False)
    monkeypatch.delenv("SOL_SUPERVISOR_SPAWNED", raising=False)

    result = CliRunner().invoke(app, _args())

    assert result.exit_code == 1
    assert (
        result.stderr
        == "sol: solstone isn't running. Start it with 'journal up' and retry.\n"
    )
    assert result.stdout == ""
