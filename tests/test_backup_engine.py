# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from solstone.think.backup import engine
from solstone.think.backup.hosted import (
    HostedBinding,
    HostedCredentials,
    HostedCredsUnavailable,
    load_hosted_binding,
    save_hosted_binding,
)
from solstone.think.backup.runner import ResticResult


def _config_path(journal: Path) -> Path:
    return journal / "config" / "journal.json"


def _write_config(journal: Path, payload: dict[str, Any]) -> None:
    config_path = _config_path(journal)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload), encoding="utf-8")


def _read_config(journal: Path) -> dict[str, Any]:
    return json.loads(_config_path(journal).read_text(encoding="utf-8"))


def _valid_backup_config(
    *,
    daily_key: str = "daily-secret",
    access_key_id: str = "access-key",
    secret_access_key: str = "secret-key",
    retention: dict[str, int] | None = None,
) -> dict[str, Any]:
    backup: dict[str, Any] = {
        "enabled": True,
        "destination": {
            "repository": "s3:safe-bucket/path",
            "backend": "s3",
            "credentials": {
                "access_key_id": access_key_id,
                "secret_access_key": secret_access_key,
            },
        },
        "daily_key": daily_key,
        "recovery_key": "R" * 64,
    }
    if retention is not None:
        backup["retention"] = retention
    return {"backup": backup}


def _restic_result(
    returncode: int,
    *,
    parsed_json: Any | None = None,
    args: list[str] | None = None,
    text: str = "",
) -> ResticResult:
    return ResticResult(
        returncode=returncode,
        stdout=text,
        stderr=text,
        json=parsed_json,
        argv=("restic", *(args or [])),
    )


@pytest.mark.parametrize(
    "backup_config",
    [
        {"enabled": False},
        {
            "enabled": True,
            "daily_key": "daily-secret",
            "recovery_key": "R" * 64,
        },
        {
            "enabled": True,
            "destination": {
                "repository": "s3:safe-bucket/path",
                "backend": "s3",
                "credentials": {
                    "access_key_id": "access-key",
                    "secret_access_key": "secret-key",
                },
            },
        },
    ],
)
def test_run_backup_skips_when_runtime_guard_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backup_config: dict[str, Any],
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {"backup": backup_config})
    ensure_restic = Mock()
    run_restic = Mock()
    record_backup_result = Mock()
    monkeypatch.setattr(engine, "ensure_restic", ensure_restic)
    monkeypatch.setattr(engine, "run_restic", run_restic)
    monkeypatch.setattr(engine, "record_backup_result", record_backup_result)

    result = engine.run_backup()

    assert result == engine.BackupResult(
        status="skipped",
        snapshot_id=None,
        error_reason=None,
    )
    ensure_restic.assert_not_called()
    run_restic.assert_not_called()
    record_backup_result.assert_not_called()


@pytest.mark.parametrize(
    "backup_config",
    [
        {"enabled": False},
        {
            "enabled": True,
            "daily_key": "daily-secret",
            "recovery_key": "R" * 64,
        },
        {
            "enabled": True,
            "destination": {
                "repository": "s3:safe-bucket/path",
                "backend": "s3",
                "credentials": {
                    "access_key_id": "access-key",
                    "secret_access_key": "secret-key",
                },
            },
        },
    ],
)
def test_run_prune_skips_when_runtime_guard_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backup_config: dict[str, Any],
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {"backup": backup_config})
    ensure_restic = Mock()
    run_restic = Mock()
    record_backup_result = Mock()
    record_prune_result = Mock()
    monkeypatch.setattr(engine, "ensure_restic", ensure_restic)
    monkeypatch.setattr(engine, "run_restic", run_restic)
    monkeypatch.setattr(engine, "record_backup_result", record_backup_result)
    monkeypatch.setattr(engine, "record_prune_result", record_prune_result)

    result = engine.run_prune()

    assert result == engine.PruneResult(status="skipped", error_reason=None)
    ensure_restic.assert_not_called()
    run_restic.assert_not_called()
    record_backup_result.assert_not_called()
    record_prune_result.assert_not_called()


