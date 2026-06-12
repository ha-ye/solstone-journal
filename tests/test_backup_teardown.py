# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from solstone.think.backup import state, teardown
from solstone.think.backup.runner import ResticResult


def _config_path(journal: Path) -> Path:
    return journal / "config" / "journal.json"


def _write_config(journal: Path, payload: dict[str, Any]) -> None:
    config_path = _config_path(journal)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload), encoding="utf-8")


def _read_config(journal: Path) -> dict[str, Any]:
    return json.loads(_config_path(journal).read_text(encoding="utf-8"))


def _configured_backup() -> dict[str, Any]:
    return {
        "backup": {
            "enabled": True,
            "destination": {
                "repository": "s3:safe-bucket/path",
                "backend": "s3",
                "credentials": {
                    "access_key_id": "access-key",
                    "secret_access_key": "secret-key",
                },
            },
            "daily_key": "daily-secret",
            "recovery_key": "A" * 64,
            "confirmed_recovery_key": True,
        }
    }


def _result(returncode: int, parsed_json: Any | None = None) -> ResticResult:
    return ResticResult(
        returncode=returncode,
        stdout="",
        stderr="",
        json=parsed_json,
        argv=("restic",),
    )


def test_teardown_skips_when_unconfigured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {})
    monkeypatch.setattr(
        teardown,
        "ensure_restic",
        lambda: pytest.fail("restic should not be resolved"),
    )

    result = teardown.teardown_backup()

    assert result == teardown.TeardownResult(status="skipped", reason_code=None)


def test_teardown_enumerates_then_forgets_full_ids_and_clears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _configured_backup())
    responses = iter(
        [
            _result(0, [{"id": "full-id-1"}, {"id": "full-id-2"}]),
            _result(0),
        ]
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        calls.append((args, kwargs))
        return next(responses)

    monkeypatch.setattr(teardown, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(teardown, "run_restic", fake_run_restic)

    result = teardown.teardown_backup()

    assert result == teardown.TeardownResult(status="ok", reason_code=None)
    assert calls[0][0] == ["snapshots"]
    assert calls[0][1]["password"] == "daily-secret"
    assert calls[0][1]["json"] is True
    assert calls[1][0] == ["forget", "full-id-1", "full-id-2", "--prune"]
    assert "json" not in calls[1][1]
    assert _read_config(tmp_path)["backup"] == state.BACKUP_DEFAULTS


def test_teardown_empty_snapshots_clears_without_forget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _configured_backup())
    calls: list[list[str]] = []

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        calls.append(args)
        return _result(0, [])

    monkeypatch.setattr(teardown, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(teardown, "run_restic", fake_run_restic)

    result = teardown.teardown_backup()

    assert result.status == "ok"
    assert calls == [["snapshots"]]
    assert _read_config(tmp_path)["backup"] == state.BACKUP_DEFAULTS


def test_teardown_snapshots_failure_leaves_config_intact_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    original_config = _configured_backup()
    _write_config(tmp_path, original_config)
    monkeypatch.setattr(teardown, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(teardown, "run_restic", lambda args, **kwargs: _result(12))
    caplog.set_level(logging.WARNING, logger="solstone.backup.teardown")

    result = teardown.teardown_backup()

    assert result == teardown.TeardownResult(status="error", reason_code="auth_failed")
    assert _read_config(tmp_path) == original_config
    serialized = json.dumps(result.__dict__)
    for secret in ("daily-secret", "access-key", "secret-key"):
        assert secret not in serialized
        assert secret not in caplog.text


def test_teardown_forget_failure_leaves_config_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    original_config = _configured_backup()
    _write_config(tmp_path, original_config)
    responses = iter(
        [
            _result(0, [{"id": "full-id"}]),
            _result(11),
        ]
    )
    monkeypatch.setattr(teardown, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(teardown, "run_restic", lambda args, **kwargs: next(responses))

    result = teardown.teardown_backup()

    assert result == teardown.TeardownResult(status="error", reason_code="locked")
    assert _read_config(tmp_path) == original_config


def test_teardown_backend_invalid_leaves_config_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    original_config = _configured_backup()
    del original_config["backup"]["destination"]["credentials"]["secret_access_key"]
    _write_config(tmp_path, original_config)

    result = teardown.teardown_backup()

    assert result == teardown.TeardownResult(status="error", reason_code="failed")
    assert _read_config(tmp_path) == original_config


def test_teardown_restic_unavailable_leaves_config_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    original_config = _configured_backup()
    _write_config(tmp_path, original_config)
    monkeypatch.setattr(
        teardown,
        "ensure_restic",
        lambda: (_ for _ in ()).throw(RuntimeError("missing")),
    )
    monkeypatch.setattr(
        teardown,
        "run_restic",
        lambda *args, **kwargs: pytest.fail("restic should not run"),
    )

    result = teardown.teardown_backup()

    assert result == teardown.TeardownResult(
        status="error",
        reason_code="restic_unavailable",
    )
    assert _read_config(tmp_path) == original_config
