# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from solstone.think import offload_restore
from solstone.think.backup import engine
from solstone.think.backup.hosted import (
    HostedBinding,
    HostedCredentials,
    save_hosted_binding,
)
from solstone.think.backup.runner import ResticResult
from solstone.think.offload_ledger import (
    OffloadFile,
    append_offload_event,
    append_restore_event,
)
from solstone.think.utils import DEFAULT_STREAM

DAY = "20260101"
SEGMENT = "090000_300"
CONTENT = b"audio-v1"
SHA = hashlib.sha256(CONTENT).hexdigest()


def _config_path(journal: Path) -> Path:
    return journal / "config" / "journal.json"


def _write_config(journal: Path, backup: dict[str, Any]) -> None:
    path = _config_path(journal)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"backup": backup}), encoding="utf-8")


def _backup_config(*, mode: str = "byo", enabled: bool = True) -> dict[str, Any]:
    backup: dict[str, Any] = {
        "enabled": enabled,
        "mode": mode,
        "daily_key": "daily-secret",
        "recovery_key": "R" * 64,
        "confirmed_recovery_key": True,
        "offload": {
            "enabled": True,
            "budget_bytes": 10_000_000_000,
            "floor_bytes": 1,
        },
    }
    if mode == "byo":
        backup["destination"] = {
            "repository": "s3:safe-bucket/path",
            "backend": "s3",
            "credentials": {
                "access_key_id": "access-key",
                "secret_access_key": "secret-key",
            },
        }
    return backup


def _segment_dir(journal: Path, *, stream: str = DEFAULT_STREAM) -> Path:
    if stream == DEFAULT_STREAM:
        path = journal / "chronicle" / DAY / SEGMENT
    else:
        path = journal / "chronicle" / DAY / stream / SEGMENT
    path.mkdir(parents=True, exist_ok=True)
    return path


def _segment_dir_for(
    journal: Path,
    day: str,
    segment: str,
    *,
    stream: str = DEFAULT_STREAM,
) -> Path:
    if stream == DEFAULT_STREAM:
        path = journal / "chronicle" / day / segment
    else:
        path = journal / "chronicle" / day / stream / segment
    path.mkdir(parents=True, exist_ok=True)
    return path


def _seed_ledger(stream: str = DEFAULT_STREAM, *, snapshot_id: str = "snap1") -> None:
    append_offload_event(
        day=DAY,
        stream=stream,
        segment=SEGMENT,
        snapshot_id=snapshot_id,
        files=[OffloadFile(name="audio.wav", bytes=len(CONTENT), sha256=SHA)],
        time=100,
    )


def _restic_result(returncode: int, args: list[str]) -> ResticResult:
    return ResticResult(
        returncode=returncode,
        stdout="",
        stderr="",
        json=None,
        argv=tuple(args),
    )


def _binding() -> HostedBinding:
    return HostedBinding(
        broker_endpoint="https://broker.example",
        account_id="acct",
        instance_id="inst",
        bucket="bkt",
        prefix="users/acct/inst",
        broker_token="BTOKEN",
    )


def _creds() -> HostedCredentials:
    return HostedCredentials(
        access_key_id="AKID",
        secret_access_key="SAK",
        session_token="SESS",
        endpoint="https://acct.r2.cloudflarestorage.com",
        expires_at="2026-07-13T12:00:00Z",
    )


def test_restore_day_uses_daily_key_default_layout_and_no_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _backup_config())
    segment_dir = _segment_dir(tmp_path)
    _seed_ledger()
    calls: list[tuple[list[str], dict[str, Any]]] = []
    fetch_hosted_credentials = Mock()
    callosum_send = Mock()

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        calls.append((args, kwargs))
        (segment_dir / "audio.wav").write_bytes(CONTENT)
        return _restic_result(0, args)

    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(engine, "fetch_hosted_credentials", fetch_hosted_credentials)
    monkeypatch.setattr(engine, "callosum_send", callosum_send)
    monkeypatch.setattr(offload_restore, "run_restic", fake_run_restic)
    monkeypatch.setattr(offload_restore, "device_free_bytes", lambda: 5_000_000_000)
    monkeypatch.setattr(offload_restore.time, "time", lambda: 200)

    result = offload_restore.restore_day(DAY)

    assert result.status == "ok"
    assert calls == [
        (
            [
                "restore",
                f"snap1:{segment_dir}",
                "--target",
                str(segment_dir),
                "--include",
                "/audio.wav",
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
                "timeout": offload_restore.OFFLOAD_RESTORE_TIMEOUT_SECONDS,
            },
        )
    ]
    assert "R" * 64 not in json.dumps(calls, default=str)
    fetch_hosted_credentials.assert_not_called()
    callosum_send.assert_not_called()
    assert json.loads(_config_path(tmp_path).read_text(encoding="utf-8"))["backup"][
        "last_restore"
    ] == {
        "time": 200,
        "status": "ok",
        "reason": None,
        "scope": "day",
        "day": DAY,
        "segments_selected": 1,
        "segments_restored": 1,
        "files_expected": 1,
        "files_restored": 1,
        "bytes_expected": len(CONTENT),
        "bytes_restored": len(CONTENT),
    }


