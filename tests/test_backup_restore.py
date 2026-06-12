# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from solstone.think.backup import restore
from solstone.think.backup.destination import Destination
from solstone.think.backup.runner import ResticResult


def _config_path(journal: Path) -> Path:
    return journal / "config" / "journal.json"


def _write_config(journal: Path, payload: dict[str, Any]) -> None:
    config_path = _config_path(journal)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload), encoding="utf-8")


def _read_config(journal: Path) -> dict[str, Any]:
    return json.loads(_config_path(journal).read_text(encoding="utf-8"))


def _destination() -> Destination:
    return Destination(
        repository="s3:safe-bucket/path",
        backend="s3",
        credentials={
            "access_key_id": "access-key",
            "secret_access_key": "secret-key",
        },
    )


def _result(returncode: int, parsed_json: Any | None = None) -> ResticResult:
    return ResticResult(
        returncode=returncode,
        stdout="",
        stderr="",
        json=parsed_json,
        argv=("restic",),
    )


def test_restore_success_normalizes_key_assembles_env_and_reindexes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {"backup": {"daily_key": "daily-secret"}})
    canonical = ("0" * 32) + ("1" * 32)
    entered = ("O" * 32) + ("I" * 32)
    destination = _destination()
    responses = iter(
        [
            _result(
                0,
                [{"paths": ["/old/journal"], "id": "snapshot-id"}],
            ),
            _result(
                0,
                {
                    "message_type": "summary",
                    "bytes_restored": 123,
                    "files_restored": 4,
                },
            ),
            _result(0),
        ]
    )
    order: list[str] = []
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        order.append(args[0])
        calls.append((args, kwargs))
        return next(responses)

    def fake_set_destination(value: Destination) -> None:
        order.append("set_destination")
        assert value == destination

    def fake_set_recovery_key(value: str) -> None:
        order.append("set_recovery_key")
        assert value == canonical

    def fake_set_recovery_key_confirmed(value: bool) -> None:
        order.append("set_recovery_key_confirmed")
        assert value is True

    def fake_scan_journal(journal: str, **kwargs: Any) -> bool:
        order.append("scan_journal")
        assert journal == str(tmp_path)
        assert kwargs == {"full": True}
        return True

    monkeypatch.setattr(restore, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(restore, "run_restic", fake_run_restic)
    monkeypatch.setattr(restore, "set_destination", fake_set_destination)
    monkeypatch.setattr(restore, "set_recovery_key", fake_set_recovery_key)
    monkeypatch.setattr(
        restore,
        "set_recovery_key_confirmed",
        fake_set_recovery_key_confirmed,
    )
    monkeypatch.setattr(restore, "get_backup_config", lambda: {"daily_key": "daily"})
    monkeypatch.setattr(restore, "scan_journal", fake_scan_journal)

    result = restore.restore_journal(destination, entered)

    assert result == restore.RestoreResult(
        status="ok",
        reason_code=None,
        integrity_ok=True,
        resumable=True,
        bytes_restored=123,
    )
    assert order == [
        "snapshots",
        "restore",
        "check",
        "set_destination",
        "set_recovery_key",
        "set_recovery_key_confirmed",
        "scan_journal",
    ]
    assert calls[0][0] == ["snapshots", "latest"]
    assert calls[0][1]["password"] == canonical
    assert calls[0][1]["repository"] == destination.repository
    assert "access-key" not in calls[0][1]["repository"]
    assert "secret-key" not in calls[0][1]["repository"]
    assert calls[0][1]["backend_env"] == {
        "AWS_ACCESS_KEY_ID": "access-key",
        "AWS_SECRET_ACCESS_KEY": "secret-key",
    }
    assert calls[0][1]["json"] is True
    assert calls[1][0] == [
        "restore",
        "latest:/old/journal",
        "--target",
        str(tmp_path),
    ]
    assert calls[1][1]["json"] is True
    assert calls[2][0] == ["check"]
    assert "json" not in calls[2][1]


def test_restore_wrong_key_returns_auth_failed_without_persisting_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    original_config = {"backup": {"daily_key": "daily-secret"}}
    _write_config(tmp_path, original_config)
    destination = _destination()
    recovery_key = "A" * 64

    monkeypatch.setattr(restore, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(
        restore,
        "run_restic",
        lambda args, **kwargs: _result(12),
    )
    monkeypatch.setattr(
        restore,
        "set_destination",
        lambda destination: pytest.fail("must not persist destination"),
    )
    monkeypatch.setattr(
        restore,
        "set_recovery_key",
        lambda key: pytest.fail("must not persist key"),
    )
    caplog.set_level(logging.WARNING, logger="solstone.backup.restore")

    result = restore.restore_journal(destination, recovery_key)

    assert result.reason_code == "auth_failed"
    assert _read_config(tmp_path) == original_config
    serialized = json.dumps(result.__dict__)
    for secret in (recovery_key, "access-key", "secret-key"):
        assert secret not in serialized
        assert secret not in caplog.text


def test_restore_invalid_key_persists_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    original_config = {"backup": {"daily_key": "daily-secret"}}
    _write_config(tmp_path, original_config)
    monkeypatch.setattr(
        restore,
        "ensure_restic",
        lambda: pytest.fail("restic should not be resolved"),
    )

    result = restore.restore_journal(_destination(), "too-short")

    assert result.reason_code == "invalid_key"
    assert _read_config(tmp_path) == original_config


def test_restore_timeout_reason_from_snapshots_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {"backup": {"daily_key": "daily-secret"}})
    monkeypatch.setattr(restore, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(restore, "run_restic", lambda args, **kwargs: _result(124))

    result = restore.restore_journal(_destination(), "A" * 64)

    assert result == restore.RestoreResult(
        status="error",
        reason_code="timeout",
        integrity_ok=False,
        resumable=False,
        bytes_restored=None,
    )


def test_restore_backend_invalid_returns_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {"backup": {"daily_key": "daily-secret"}})
    destination = _destination()
    del destination.credentials["secret_access_key"]
    monkeypatch.setattr(
        restore,
        "ensure_restic",
        lambda: pytest.fail("restic should not be resolved"),
    )

    result = restore.restore_journal(destination, "A" * 64)

    assert result.reason_code == "failed"


def test_restore_restic_unavailable_returns_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {"backup": {"daily_key": "daily-secret"}})
    monkeypatch.setattr(
        restore,
        "ensure_restic",
        lambda: (_ for _ in ()).throw(RuntimeError("missing")),
    )
    monkeypatch.setattr(
        restore,
        "run_restic",
        lambda *args, **kwargs: pytest.fail("restic should not run"),
    )

    result = restore.restore_journal(_destination(), "A" * 64)

    assert result.reason_code == "restic_unavailable"


