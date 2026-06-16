# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import sys

import pytest

from solstone.think import settings_cli


def _run(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["journal settings", *args])
    settings_cli.main()


def test_status_json_does_not_require_running_stack(
    journal_copy,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(settings_cli, "get_host_url", lambda: "http://localhost:5015")

    _run(monkeypatch, ["convey", "status", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {
        "effective_host_url": "http://localhost:5015",
    }
    assert captured.err == ""


def test_missing_subcommand_prints_help_and_exits_one(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["journal settings"])

    with pytest.raises(SystemExit) as exc_info:
        settings_cli.main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "Manage local journal settings" in captured.out