def test_run_backup_unlocks_then_calls_restic_with_expected_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _valid_backup_config())
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        calls.append((args, kwargs))
        if args == ["unlock"]:
            return _restic_result(0, args=args)
        return _restic_result(
            0,
            parsed_json={"message_type": "summary", "snapshot_id": "snap-1"},
            args=args,
        )

    record_backup_result = Mock()
    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(engine, "run_restic", fake_run_restic)
    monkeypatch.setattr(engine, "record_backup_result", record_backup_result)
    monkeypatch.setattr(engine.time, "time", lambda: 1000.9)

    result = engine.run_backup()

    assert result == engine.BackupResult(
        status="ok",
        snapshot_id="snap-1",
        error_reason=None,
    )
    assert calls[0] == (
        ["unlock"],
        {
            "repository": "s3:safe-bucket/path",
            "password": "daily-secret",
            "restic_path": Path("/restic"),
            "backend_env": {
                "AWS_ACCESS_KEY_ID": "access-key",
                "AWS_SECRET_ACCESS_KEY": "secret-key",
            },
            "timeout": engine.UNLOCK_TIMEOUT_SECONDS,
        },
    )
    assert calls[1] == (
        [
            "backup",
            str(tmp_path),
            "--exclude",
            "*.sqlite*",
            "--exclude",
            "indexer",
            "--exclude",
            "cache",
            "--exclude",
            ".cache",
            "--exclude",
            "*.sock",
            "--exclude",
            "*.pid",
            "--exclude",
            "*.port",
            "--exclude",
            "*.lock",
            "--exclude",
            "*.tmp",
            "--exclude",
            ".tmp*",
            "--exclude",
            "supervisor.ready",
            "--exclude",
            "supervisor.start_time",
            "--exclude",
            "scheduler.json",
            "--exclude",
            "talents.json",
            "--exclude",
            "agents.json",
        ],
        {
            "repository": "s3:safe-bucket/path",
            "password": "daily-secret",
            "restic_path": Path("/restic"),
            "backend_env": {
                "AWS_ACCESS_KEY_ID": "access-key",
                "AWS_SECRET_ACCESS_KEY": "secret-key",
            },
            "json": True,
            "timeout": engine.BACKUP_TIMEOUT_SECONDS,
        },
    )
    record_backup_result.assert_called_once_with(
        status="ok",
        time=1000,
        snapshot_id="snap-1",
        error_reason=None,
    )


@pytest.mark.parametrize(
    "parsed_json",
    [
        {"message_type": "summary", "snapshot_id": "snap-dict"},
        [
            {"message_type": "status", "percent_done": 50},
            {"message_type": "summary", "snapshot_id": "snap-list"},
        ],
    ],
)
def test_run_backup_selects_summary_from_dict_or_list_and_records_ok(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parsed_json: Any,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _valid_backup_config())
    expected_snapshot_id = (
        parsed_json["snapshot_id"]
        if isinstance(parsed_json, dict)
        else parsed_json[-1]["snapshot_id"]
    )

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        if args == ["unlock"]:
            return _restic_result(0, args=args)
        return _restic_result(0, parsed_json=parsed_json, args=args)

    record_backup_result = Mock()
    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(engine, "run_restic", fake_run_restic)
    monkeypatch.setattr(engine, "record_backup_result", record_backup_result)
    monkeypatch.setattr(engine.time, "time", lambda: 123)

    result = engine.run_backup()

    assert result == engine.BackupResult(
        status="ok",
        snapshot_id=expected_snapshot_id,
        error_reason=None,
    )
    record_backup_result.assert_called_once_with(
        status="ok",
        time=123,
        snapshot_id=expected_snapshot_id,
        error_reason=None,
    )


