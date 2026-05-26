# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import plistlib
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from solstone.think import service

STALE_SHAPES = [
    ("sol-supervisor-no-port", "sol", ["supervisor"], []),
    ("sol-supervisor-port", "sol", ["supervisor", "5015"], ["5015"]),
    ("journal-supervisor-no-port", "journal", ["supervisor"], []),
    ("journal-supervisor-port", "journal", ["supervisor", "5015"], ["5015"]),
]


def _managed(tmp_path: Path):
    return lambda binary: str(tmp_path / "current" / binary)


def _old_binary(tmp_path: Path, binary: str) -> str:
    return str(tmp_path / "old" / binary)


@pytest.mark.skipif(sys.platform != "linux", reason="systemd reconcile shape")
@pytest.mark.parametrize(("case", "binary", "args", "tail"), STALE_SHAPES)
def test_reconcile_rewrites_stale_systemd_unit_to_journal_start(
    monkeypatch,
    tmp_path,
    case,
    binary,
    args,
    tail,
):
    del case
    unit_path = tmp_path / "solstone.service"
    old_args = [_old_binary(tmp_path, binary), *args]
    unit_path.write_text(
        "[Unit]\n"
        "Description=Solstone Supervisor\n"
        "[Service]\n"
        f"ExecStart={shlex.join(old_args)}\n"
        "Restart=on-failure\n",
        encoding="utf-8",
    )
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(service, "_unit_path", lambda: unit_path)
    monkeypatch.setattr(service, "_managed_wrapper", _managed(tmp_path))
    monkeypatch.setattr(service.subprocess, "run", run)

    result = service.reconcile_installed_unit()

    expected = [str(tmp_path / "current" / "journal"), "start", *tail]
    assert result.was_stale is True
    assert result.stale_binary == binary
    assert result.stale_verb == "supervisor"
    assert result.canonical_path == unit_path
    assert f"ExecStart={shlex.join(expected)}\n" in unit_path.read_text(
        encoding="utf-8"
    )
    run.assert_called_once_with(["systemctl", "--user", "daemon-reload"], check=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd reconcile shape")
@pytest.mark.parametrize(("case", "binary", "args", "tail"), STALE_SHAPES)
def test_reconcile_rewrites_stale_launchd_plist_to_journal_start(
    monkeypatch,
    tmp_path,
    case,
    binary,
    args,
    tail,
):
    del case
    plist_path = tmp_path / "org.solpbc.solstone.plist"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": service.SERVICE_LABEL,
                "ProgramArguments": [_old_binary(tmp_path, binary), *args],
            }
        )
    )
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "_plist_path", lambda: plist_path)
    monkeypatch.setattr(service, "_managed_wrapper", _managed(tmp_path))
    monkeypatch.setattr(service.os, "getuid", lambda: 501)
    monkeypatch.setattr(service.subprocess, "run", run)

    result = service.reconcile_installed_unit()

    data = plistlib.loads(plist_path.read_bytes())
    assert result.was_stale is True
    assert result.stale_binary == binary
    assert result.stale_verb == "supervisor"
    assert data["ProgramArguments"] == [
        str(tmp_path / "current" / "journal"),
        "start",
        *tail,
    ]
    assert run.call_args_list == [
        call(
            ["launchctl", "bootout", "gui/501", str(plist_path)],
            capture_output=True,
        ),
        call(
            ["launchctl", "bootstrap", "gui/501", str(plist_path)],
            capture_output=True,
            text=True,
        ),
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="systemd reconcile shape")
@pytest.mark.parametrize("tail", [[], ["5015"]])
def test_reconcile_canonical_systemd_noop(monkeypatch, tmp_path, tail):
    unit_path = tmp_path / "solstone.service"
    current = [str(tmp_path / "current" / "journal"), "start", *tail]
    unit_path.write_text(
        "[Unit]\n"
        "Description=Solstone Supervisor\n"
        "[Service]\n"
        f"ExecStart={shlex.join(current)}\n",
        encoding="utf-8",
    )
    run = MagicMock()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(service, "_unit_path", lambda: unit_path)
    monkeypatch.setattr(service, "_managed_wrapper", _managed(tmp_path))
    monkeypatch.setattr(service.subprocess, "run", run)

    result = service.reconcile_installed_unit()

    assert result.was_stale is False
    assert unit_path.read_text(encoding="utf-8").count("ExecStart=") == 1
    run.assert_not_called()


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd reconcile shape")
@pytest.mark.parametrize("tail", [[], ["5015"]])
def test_reconcile_canonical_launchd_noop(monkeypatch, tmp_path, tail):
    plist_path = tmp_path / "org.solpbc.solstone.plist"
    current = [str(tmp_path / "current" / "journal"), "start", *tail]
    plist_path.write_bytes(
        plistlib.dumps({"Label": service.SERVICE_LABEL, "ProgramArguments": current})
    )
    run = MagicMock()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(service, "_plist_path", lambda: plist_path)
    monkeypatch.setattr(service, "_managed_wrapper", _managed(tmp_path))
    monkeypatch.setattr(service.subprocess, "run", run)

    result = service.reconcile_installed_unit()

    assert result.was_stale is False
    assert plistlib.loads(plist_path.read_bytes())["ProgramArguments"] == current
    run.assert_not_called()


@pytest.mark.skipif(sys.platform != "linux", reason="systemd reconcile shape")
def test_reconcile_no_installed_unit_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(service, "_unit_path", lambda: tmp_path / "missing.service")

    result = service.reconcile_installed_unit()

    assert result == service.Reconciled(False, None, None, None)


@pytest.mark.skipif(sys.platform != "linux", reason="systemd reconcile shape")
def test_reconcile_parse_error_logs_and_skips(monkeypatch, tmp_path, capsys):
    unit_path = tmp_path / "solstone.service"
    unit_path.write_text(
        '[Unit]\nDescription=Solstone Supervisor\n[Service]\nExecStart="unterminated\n',
        encoding="utf-8",
    )
    run = MagicMock()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(service, "_unit_path", lambda: unit_path)
    monkeypatch.setattr(service.subprocess, "run", run)

    result = service.reconcile_installed_unit()

    assert result.was_stale is False
    assert "invalid ExecStart" in capsys.readouterr().err
    run.assert_not_called()
