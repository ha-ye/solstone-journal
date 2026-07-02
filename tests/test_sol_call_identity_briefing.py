# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for ``journal identity briefing``."""

from typer.testing import CliRunner

from solstone.think.tools.sol import app

runner = CliRunner()


def _seed_briefing(day: str, body: str) -> None:
    from solstone.think.talent import morning_briefing_path

    path = morning_briefing_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_briefing_no_day_returns_most_recent_available(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    _seed_briefing("20260101", "# Morning Briefing\n\nolder-marker\n")
    from solstone.think.talent import morning_briefing_path

    morning_briefing_path("20260102").parent.mkdir(parents=True, exist_ok=True)

    result = runner.invoke(app, ["briefing"])

    assert result.exit_code == 0
    assert "older-marker" in result.stdout


def test_briefing_no_briefing_anywhere_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    result = runner.invoke(app, ["briefing"])

    assert result.exit_code == 1