@pytest.mark.parametrize(
    ("returncode", "parsed_json", "expected_reason", "expected_snapshot_id"),
    [
        (
            3,
            {"message_type": "summary", "snapshot_id": "partial-snapshot"},
            "incomplete",
            "partial-snapshot",
        ),
        (10, None, "repo_missing", None),
        (11, None, "locked", None),
        (12, None, "auth_failed", None),
        (124, None, "timeout", None),
        (77, None, "failed", None),
        (0, None, "unknown", None),
    ],
)
def test_run_backup_failure_paths_record_sanitized_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    parsed_json: Any | None,
    expected_reason: str,
    expected_snapshot_id: str | None,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _valid_backup_config())

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        if args == ["unlock"]:
            return _restic_result(0, args=args)
        return _restic_result(
            returncode,
            parsed_json=parsed_json,
            args=args,
            text="raw-secret-output",
        )

    record_backup_result = Mock()
    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(engine, "run_restic", fake_run_restic)
    monkeypatch.setattr(engine, "record_backup_result", record_backup_result)
    monkeypatch.setattr(engine.time, "time", lambda: 456)

    result = engine.run_backup()

    assert result == engine.BackupResult(
        status="error",
        snapshot_id=expected_snapshot_id,
        error_reason=expected_reason,
    )
    record_backup_result.assert_called_once_with(
        status="error",
        time=456,
        snapshot_id=expected_snapshot_id,
        error_reason=expected_reason,
    )
    assert "raw-secret-output" != result.error_reason


def test_run_backup_restic_unavailable_records_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _valid_backup_config())
    run_restic = Mock()
    record_backup_result = Mock()
    monkeypatch.setattr(
        engine,
        "ensure_restic",
        Mock(side_effect=RuntimeError("download failed")),
    )
    monkeypatch.setattr(engine, "run_restic", run_restic)
    monkeypatch.setattr(engine, "record_backup_result", record_backup_result)
    monkeypatch.setattr(engine.time, "time", lambda: 789)

    result = engine.run_backup()

    assert result == engine.BackupResult(
        status="error",
        snapshot_id=None,
        error_reason="restic_unavailable",
    )
    run_restic.assert_not_called()
    record_backup_result.assert_called_once_with(
        status="error",
        time=789,
        snapshot_id=None,
        error_reason="restic_unavailable",
    )


def test_run_prune_unlocks_then_forgets_with_prune_and_repack_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(
        tmp_path,
        _valid_backup_config(
            retention={"hourly": 2, "daily": 3, "weekly": 4, "monthly": 5}
        ),
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        calls.append((args, kwargs))
        return _restic_result(0, args=args)

    record_prune_result = Mock()
    record_backup_result = Mock()
    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(engine, "run_restic", fake_run_restic)
    monkeypatch.setattr(engine, "record_prune_result", record_prune_result)
    monkeypatch.setattr(engine, "record_backup_result", record_backup_result)
    monkeypatch.setattr(engine.time, "time", lambda: 2000.9)

    result = engine.run_prune()

    assert result == engine.PruneResult(status="ok", error_reason=None)
    assert calls[0][0] == ["unlock"]
    assert calls[0][1]["timeout"] == engine.UNLOCK_TIMEOUT_SECONDS
    assert calls[1] == (
        [
            "forget",
            "--keep-hourly",
            "2",
            "--keep-daily",
            "3",
            "--keep-weekly",
            "4",
            "--keep-monthly",
            "5",
            "--prune",
        ],
        {
            "repository": "s3:safe-bucket/path",
            "password": "daily-secret",
            "restic_path": Path("/restic"),
            "backend_env": {
                "AWS_ACCESS_KEY_ID": "access-key",
                "AWS_SECRET_ACCESS_KEY": "secret-key",
            },
            "timeout": engine.PRUNE_TIMEOUT_SECONDS,
            "max_repack_size": engine.PRUNE_MAX_REPACK_SIZE,
        },
    )
    assert "json" not in calls[1][1]
    record_prune_result.assert_called_once_with(
        status="ok",
        time=2000,
        error_reason=None,
    )
    record_backup_result.assert_not_called()


def test_run_prune_failure_records_last_prune_not_last_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _valid_backup_config())

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        if args == ["unlock"]:
            return _restic_result(0, args=args)
        return _restic_result(11, args=args, text="raw-secret-output")

    record_prune_result = Mock()
    record_backup_result = Mock()
    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(engine, "run_restic", fake_run_restic)
    monkeypatch.setattr(engine, "record_prune_result", record_prune_result)
    monkeypatch.setattr(engine, "record_backup_result", record_backup_result)
    monkeypatch.setattr(engine.time, "time", lambda: 3000)

    result = engine.run_prune()

    assert result == engine.PruneResult(status="error", error_reason="locked")
    record_prune_result.assert_called_once_with(
        status="error",
        time=3000,
        error_reason="locked",
    )
    record_backup_result.assert_not_called()


