# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from solstone.think.backup import rotation, state
from solstone.think.backup.destination import DestinationStatus
from solstone.think.backup.repo import ResticKeyError

OLD_KEY = "A" * 64
NEW_KEY = "B" * 64


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
            "recovery_key": OLD_KEY,
            "confirmed_recovery_key": True,
        }
    }


def _repo_exists() -> DestinationStatus:
    return DestinationStatus(True, True, "repo_exists", "exists")


def test_rotation_skips_when_unconfigured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {})
    monkeypatch.setattr(
        rotation,
        "ensure_restic",
        lambda: pytest.fail("restic should not be resolved"),
    )

    result = rotation.rotate_recovery_key()

    assert result == rotation.RotationResult(
        status="skipped",
        reason_code=None,
        recovery_key=None,
        recovery_key_display=None,
    )


def test_rotation_restic_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _configured_backup())
    monkeypatch.setattr(
        rotation,
        "ensure_restic",
        lambda: (_ for _ in ()).throw(RuntimeError("missing")),
    )

    result = rotation.rotate_recovery_key()

    assert result.reason_code == "restic_unavailable"


def test_rotation_backend_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config = _configured_backup()
    del config["backup"]["destination"]["credentials"]["secret_access_key"]
    _write_config(tmp_path, config)
    monkeypatch.setattr(rotation, "ensure_restic", lambda: Path("/restic"))

    result = rotation.rotate_recovery_key()

    assert result.reason_code == "failed"


