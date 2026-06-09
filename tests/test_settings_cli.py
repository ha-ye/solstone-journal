# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from solstone.apps.settings.copy import (
    CONVEY_NETWORK_DISABLE_DONE,
    CONVEY_NETWORK_DISABLE_PROGRESS,
    CONVEY_NETWORK_ENABLE_DONE,
    CONVEY_NETWORK_ENABLE_PROGRESS,
    CONVEY_REFUSE_NO_PASSWORD_NETWORK,
    CONVEY_RESTART_TIMEOUT,
)
from solstone.think import settings_cli


def _read_config(journal_dir: Path) -> dict:
    return json.loads((journal_dir / "config" / "journal.json").read_text("utf-8"))


def _write_config(journal_dir: Path, payload: dict) -> None:
    (journal_dir / "config" / "journal.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def _clear_password(journal_dir: Path) -> None:
    config = _read_config(journal_dir)
    config["convey"].pop("password_hash", None)
    config["convey"].pop("password", None)
    _write_config(journal_dir, config)


def _run(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["journal settings", *args])
    settings_cli.main()


def test_status_json_does_not_require_running_stack(
    journal_copy,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        settings_cli,
        "require_solstone",
        lambda: pytest.fail("status should not require a running stack"),
    )
    monkeypatch.setattr(settings_cli, "get_host_url", lambda: "http://localhost:5015")

    _run(monkeypatch, ["convey", "status", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {
        "effective_host_url": "http://localhost:5015",
        "network_access_enabled": False,
        "password_configured": True,
    }
    assert "can_change_network_access" not in payload
    assert captured.err == ""


def test_network_access_enable_refuses_without_password(
    journal_copy,
    monkeypatch,
    capsys,
):
    _clear_password(journal_copy)
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")
    before = _read_config(journal_copy)

    with patch("solstone.convey.restart.wait_for_convey_restart") as restart:
        with pytest.raises(SystemExit) as exc_info:
            _run(monkeypatch, ["convey", "network-access", "enable"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert captured.out == ""
    assert captured.err == CONVEY_REFUSE_NO_PASSWORD_NETWORK + "\n"
    restart.assert_not_called()
    assert _read_config(journal_copy) == before


def test_network_access_enable_persists_restarts_and_prints_host_url(
    journal_copy,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")

    with (
        patch(
            "solstone.convey.restart.wait_for_convey_restart", return_value=(True, [])
        ) as restart,
        patch(
            "solstone.think.pairing.config.get_host_url",
            return_value="http://192.168.1.44:5015",
        ),
    ):
        _run(monkeypatch, ["convey", "network-access", "enable"])

    captured = capsys.readouterr()
    assert captured.out == (
        CONVEY_NETWORK_ENABLE_PROGRESS
        + "\n"
        + CONVEY_NETWORK_ENABLE_DONE.format(host_url="http://192.168.1.44:5015")
        + "\n"
    )
    assert captured.err == ""
    restart.assert_called_once_with(timeout=15.0)
    assert _read_config(journal_copy)["convey"]["allow_network_access"] is True


def test_network_access_disable_persists_and_prints_localhost(
    journal_copy,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")
    config = _read_config(journal_copy)
    config["convey"]["allow_network_access"] = True
    _write_config(journal_copy, config)

    with patch(
        "solstone.convey.restart.wait_for_convey_restart", return_value=(True, [])
    ) as restart:
        _run(monkeypatch, ["convey", "network-access", "disable"])

    captured = capsys.readouterr()
    assert captured.out == (
        CONVEY_NETWORK_DISABLE_PROGRESS
        + "\n"
        + CONVEY_NETWORK_DISABLE_DONE.format(port=5015)
        + "\n"
    )
    assert captured.err == ""
    restart.assert_called_once_with(timeout=15.0)
    assert _read_config(journal_copy)["convey"]["allow_network_access"] is False


def test_network_access_timeout_exits_nonzero_but_keeps_persisted_state(
    journal_copy,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")
    config = _read_config(journal_copy)
    config["convey"]["allow_network_access"] = True
    _write_config(journal_copy, config)

    with (
        patch(
            "solstone.convey.restart.wait_for_convey_restart", return_value=(False, [])
        ),
        patch(
            "solstone.think.pairing.config.get_host_url",
            return_value="http://localhost:5015",
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _run(monkeypatch, ["convey", "network-access", "disable"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert captured.out == CONVEY_NETWORK_DISABLE_PROGRESS + "\n"
    assert captured.err == CONVEY_RESTART_TIMEOUT + "\n"
    assert _read_config(journal_copy)["convey"]["allow_network_access"] is False


def test_missing_subcommand_prints_help_and_exits_one(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["journal settings"])

    with pytest.raises(SystemExit) as exc_info:
        settings_cli.main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "Manage local journal settings" in captured.out