def test_restore_malformed_snapshots_json_returns_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {"backup": {"daily_key": "daily-secret"}})
    monkeypatch.setattr(restore, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(
        restore,
        "run_restic",
        lambda args, **kwargs: _result(0, {"unexpected": True}),
    )

    result = restore.restore_journal(_destination(), "A" * 64)

    assert result.reason_code == "failed"


def test_restore_check_failure_is_nonfatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {"backup": {"daily_key": "daily-secret"}})
    responses = iter(
        [
            _result(0, [{"paths": ["/old/journal"]}]),
            _result(0, {"message_type": "summary", "bytes_restored": 5}),
            _result(11),
        ]
    )
    monkeypatch.setattr(restore, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(restore, "run_restic", lambda args, **kwargs: next(responses))
    monkeypatch.setattr(restore, "set_destination", lambda destination: None)
    monkeypatch.setattr(restore, "set_recovery_key", lambda key: None)
    monkeypatch.setattr(restore, "set_recovery_key_confirmed", lambda confirmed: None)
    monkeypatch.setattr(restore, "get_backup_config", lambda: {"daily_key": "daily"})
    monkeypatch.setattr(restore, "scan_journal", lambda journal, **kwargs: True)

    result = restore.restore_journal(_destination(), "A" * 64)

    assert result.status == "ok"
    assert result.reason_code is None
    assert result.integrity_ok is False


def test_restore_missing_daily_key_is_not_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {"backup": {}})
    responses = iter(
        [
            _result(0, [{"paths": ["/old/journal"]}]),
            _result(0, {"message_type": "summary", "bytes_restored": 5}),
            _result(0),
        ]
    )
    monkeypatch.setattr(restore, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(restore, "run_restic", lambda args, **kwargs: next(responses))
    monkeypatch.setattr(restore, "set_destination", lambda destination: None)
    monkeypatch.setattr(restore, "set_recovery_key", lambda key: None)
    monkeypatch.setattr(restore, "set_recovery_key_confirmed", lambda confirmed: None)
    monkeypatch.setattr(restore, "get_backup_config", lambda: {"daily_key": None})
    monkeypatch.setattr(restore, "scan_journal", lambda journal, **kwargs: True)

    result = restore.restore_journal(_destination(), "A" * 64)

    assert result.status == "ok"
    assert result.resumable is False
