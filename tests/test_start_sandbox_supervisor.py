# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.start_sandbox_supervisor as sandbox_supervisor

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "start_sandbox_supervisor.py"


class StubProcess:
    pid = 4242


def _set_sandbox_env(monkeypatch, journal: Path) -> tuple[Path, str, str]:
    service_log = journal / "health" / "service.log"
    sandbox_path = "/tmp/solstone-venv/bin:/usr/bin"
    journal_bin = "/tmp/solstone-venv/bin/journal"
    monkeypatch.setenv("SANDBOX_LOG", str(service_log))
    monkeypatch.setenv("SANDBOX_PATH", sandbox_path)
    monkeypatch.setenv("JOURNAL_BIN", journal_bin)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return service_log, sandbox_path, journal_bin


def test_sandbox_launch_redirects_to_service_log_not_supervisor_log(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    journal.mkdir()
    service_log, sandbox_path, journal_bin = _set_sandbox_env(monkeypatch, journal)
    health_dir = journal / "health"
    assert not health_dir.exists()
    records = []

    def recorder(argv, **kwargs):
        records.append(
            {
                "argv": argv,
                "kwargs": kwargs,
                "health_dir_exists": health_dir.is_dir(),
                "stdout_name": kwargs["stdout"].name,
                "stdout_open": not kwargs["stdout"].closed,
            }
        )
        return StubProcess()

    pid = sandbox_supervisor.start_sandbox_supervisor(launcher=recorder)

    assert pid == StubProcess.pid
    assert len(records) == 1
    record = records[0]
    assert record["health_dir_exists"] is True
    assert record["argv"] == [journal_bin, "supervisor", "0", "--no-daily"]
    kwargs = record["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT
    assert Path(record["stdout_name"]) == service_log
    assert "supervisor.log" not in record["stdout_name"]
    assert record["stdout_open"] is True
    assert kwargs["env"]["PATH"] == sandbox_path
    assert kwargs["env"]["SOLSTONE_JOURNAL"] == str(journal)
    assert kwargs["start_new_session"] is True

    records.clear()
    monkeypatch.setattr(sandbox_supervisor.subprocess, "Popen", recorder)
    assert sandbox_supervisor.main() == 0
    assert capsys.readouterr().out == f"{StubProcess.pid}\n"
    assert len(records) == 1


def test_sandbox_launch_propagates_launcher_error_without_pid(
    tmp_path, monkeypatch, capsys
):
    journal = tmp_path / "journal"
    journal.mkdir()
    _set_sandbox_env(monkeypatch, journal)

    def fail_launch(_argv, **_kwargs):
        raise OSError("launch failed")

    monkeypatch.setattr(sandbox_supervisor.subprocess, "Popen", fail_launch)

    with pytest.raises(OSError, match="launch failed"):
        sandbox_supervisor.main()

    assert capsys.readouterr().out == ""


def test_sandbox_launch_subprocess_failure_reports_missing_binary(tmp_path):
    journal = tmp_path / "journal"
    journal.mkdir()
    service_log = journal / "health" / "service.log"
    missing_bin = tmp_path / "missing-journal"
    env = os.environ.copy()
    env.update(
        {
            "SANDBOX_LOG": str(service_log),
            "SANDBOX_PATH": "/usr/bin",
            "JOURNAL_BIN": str(missing_bin),
        }
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert str(missing_bin) in result.stderr
    assert "FileNotFoundError" in result.stderr


def test_sandbox_launch_missing_required_env_names_variable(monkeypatch):
    monkeypatch.delenv("SANDBOX_LOG", raising=False)

    with pytest.raises(RuntimeError, match="SANDBOX_LOG"):
        sandbox_supervisor.start_sandbox_supervisor(launcher=lambda: StubProcess())