def test_restore_day_operated_uses_backup_scope_and_append_only_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _backup_config(mode="operated"))
    save_hosted_binding(_binding())
    segment_dir = _segment_dir(tmp_path, stream="camera")
    _seed_ledger("camera")
    captured_scopes: list[str] = []
    calls: list[list[str]] = []

    def fake_fetch(
        _binding: HostedBinding,
        *,
        scope: str,
    ) -> HostedCredentials:
        captured_scopes.append(scope)
        return _creds()

    def fake_run_restic(args: list[str], **_kwargs: Any) -> ResticResult:
        calls.append(args)
        (segment_dir / "audio.wav").write_bytes(CONTENT)
        return _restic_result(0, args)

    monkeypatch.setattr(engine, "fetch_hosted_credentials", fake_fetch)
    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(engine, "ensure_rclone", Mock(return_value=Path("/rclone")))
    monkeypatch.setattr(offload_restore, "run_restic", fake_run_restic)
    monkeypatch.setattr(offload_restore, "device_free_bytes", lambda: 5_000_000_000)

    result = offload_restore.restore_day(DAY)

    assert result.status == "ok"
    assert captured_scopes == ["operated"]
    assert calls[0][:4] == [
        "-o",
        "rclone.program=/rclone",
        "-o",
        "rclone.args=serve restic --stdio --append-only --config /dev/null",
    ]
    assert calls[0][4:7] == ["restore", f"snap1:{segment_dir}", "--target"]


@pytest.mark.parametrize(
    ("returncode", "reason"),
    [
        (10, "repo_missing"),
        (12, "auth_failed"),
        (11, "locked"),
        (124, "timeout"),
        (77, "failed"),
    ],
)
def test_restore_day_maps_restic_returncode_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    reason: str,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _backup_config())
    _segment_dir(tmp_path)
    _seed_ledger()
    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(offload_restore, "device_free_bytes", lambda: 5_000_000_000)
    monkeypatch.setattr(
        offload_restore,
        "run_restic",
        lambda args, **_kwargs: _restic_result(returncode, args),
    )

    result = offload_restore.restore_day(DAY)

    assert result.status == "error"
    assert result.reason == reason


def test_missing_include_exit_zero_is_verified_as_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _backup_config())
    _segment_dir(tmp_path)
    _seed_ledger()
    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(offload_restore, "device_free_bytes", lambda: 5_000_000_000)
    monkeypatch.setattr(
        offload_restore,
        "run_restic",
        lambda args, **_kwargs: _restic_result(0, args),
    )

    result = offload_restore.restore_day(DAY)

    assert result.status == "error"
    assert result.reason == "missing_file_after_restore"


def test_verification_failure_rolls_back_recorded_attempted_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _backup_config())
    segment_dir = _segment_dir(tmp_path)
    _seed_ledger()

    def fake_run_restic(args: list[str], **_kwargs: Any) -> ResticResult:
        (segment_dir / "audio.wav").write_bytes(b"wrong")
        return _restic_result(0, args)

    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(offload_restore, "device_free_bytes", lambda: 5_000_000_000)
    monkeypatch.setattr(offload_restore, "run_restic", fake_run_restic)

    result = offload_restore.restore_day(DAY)

    assert result.status == "error"
    assert result.reason == "verification_failed"
    assert not (segment_dir / "audio.wav").exists()


def test_restore_all_degraded_after_partial_success_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _backup_config())
    first = _segment_dir(tmp_path)
    second_segment = "091000_300"
    second = tmp_path / "chronicle" / DAY / second_segment
    second.mkdir(parents=True)
    append_offload_event(
        day=DAY,
        stream=DEFAULT_STREAM,
        segment=SEGMENT,
        snapshot_id="snap1",
        files=[OffloadFile(name="audio.wav", bytes=len(CONTENT), sha256=SHA)],
        time=100,
    )
    append_offload_event(
        day=DAY,
        stream=DEFAULT_STREAM,
        segment=second_segment,
        snapshot_id="snap2",
        files=[OffloadFile(name="audio.wav", bytes=len(CONTENT), sha256=SHA)],
        time=101,
    )
    call_count = 0

    def fake_run_restic(args: list[str], **_kwargs: Any) -> ResticResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            (first / "audio.wav").write_bytes(CONTENT)
        else:
            (second / "audio.wav").write_bytes(b"wrong")
        return _restic_result(0, args)

    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(offload_restore, "device_free_bytes", lambda: 5_000_000_000)
    monkeypatch.setattr(offload_restore, "run_restic", fake_run_restic)

    result = offload_restore.restore_all()

    assert result.status == "degraded"
    assert result.reason == "verification_failed"
    assert result.segments_restored == 1
    assert call_count == 2
    assert (first / "audio.wav").exists()
    assert not (second / "audio.wav").exists()