def test_run_prune_restic_unavailable_records_last_prune(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _valid_backup_config())
    run_restic = Mock()
    record_prune_result = Mock()
    monkeypatch.setattr(
        engine,
        "ensure_restic",
        Mock(side_effect=RuntimeError("download failed")),
    )
    monkeypatch.setattr(engine, "run_restic", run_restic)
    monkeypatch.setattr(engine, "record_prune_result", record_prune_result)
    monkeypatch.setattr(engine.time, "time", lambda: 4000)

    result = engine.run_prune()

    assert result == engine.PruneResult(
        status="error",
        error_reason="restic_unavailable",
    )
    run_restic.assert_not_called()
    record_prune_result.assert_called_once_with(
        status="error",
        time=4000,
        error_reason="restic_unavailable",
    )


def test_malformed_backend_env_records_failed_without_raw_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config = _valid_backup_config()
    del config["backup"]["destination"]["credentials"]["secret_access_key"]
    _write_config(tmp_path, config)
    run_restic = Mock()
    record_backup_result = Mock()
    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(engine, "run_restic", run_restic)
    monkeypatch.setattr(engine, "record_backup_result", record_backup_result)
    monkeypatch.setattr(engine.time, "time", lambda: 5000)

    result = engine.run_backup()

    assert result == engine.BackupResult(
        status="error",
        snapshot_id=None,
        error_reason="failed",
    )
    run_restic.assert_not_called()
    record_backup_result.assert_called_once_with(
        status="error",
        time=5000,
        snapshot_id=None,
        error_reason="failed",
    )


def test_backup_and_prune_failures_do_not_persist_or_log_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    daily_key = "SECRET-DAILY"
    access_key_id = "SECRET-ACCESS"
    secret_access_key = "SECRET-BACKEND"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(
        tmp_path,
        _valid_backup_config(
            daily_key=daily_key,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
        ),
    )

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        if args == ["unlock"]:
            return _restic_result(0, args=args)
        return _restic_result(
            12,
            args=args,
            text=f"{daily_key} {access_key_id} {secret_access_key}",
        )

    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(engine, "run_restic", fake_run_restic)
    monkeypatch.setattr(engine.time, "time", lambda: 6000)
    caplog.set_level(logging.WARNING, logger="solstone.backup.engine")

    backup_result = engine.run_backup()
    prune_result = engine.run_prune()

    config = _read_config(tmp_path)
    serialized_results = json.dumps(
        {
            "last_backup": config["backup"]["last_backup"],
            "last_prune": config["backup"]["last_prune"],
        }
    )
    for secret in (daily_key, access_key_id, secret_access_key):
        assert secret not in serialized_results
        assert secret not in caplog.text
    assert backup_result.error_reason == "auth_failed"
    assert prune_result.error_reason == "auth_failed"


