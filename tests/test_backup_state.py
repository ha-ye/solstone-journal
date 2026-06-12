# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

from solstone.think.backup import state
from solstone.think.backup.destination import Destination


def _config_path(journal: Path) -> Path:
    return journal / "config" / "journal.json"


def _write_config(journal: Path, payload: dict) -> None:
    config_path = _config_path(journal)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload), encoding="utf-8")


def _read_config(journal: Path) -> dict:
    return json.loads(_config_path(journal).read_text(encoding="utf-8"))


def test_missing_backup_section_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {"identity": {"name": "Test"}})

    config = state.get_backup_config()

    assert config == state.BACKUP_DEFAULTS
    assert state.get_destination() is None
    assert state.get_keys() is None


def test_partial_backup_section_gets_per_field_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {"backup": {"enabled": True}})

    config = state.get_backup_config()

    assert config["enabled"] is True
    assert config["mode"] == "byo"
    assert config["destination"] == state.BACKUP_DEFAULTS["destination"]
    assert config["retention"] == state.BACKUP_DEFAULTS["retention"]
    assert config["schedule"] == state.BACKUP_DEFAULTS["schedule"]
    assert config["last_backup"] == state.BACKUP_DEFAULTS["last_backup"]


def test_generate_and_store_keys_get_or_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {})
    monkeypatch.setattr(state, "generate_daily_key", lambda: "generated-daily")
    monkeypatch.setattr(state, "generate_recovery_key", lambda: "A" * 64)

    first = state.generate_and_store_keys()

    assert first.daily_key == "generated-daily"
    assert first.recovery_key == "A" * 64
    assert _read_config(tmp_path)["backup"]["daily_key"] == "generated-daily"

    monkeypatch.setattr(state, "generate_daily_key", lambda: "new-daily")
    monkeypatch.setattr(state, "generate_recovery_key", lambda: "B" * 64)

    second = state.generate_and_store_keys()

    assert second == first
    assert _read_config(tmp_path)["backup"]["recovery_key"] == "A" * 64


def test_generate_and_store_keys_preserves_hand_set_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(
        tmp_path,
        {
            "backup": {
                "daily_key": "manual-daily",
                "recovery_key": "B" * 64,
            }
        },
    )

    def fail_generate() -> str:
        raise AssertionError("existing keys must not be regenerated")

    monkeypatch.setattr(state, "generate_daily_key", fail_generate)
    monkeypatch.setattr(state, "generate_recovery_key", fail_generate)

    keys = state.generate_and_store_keys()

    assert keys.daily_key == "manual-daily"
    assert keys.recovery_key == "B" * 64


def test_set_destination_writes_private_config_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {})

    state.set_destination(
        Destination(
            repository="s3:safe-bucket/path",
            backend="s3",
            credentials={
                "access_key_id": "access-key",
                "secret_access_key": "secret-key",
            },
        )
    )

    assert stat.S_IMODE(_config_path(tmp_path).stat().st_mode) == 0o600


def test_setters_round_trip_under_config_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {})
    entries = 0

    @contextmanager
    def fake_lock():
        nonlocal entries
        entries += 1
        yield

    monkeypatch.setattr(state, "hold_config_lock", fake_lock)
    destination = Destination(
        repository="b2:bucket:path",
        backend="b2",
        credentials={
            "account_id": "account-id",
            "account_key": "account-key",
        },
    )

    state.set_destination(destination)
    state.set_recovery_key_confirmed()

    assert entries == 2
    assert state.get_destination() == destination
    assert _read_config(tmp_path)["backup"]["confirmed_recovery_key"] is True


def test_set_enabled_round_trips_under_config_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {})
    entries = 0

    @contextmanager
    def fake_lock():
        nonlocal entries
        entries += 1
        yield

    monkeypatch.setattr(state, "hold_config_lock", fake_lock)

    state.set_enabled(True)
    state.set_enabled(False)

    assert entries == 2
    assert _read_config(tmp_path)["backup"]["enabled"] is False
    assert state.get_backup_config()["enabled"] is False
    assert stat.S_IMODE(_config_path(tmp_path).stat().st_mode) == 0o600