def test_restore_all_runs_oldest_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _backup_config())
    newer_day = "20260102"
    older_day = "20260101"
    newer_dir = _segment_dir_for(tmp_path, newer_day, SEGMENT)
    older_dir = _segment_dir_for(tmp_path, older_day, SEGMENT)
    append_offload_event(
        day=newer_day,
        stream=DEFAULT_STREAM,
        segment=SEGMENT,
        snapshot_id="snap-new",
        files=[OffloadFile(name="audio.wav", bytes=len(CONTENT), sha256=SHA)],
        time=100,
    )
    append_offload_event(
        day=older_day,
        stream=DEFAULT_STREAM,
        segment=SEGMENT,
        snapshot_id="snap-old",
        files=[OffloadFile(name="audio.wav", bytes=len(CONTENT), sha256=SHA)],
        time=101,
    )
    restored_targets: list[Path] = []

    def fake_run_restic(args: list[str], **_kwargs: Any) -> ResticResult:
        target = Path(args[args.index("--target") + 1])
        restored_targets.append(target)
        (target / "audio.wav").write_bytes(CONTENT)
        return _restic_result(0, args)

    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(offload_restore, "device_free_bytes", lambda: 5_000_000_000)
    monkeypatch.setattr(offload_restore, "run_restic", fake_run_restic)

    result = offload_restore.restore_all()

    assert result.status == "ok"
    assert restored_targets == [older_dir, newer_dir]


def test_restore_refuses_when_free_space_guard_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _backup_config())
    _segment_dir(tmp_path)
    append_offload_event(
        day=DAY,
        stream=DEFAULT_STREAM,
        segment=SEGMENT,
        snapshot_id="snap1",
        files=[OffloadFile(name="huge.wav", bytes=2_000_000_000, sha256=SHA)],
        time=100,
    )
    run_restic = Mock()
    monkeypatch.setattr(offload_restore, "device_free_bytes", lambda: 2_999_999_999)
    monkeypatch.setattr(offload_restore, "run_restic", run_restic)

    result = offload_restore.restore_day(DAY)

    assert result.status == "refused"
    assert result.reason == "insufficient_free_space"
    run_restic.assert_not_called()


def test_restore_no_op_and_backup_not_ready_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _backup_config())
    run_restic = Mock()
    monkeypatch.setattr(offload_restore, "run_restic", run_restic)

    no_op = offload_restore.restore_day(DAY)

    assert no_op.status == "no_op"
    assert no_op.reason == "nothing_to_restore"
    run_restic.assert_not_called()

    _segment_dir(tmp_path)
    _seed_ledger()
    append_restore_event(day=DAY, stream=DEFAULT_STREAM, segment=SEGMENT, time=101)

    already_restored = offload_restore.restore_day(DAY)

    assert already_restored.status == "no_op"
    assert already_restored.reason == "nothing_to_restore"
    run_restic.assert_not_called()

    _write_config(tmp_path, _backup_config(enabled=False))
    _seed_ledger()

    not_ready = offload_restore.restore_day(DAY)

    assert not_ready.status == "error"
    assert not_ready.reason == "backup_not_ready"


def test_restore_tool_unavailable_and_ledger_degraded_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _backup_config())
    _segment_dir(tmp_path)
    _seed_ledger()
    monkeypatch.setattr(offload_restore, "device_free_bytes", lambda: 5_000_000_000)
    monkeypatch.setattr(
        engine,
        "ensure_restic",
        Mock(side_effect=RuntimeError("missing")),
    )

    restic_unavailable = offload_restore.restore_day(DAY)

    assert restic_unavailable.reason == "restic_unavailable"

    ledger_path = tmp_path / "health" / "offload" / f"{DAY}.jsonl"
    ledger_path.write_bytes(b"\xff")

    degraded = offload_restore.restore_day(DAY)

    assert degraded.status == "error"
    assert degraded.reason == "ledger_degraded"


def test_operated_restore_reports_rclone_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _backup_config(mode="operated"))
    save_hosted_binding(_binding())
    _segment_dir(tmp_path)
    _seed_ledger()
    monkeypatch.setattr(offload_restore, "device_free_bytes", lambda: 5_000_000_000)
    monkeypatch.setattr(engine, "fetch_hosted_credentials", Mock(return_value=_creds()))
    monkeypatch.setattr(engine, "ensure_restic", Mock(return_value=Path("/restic")))
    monkeypatch.setattr(
        engine,
        "ensure_rclone",
        Mock(side_effect=RuntimeError("missing")),
    )

    result = offload_restore.restore_day(DAY)

    assert result.status == "error"
    assert result.reason == "rclone_unavailable"