def test_operated_backup_fetches_creds_and_builds_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(
        tmp_path,
        {
            "backup": {
                "enabled": True,
                "mode": "operated",
                "daily_key": "dk",
                "recovery_key": "R" * 64,
            }
        },
    )
    save_hosted_binding(
        HostedBinding(
            broker_endpoint="https://broker.example",
            account_id="acct",
            instance_id="inst",
            bucket="bkt",
            prefix="users/acct/inst",
            broker_token="BTOKEN",
        )
    )
    captured: dict[str, str] = {}
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_fetch(
        _binding: HostedBinding,
        *,
        scope: str,
    ) -> HostedCredentials:
        captured["scope"] = scope
        return HostedCredentials(
            access_key_id="AKID",
            secret_access_key="SAK",
            session_token="SESS",
            endpoint="https://acct.r2.cloudflarestorage.com",
        )

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        calls.append((args, kwargs))
        if args == ["unlock"]:
            return _restic_result(0, args=args)
        return _restic_result(
            0,
            parsed_json={"message_type": "summary", "snapshot_id": "snap1"},
            args=args,
        )

    monkeypatch.setattr(engine, "fetch_hosted_credentials", fake_fetch)
    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(engine, "run_restic", fake_run_restic)

    result = engine.run_backup()

    assert result.status == "ok"
    backup_call = next(call for call in calls if call[0][0] == "backup")
    backup_kwargs = backup_call[1]
    assert backup_kwargs["backend_env"] == {
        "AWS_ACCESS_KEY_ID": "AKID",
        "AWS_SECRET_ACCESS_KEY": "SAK",
        "AWS_SESSION_TOKEN": "SESS",
    }
    assert (
        backup_kwargs["repository"]
        == "s3:https://acct.r2.cloudflarestorage.com/bkt/users/acct/inst"
    )
    for secret in ("AKID", "SAK", "SESS"):
        assert secret not in backup_kwargs["repository"]
    assert captured["scope"] == "backup"


def test_operated_prune_requests_maintenance_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(
        tmp_path,
        {
            "backup": {
                "enabled": True,
                "mode": "operated",
                "daily_key": "dk",
                "recovery_key": "R" * 64,
            }
        },
    )
    save_hosted_binding(
        HostedBinding(
            broker_endpoint="https://broker.example",
            account_id="acct",
            instance_id="inst",
            bucket="bkt",
            prefix="users/acct/inst",
            broker_token="BTOKEN",
        )
    )
    captured: dict[str, str] = {}
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_fetch(
        _binding: HostedBinding,
        *,
        scope: str,
    ) -> HostedCredentials:
        captured["scope"] = scope
        return HostedCredentials(
            access_key_id="AKID",
            secret_access_key="SAK",
            session_token="SESS",
            endpoint="https://acct.r2.cloudflarestorage.com",
        )

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        calls.append((args, kwargs))
        return _restic_result(0, args=args)

    monkeypatch.setattr(engine, "fetch_hosted_credentials", fake_fetch)
    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(engine, "run_restic", fake_run_restic)

    result = engine.run_prune()

    assert result.status == "ok"
    forget_call = next(call for call in calls if call[0][0] == "forget")
    assert captured["scope"] == "maintenance"
    assert forget_call[1]["backend_env"]["AWS_SESSION_TOKEN"] == "SESS"


def _assert_operated_backup_degrades_on_hosted_credential_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(
        tmp_path,
        {
            "backup": {
                "enabled": True,
                "mode": "operated",
                "daily_key": "dk",
                "recovery_key": "R" * 64,
            }
        },
    )
    save_hosted_binding(
        HostedBinding(
            broker_endpoint="https://broker.example",
            account_id="acct",
            instance_id="inst",
            bucket="bkt",
            prefix="users/acct/inst",
            broker_token="BTOKEN",
        )
    )
    run_restic = Mock()
    record_backup_result = Mock()
    monkeypatch.setattr(
        engine,
        "fetch_hosted_credentials",
        Mock(side_effect=HostedCredsUnavailable(reason_code)),
    )
    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(engine, "run_restic", run_restic)
    monkeypatch.setattr(engine, "record_backup_result", record_backup_result)

    result = engine.run_backup()

    assert result.status == "error"
    assert result.error_reason == reason_code
    run_restic.assert_not_called()
    assert record_backup_result.call_args.kwargs["error_reason"] == reason_code


