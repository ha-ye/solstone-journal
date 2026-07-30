# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Callable

import pytest

from solstone.think.journal_io.errors import LockTimeout

mod = importlib.import_module(
    "solstone.apps.sol.maint.009_remove_granola_sync_schedule"
)


@pytest.fixture(autouse=True)
def _use_tmp_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def _write_schedules(journal: Path, data: object) -> Path:
    config_dir = journal / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    schedules_path = config_dir / "schedules.json"
    schedules_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return schedules_path


def _read_schedules(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _current_granola_entry(*, enabled: bool = True) -> dict:
    return {
        "cmd": ["journal", "importer", "--sync", "granola", "--save"],
        "every": "hourly",
        "enabled": enabled,
    }


def test_removes_enabled_current_granola_schedule(tmp_path):
    schedules_path = _write_schedules(
        tmp_path,
        {"sync:granola": _current_granola_entry(enabled=True)},
    )

    summary = mod.run_migration(dry_run=False)

    assert summary.removed == 1
    assert summary.removed_names == ["sync:granola"]
    assert summary.errors == 0
    assert "sync:granola" not in _read_schedules(schedules_path)


def test_removes_disabled_current_granola_schedule(tmp_path):
    schedules_path = _write_schedules(
        tmp_path,
        {"sync:granola": _current_granola_entry(enabled=False)},
    )

    summary = mod.run_migration(dry_run=False)

    assert summary.removed == 1
    assert summary.removed_names == ["sync:granola"]
    assert summary.errors == 0
    assert "sync:granola" not in _read_schedules(schedules_path)


def test_removes_only_granola_and_preserves_sibling_values_and_order(tmp_path):
    sync_plaud = {
        "cmd": ["journal", "importer", "--sync", "plaud", "--save"],
        "every": "hourly",
        "enabled": True,
    }
    sync_obsidian = {
        "cmd": ["journal", "importer", "--sync", "obsidian", "--save"],
        "every": "hourly",
        "enabled": False,
    }
    heartbeat = {
        "cmd": ["journal", "heartbeat"],
        "every": "daily",
        "enabled": True,
        "max_runtime": "10m",
    }
    weekly_agents = {
        "cmd": ["journal", "think", "--weekly", "-v"],
        "every": "weekly",
        "enabled": True,
        "max_runtime": "30m",
    }
    initial = {
        "sync:plaud": sync_plaud,
        "sync:granola": _current_granola_entry(enabled=True),
        "sync:obsidian": sync_obsidian,
        "heartbeat": heartbeat,
        "weekly-agents": weekly_agents,
        "daily_time": "03:17",
        "weekly_time": "04:21",
    }
    schedules_path = _write_schedules(tmp_path, initial)
    expected = {
        name: value for name, value in initial.items() if name != "sync:granola"
    }

    summary = mod.run_migration(dry_run=False)
    data = _read_schedules(schedules_path)

    assert summary.removed == 1
    assert summary.errors == 0
    assert data == expected
    assert list(data) == list(expected)


def test_removes_pre_007_sol_import_granola_schedule(tmp_path):
    schedules_path = _write_schedules(
        tmp_path,
        {
            "sync:granola": {
                "cmd": ["sol", "import", "--sync", "granola", "--save"],
                "every": "hourly",
                "enabled": True,
            }
        },
    )

    summary = mod.run_migration(dry_run=False)

    assert summary.removed == 1
    assert summary.removed_names == ["sync:granola"]
    assert summary.errors == 0
    assert "sync:granola" not in _read_schedules(schedules_path)


def test_removes_equals_form_granola_schedule(tmp_path):
    schedules_path = _write_schedules(
        tmp_path,
        {
            "sync:granola": {
                "cmd": ["journal", "importer", "--sync=granola", "--save"],
                "every": "hourly",
                "enabled": True,
            }
        },
    )

    summary = mod.run_migration(dry_run=False)

    assert summary.removed == 1
    assert summary.removed_names == ["sync:granola"]
    assert summary.errors == 0
    assert "sync:granola" not in _read_schedules(schedules_path)


def test_removes_granola_entry_without_cmd(tmp_path):
    schedules_path = _write_schedules(
        tmp_path,
        {"sync:granola": {"every": "hourly", "enabled": True}},
    )

    summary = mod.run_migration(dry_run=False)

    assert summary.removed == 1
    assert summary.removed_names == ["sync:granola"]
    assert summary.errors == 0
    assert "sync:granola" not in _read_schedules(schedules_path)


def test_preserves_non_dict_granola_entry_and_prints_warning(tmp_path, capsys):
    initial = {"sync:granola": "journal importer --sync granola"}
    schedules_path = _write_schedules(tmp_path, initial)

    summary = mod.run_migration(dry_run=False)
    captured = capsys.readouterr()

    assert summary.removed == 0
    assert summary.preserved == 1
    assert summary.preserved_names == ["sync:granola"]
    assert summary.errors == 0
    assert _read_schedules(schedules_path) == initial
    assert "WARNING" in captured.out
    assert "sync:granola" in captured.out


def test_preserves_other_backend_granola_key_and_prints_warning(tmp_path, capsys):
    legacy_entry = {
        "cmd": ["journal", "importer", "--sync", "plaud", "--save"],
        "every": "hourly",
        "enabled": True,
    }
    schedules_path = _write_schedules(tmp_path, {"sync:granola": legacy_entry})

    summary = mod.run_migration(dry_run=False)
    captured = capsys.readouterr()

    assert summary.removed == 0
    assert summary.preserved == 1
    assert summary.preserved_names == ["sync:granola"]
    assert summary.errors == 0
    assert _read_schedules(schedules_path) == {"sync:granola": legacy_entry}
    assert "WARNING" in captured.out
    assert "sync:granola" in captured.out


def test_dry_run_reports_would_remove_and_preserves_file_bytes_and_mtime(tmp_path):
    schedules_path = _write_schedules(
        tmp_path,
        {"sync:granola": _current_granola_entry(enabled=True)},
    )
    before_bytes = schedules_path.read_bytes()
    before_mtime_ns = schedules_path.stat().st_mtime_ns

    summary = mod.run_migration(dry_run=True)

    assert summary.removed == 1
    assert summary.removed_names == ["sync:granola"]
    assert summary.errors == 0
    assert schedules_path.read_bytes() == before_bytes
    assert schedules_path.stat().st_mtime_ns == before_mtime_ns


def test_second_run_is_noop_and_does_not_rewrite_schedules_file(tmp_path):
    schedules_path = _write_schedules(
        tmp_path,
        {"sync:granola": _current_granola_entry(enabled=True)},
    )

    first = mod.run_migration(dry_run=False)
    after_first_bytes = schedules_path.read_bytes()
    after_first_mtime_ns = schedules_path.stat().st_mtime_ns
    second = mod.run_migration(dry_run=False)

    assert first.removed == 1
    assert first.errors == 0
    assert second.removed == 0
    assert second.removed_names == []
    assert second.errors == 0
    assert schedules_path.read_bytes() == after_first_bytes
    assert schedules_path.stat().st_mtime_ns == after_first_mtime_ns


@pytest.mark.parametrize(
    ("payload", "expected_skipped_reason"),
    [
        pytest.param(None, "no file", id="missing-file"),
        pytest.param(b"", "empty file", id="empty-file"),
        pytest.param(b"{not json", "unparseable", id="non-json"),
        pytest.param(b"[]", "unparseable", id="non-dict"),
    ],
)
def test_read_side_bad_or_missing_schedules_skip_without_errors_and_cli_exits_zero(
    tmp_path,
    monkeypatch,
    payload,
    expected_skipped_reason,
):
    if payload is not None:
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "schedules.json").write_bytes(payload)

    summary = mod.run_migration(dry_run=False)

    assert summary.skipped_reason == expected_skipped_reason
    assert summary.errors == 0

    monkeypatch.setattr(sys, "argv", ["maint-009"])
    mod.main()