def test_set_retention_round_trips_under_config_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {})
    entries = 0

    @contextmanager
    def fake_lock():
        nonlocal entries
        entries += 1
        yield

    monkeypatch.setattr(state, "hold_config_lock", fake_lock)

    state.set_retention({"hourly": 1, "daily": 2, "weekly": 3, "monthly": 4})

    assert entries == 1
    assert _read_config(tmp_path)["backup"]["retention"] == {
        "hourly": 1,
        "daily": 2,
        "weekly": 3,
        "monthly": 4,
    }
    assert state.get_backup_config()["retention"] == {
        "hourly": 1,
        "daily": 2,
        "weekly": 3,
        "monthly": 4,
    }
    assert stat.S_IMODE(_config_path(tmp_path).stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "retention",
    [
        {"hourly": 1, "daily": 2, "weekly": 3},
        {"hourly": 1, "daily": 2, "weekly": 3, "monthly": 4, "yearly": 5},
        {"hourly": True, "daily": 2, "weekly": 3, "monthly": 4},
        {"hourly": "1", "daily": 2, "weekly": 3, "monthly": 4},
        {"hourly": -1, "daily": 2, "weekly": 3, "monthly": 4},
    ],
)
def test_set_retention_rejects_invalid_values_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retention: dict[str, object],
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    original = {
        "backup": {"retention": {"hourly": 9, "daily": 8, "weekly": 7, "monthly": 6}}
    }
    _write_config(tmp_path, original)
    before = _read_config(tmp_path)
    entries = 0

    @contextmanager
    def fake_lock():
        nonlocal entries
        entries += 1
        yield

    monkeypatch.setattr(state, "hold_config_lock", fake_lock)

    with pytest.raises(ValueError):
        state.set_retention(retention)  # type: ignore[arg-type]

    assert entries == 0
    assert _read_config(tmp_path) == before


def test_set_recovery_key_writes_known_key_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(
        tmp_path,
        {
            "backup": {
                "daily_key": "daily-secret",
                "recovery_key": "A" * 64,
                "confirmed_recovery_key": True,
            }
        },
    )

    state.set_recovery_key("B" * 64)

    backup = _read_config(tmp_path)["backup"]
    assert backup["daily_key"] == "daily-secret"
    assert backup["recovery_key"] == "B" * 64
    assert backup["confirmed_recovery_key"] is True
    assert stat.S_IMODE(_config_path(tmp_path).stat().st_mode) == 0o600


def test_clear_backup_config_resets_backup_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(
        tmp_path,
        {
            "identity": {"name": "Test"},
            "backup": {
                "enabled": True,
                "daily_key": "daily-secret",
                "recovery_key": "C" * 64,
            },
        },
    )

    state.clear_backup_config()

    config = _read_config(tmp_path)
    assert config["identity"] == {"name": "Test"}
    assert config["backup"] == state.BACKUP_DEFAULTS
    assert stat.S_IMODE(_config_path(tmp_path).stat().st_mode) == 0o600


def test_record_backup_result_writes_last_backup_under_config_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {})
    entries = 0

    @contextmanager
    def fake_lock():
        nonlocal entries
        entries += 1
        yield

    monkeypatch.setattr(state, "hold_config_lock", fake_lock)

    state.record_backup_result(
        status="error",
        time=123,
        snapshot_id="partial-snapshot",
        error_reason="incomplete",
    )

    assert entries == 1
    assert _read_config(tmp_path)["backup"]["last_backup"] == {
        "time": 123,
        "snapshot_id": "partial-snapshot",
        "status": "error",
        "error_reason": "incomplete",
    }
    assert stat.S_IMODE(_config_path(tmp_path).stat().st_mode) == 0o600


def test_record_prune_result_writes_last_prune(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(tmp_path, {})
    entries = 0

    @contextmanager
    def fake_lock():
        nonlocal entries
        entries += 1
        yield

    monkeypatch.setattr(state, "hold_config_lock", fake_lock)

    state.record_prune_result(
        status="error",
        time=456,
        error_reason="timeout",
    )

    assert entries == 1
    assert _read_config(tmp_path)["backup"]["last_prune"] == {
        "time": 456,
        "status": "error",
        "error_reason": "timeout",
    }
    assert "snapshot_id" not in _read_config(tmp_path)["backup"]["last_prune"]


def test_status_view_redacts_all_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    _write_config(
        tmp_path,
        {
            "backup": {
                "enabled": True,
                "mode": "byo",
                "destination": {
                    "repository": "s3:safe-bucket/path",
                    "backend": "s3",
                    "credentials": {
                        "access_key_id": "access-key",
                        "secret_access_key": "secret-key",
                    },
                },
                "daily_key": "daily-secret",
                "recovery_key": "C" * 64,
                "confirmed_recovery_key": True,
            }
        },
    )

    view = state.status_view()
    serialized = json.dumps(view)

    for secret in ("daily-secret", "C" * 64, "access-key", "secret-key"):
        assert secret not in serialized
    assert view["destination"] == {
        "repository": "s3:safe-bucket/path",
        "backend": "s3",
        "credentials_set": True,
    }
    assert view["daily_key_set"] is True
    assert view["recovery_key_set"] is True
    assert view["recovery_key_confirmed"] is True
    assert view["last_prune"] == state.BACKUP_DEFAULTS["last_prune"]
