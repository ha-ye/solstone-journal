# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from unittest.mock import Mock

from solstone.apps.backup import maintenance
from solstone.think.backup import engine
from solstone.think.maintenance import (
    discover_routines,
    expected_schedule_entry,
)


def test_backup_routines_are_discovered_with_expected_schedule_entries() -> None:
    routines = discover_routines()

    assert "backup:run" in routines
    assert "backup:prune" in routines
    assert routines["backup:run"].every == "hourly"
    assert routines["backup:run"].max_runtime == "7h"
    assert routines["backup:prune"].every == "daily"
    assert routines["backup:prune"].max_runtime == "3h"
    assert expected_schedule_entry("backup:run", routines["backup:run"]) == {
        "cmd": ["journal", "maintenance", "run", "backup:run"],
        "every": "hourly",
        "enabled": True,
        "max_runtime": "7h",
    }
    assert expected_schedule_entry("backup:prune", routines["backup:prune"]) == {
        "cmd": ["journal", "maintenance", "run", "backup:prune"],
        "every": "daily",
        "enabled": True,
        "max_runtime": "3h",
    }


def test_backup_routine_wrappers_require_solstone_parse_empty_args_and_return_zero(
    monkeypatch,
    capsys,
) -> None:
    require_solstone = Mock()
    run_backup = Mock(
        return_value=engine.BackupResult(
            status="ok",
            snapshot_id="snap-1",
            error_reason=None,
        )
    )
    run_prune = Mock(
        return_value=engine.PruneResult(
            status="error",
            error_reason="locked",
        )
    )
    monkeypatch.setattr(maintenance, "require_solstone", require_solstone)
    monkeypatch.setattr(maintenance, "run_backup", run_backup)
    monkeypatch.setattr(maintenance, "run_prune", run_prune)

    backup_code = maintenance.run_backup_routine([])
    prune_code = maintenance.run_prune_routine([])

    assert backup_code == 0
    assert prune_code == 0
    assert require_solstone.call_count == 2
    run_backup.assert_called_once_with()
    run_prune.assert_called_once_with()
    output = capsys.readouterr().out
    assert "backup: ok snapshot_id=snap-1" in output
    assert "backup prune: error reason=locked" in output


def test_request_backup_now_sends_supervisor_request_without_ref(monkeypatch) -> None:
    callosum_send = Mock(return_value=True)
    monkeypatch.setattr(engine, "callosum_send", callosum_send)

    assert engine.request_backup_now() is True

    callosum_send.assert_called_once_with(
        "supervisor",
        "request",
        cmd=["journal", "maintenance", "run", "backup:run"],
    )
    assert "ref" not in callosum_send.call_args.kwargs