@pytest.mark.parametrize(
    "exc_factory",
    [
        pytest.param(lambda _path: OSError("boom"), id="oserror"),
        pytest.param(lambda path: LockTimeout(path, 1.0), id="lock-timeout"),
    ],
)
def test_remove_failure_records_error_preserves_bytes_and_cli_exits_one(
    tmp_path,
    monkeypatch,
    exc_factory: Callable[[Path], Exception],
):
    schedules_path = _write_schedules(
        tmp_path,
        {"sync:granola": _current_granola_entry(enabled=True)},
    )
    before_bytes = schedules_path.read_bytes()

    def _raise_remove(_name: str) -> None:
        raise exc_factory(schedules_path)

    monkeypatch.setattr(mod, "remove_schedule_entry", _raise_remove)

    summary = mod.run_migration(dry_run=False)

    assert summary.removed == 1
    assert summary.removed_names == ["sync:granola"]
    assert summary.errors == 1
    assert schedules_path.read_bytes() == before_bytes

    monkeypatch.setattr(sys, "argv", ["maint-009"])
    with pytest.raises(SystemExit) as exc_info:
        mod.main()

    assert exc_info.value.code == 1
    assert schedules_path.read_bytes() == before_bytes


def test_task_opt_ins_are_retry_only():
    assert mod.MAINT_RETRY_ON_NEXT_START is True
    assert getattr(mod, "MAINT_BLOCKS_SUPERVISOR_START", False) is False


def test_health_scheduler_state_is_untouched(tmp_path):
    _write_schedules(
        tmp_path,
        {"sync:granola": _current_granola_entry(enabled=True)},
    )
    health_dir = tmp_path / "health"
    health_dir.mkdir(parents=True, exist_ok=True)
    state_path = health_dir / "scheduler.json"
    state_path.write_text(
        json.dumps({"sync:granola": {"last_run": 123}}, indent=2),
        encoding="utf-8",
    )
    before_bytes = state_path.read_bytes()

    summary = mod.run_migration(dry_run=False)

    assert summary.removed == 1
    assert summary.errors == 0
    assert state_path.read_bytes() == before_bytes


def test_only_exact_sync_granola_key_is_considered_and_other_backends_are_preserved(
    tmp_path,
):
    assert (
        mod._is_retired_granola_sync(
            {"cmd": ["journal", "importer", "--sync", "plaud", "--save"]}
        )
        is False
    )
    granola_looking = {
        "cmd": ["journal", "importer", "--sync", "granola", "--save"],
        "every": "hourly",
        "enabled": True,
    }
    initial = {
        "sync:plaud": granola_looking,
        "sync:granola-copy": granola_looking,
    }
    schedules_path = _write_schedules(tmp_path, initial)
    before_bytes = schedules_path.read_bytes()

    summary = mod.run_migration(dry_run=False)

    assert summary.removed == 0
    assert summary.preserved == 0
    assert summary.errors == 0
    assert schedules_path.read_bytes() == before_bytes
    assert _read_schedules(schedules_path) == initial