def test_rotation_success_order_persists_new_key_and_scrubs_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, _configured_backup())
    order: list[tuple[str, Any]] = []
    original_set_recovery_key = state.set_recovery_key
    original_set_recovery_key_confirmed = state.set_recovery_key_confirmed

    def fake_capture(destination, *, password, restic_path, timeout=None):
        order.append(("capture", password))
        return "old-key-id"

    def fake_add(destination, *, daily_key, recovery_key, restic_path, timeout=None):
        order.append(("add", daily_key, recovery_key))

    def fake_validate(destination, password, *, restic_path, timeout=None):
        order.append(("verify", password))
        return _repo_exists()

    def fake_remove(destination, *, password, key_id, restic_path, timeout=None):
        order.append(("remove", password, key_id))

    def wrapped_set_recovery_key(recovery_key: str) -> None:
        order.append(("persist-key", recovery_key))
        original_set_recovery_key(recovery_key)

    def wrapped_set_recovery_key_confirmed(confirmed: bool) -> None:
        order.append(("persist-confirmed", confirmed))
        original_set_recovery_key_confirmed(confirmed)

    monkeypatch.setattr(rotation, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(rotation, "_capture_current_key_id", fake_capture)
    monkeypatch.setattr(rotation, "_add_recovery_key", fake_add)
    monkeypatch.setattr(rotation, "validate_destination", fake_validate)
    monkeypatch.setattr(rotation, "_remove_key", fake_remove)
    monkeypatch.setattr(rotation, "generate_recovery_key", lambda: NEW_KEY)
    monkeypatch.setattr(rotation, "set_recovery_key", wrapped_set_recovery_key)
    monkeypatch.setattr(
        rotation,
        "set_recovery_key_confirmed",
        wrapped_set_recovery_key_confirmed,
    )
    caplog.set_level(logging.INFO, logger="solstone.backup.rotation")

    result = rotation.rotate_recovery_key()

    assert result.status == "ok"
    assert result.reason_code is None
    assert result.recovery_key == NEW_KEY
    assert result.recovery_key_display == rotation.format_recovery_key_display(NEW_KEY)
    assert order == [
        ("capture", OLD_KEY),
        ("add", "daily-secret", NEW_KEY),
        ("verify", NEW_KEY),
        ("remove", "daily-secret", "old-key-id"),
        ("persist-key", NEW_KEY),
        ("persist-confirmed", False),
    ]
    backup = _read_config(tmp_path)["backup"]
    assert backup["daily_key"] == "daily-secret"
    assert backup["recovery_key"] == NEW_KEY
    assert backup["confirmed_recovery_key"] is False
    serialized = json.dumps(result.__dict__)
    for secret in ("daily-secret", OLD_KEY, "access-key", "secret-key"):
        assert secret not in serialized
        assert secret not in caplog.text
    assert NEW_KEY not in caplog.text


def test_rotation_capture_auth_failure_maps_reason_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    original_config = _configured_backup()
    _write_config(tmp_path, original_config)
    monkeypatch.setattr(rotation, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(
        rotation,
        "_capture_current_key_id",
        lambda *args, **kwargs: (_ for _ in ()).throw(ResticKeyError("key list", 12)),
    )

    result = rotation.rotate_recovery_key()

    assert result.reason_code == "auth_failed"
    assert _read_config(tmp_path) == original_config


def test_rotation_confirm_failure_aborts_before_verify_or_remove(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    original_config = _configured_backup()
    _write_config(tmp_path, original_config)
    calls: list[str] = []
    monkeypatch.setattr(rotation, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(
        rotation,
        "_capture_current_key_id",
        lambda *args, **kwargs: calls.append("capture") or "old-key-id",
    )
    monkeypatch.setattr(
        rotation,
        "_add_recovery_key",
        lambda *args, **kwargs: calls.append("add"),
    )
    monkeypatch.setattr(rotation, "generate_recovery_key", lambda: NEW_KEY)
    monkeypatch.setattr(rotation, "confirm_recovery_key", lambda *args: False)
    monkeypatch.setattr(
        rotation,
        "validate_destination",
        lambda *args, **kwargs: pytest.fail("must not verify"),
    )
    monkeypatch.setattr(
        rotation,
        "_remove_key",
        lambda *args, **kwargs: pytest.fail("must not remove"),
    )

    result = rotation.rotate_recovery_key()

    assert result.reason_code == "failed"
    assert calls == ["capture", "add"]
    assert _read_config(tmp_path) == original_config


def test_rotation_verify_failure_aborts_before_remove_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    original_config = _configured_backup()
    _write_config(tmp_path, original_config)
    monkeypatch.setattr(rotation, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(rotation, "_capture_current_key_id", lambda *a, **k: "old-id")
    monkeypatch.setattr(rotation, "_add_recovery_key", lambda *a, **k: None)
    monkeypatch.setattr(rotation, "generate_recovery_key", lambda: NEW_KEY)
    monkeypatch.setattr(
        rotation,
        "validate_destination",
        lambda *a, **k: DestinationStatus(True, True, "auth_failed", "bad"),
    )
    monkeypatch.setattr(
        rotation,
        "_remove_key",
        lambda *args, **kwargs: pytest.fail("must not remove"),
    )

    result = rotation.rotate_recovery_key()

    assert result.reason_code == "auth_failed"
    assert _read_config(tmp_path) == original_config


def test_rotation_remove_failure_leaves_config_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    original_config = _configured_backup()
    _write_config(tmp_path, original_config)
    monkeypatch.setattr(rotation, "ensure_restic", lambda: Path("/restic"))
    monkeypatch.setattr(rotation, "_capture_current_key_id", lambda *a, **k: "old-id")
    monkeypatch.setattr(rotation, "_add_recovery_key", lambda *a, **k: None)
    monkeypatch.setattr(rotation, "generate_recovery_key", lambda: NEW_KEY)
    monkeypatch.setattr(
        rotation, "validate_destination", lambda *a, **k: _repo_exists()
    )
    monkeypatch.setattr(
        rotation,
        "_remove_key",
        lambda *args, **kwargs: (_ for _ in ()).throw(ResticKeyError("key remove", 11)),
    )

    result = rotation.rotate_recovery_key()

    assert result.reason_code == "locked"
    assert _read_config(tmp_path) == original_config