def test_operated_backup_degrades_on_entitlement_inactive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_operated_backup_degrades_on_hosted_credential_error(
        tmp_path,
        monkeypatch,
        "hosted_entitlement_inactive",
    )


def test_operated_backup_degrades_on_broker_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_operated_backup_degrades_on_hosted_credential_error(
        tmp_path,
        monkeypatch,
        "broker_unreachable",
    )


def test_operated_degrade_is_non_destructive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    byo_destination = {
        "repository": "s3:byo/path",
        "backend": "s3",
        "credentials": {"access_key_id": "a", "secret_access_key": "b"},
    }
    _write_config(
        tmp_path,
        {
            "backup": {
                "enabled": True,
                "mode": "operated",
                "daily_key": "dk",
                "recovery_key": "R" * 64,
                "destination": byo_destination,
            }
        },
    )
    save_hosted_binding(
        HostedBinding(
            broker_endpoint="https://broker.example",
            account_id="acct",
            instance_id="inst",
            bucket="bkt",
            prefix="users/acct/inst",
            broker_token="BTOKEN",
        )
    )
    monkeypatch.setattr(
        engine,
        "fetch_hosted_credentials",
        Mock(side_effect=HostedCredsUnavailable("broker_unreachable")),
    )

    result = engine.run_backup()

    backup = _read_config(tmp_path)["backup"]
    assert result.error_reason == "broker_unreachable"
    assert backup["daily_key"] == "dk"
    assert backup["recovery_key"] == "R" * 64
    assert backup["destination"] == byo_destination
    assert backup["last_backup"]["status"] == "error"
    assert load_hosted_binding() is not None


def test_operated_does_not_persist_or_log_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secrets = (
        "dk-secret",
        "AKID-SECRET",
        "SAK-SECRET",
        "SESS-SECRET",
        "BTOKEN-SECRET",
    )
    secret_text = " ".join(secrets)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(
        tmp_path,
        {
            "backup": {
                "enabled": True,
                "mode": "operated",
                "daily_key": "dk-secret",
                "recovery_key": "R" * 64,
            }
        },
    )
    save_hosted_binding(
        HostedBinding(
            broker_endpoint="https://broker.example",
            account_id="acct",
            instance_id="inst",
            bucket="bkt",
            prefix="users/acct/inst",
            broker_token="BTOKEN-SECRET",
        )
    )

    def fake_fetch(
        _binding: HostedBinding,
        *,
        scope: str,
    ) -> HostedCredentials:
        assert scope in {"backup", "maintenance"}
        return HostedCredentials(
            access_key_id="AKID-SECRET",
            secret_access_key="SAK-SECRET",
            session_token="SESS-SECRET",
            endpoint="https://acct.r2.cloudflarestorage.com",
        )

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        if args == ["unlock"]:
            return _restic_result(0, args=args, text=secret_text)
        if args and args[0] == "backup":
            return _restic_result(
                0,
                parsed_json={"message_type": "summary", "snapshot_id": "snap1"},
                args=args,
                text=secret_text,
            )
        return _restic_result(12, args=args, text=secret_text)

    monkeypatch.setattr(engine, "fetch_hosted_credentials", fake_fetch)
    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(engine, "run_restic", fake_run_restic)
    caplog.set_level(logging.WARNING, logger="solstone.backup.engine")

    engine.run_backup()
    engine.run_prune()

    config = _read_config(tmp_path)
    serialized = json.dumps(
        {
            "last_backup": config["backup"]["last_backup"],
            "last_prune": config["backup"]["last_prune"],
        }
    )
    for secret in secrets:
        assert secret not in serialized
        assert secret not in caplog.text
