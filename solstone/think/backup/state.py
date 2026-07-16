# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Journal config accessors for solstone backup state."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from solstone.think.backup.destination import Destination
from solstone.think.backup.hosted import load_hosted_binding
from solstone.think.backup.keys import (
    format_recovery_key_display,
    generate_daily_key,
    generate_recovery_key,
)
from solstone.think.journal_config import (
    hold_config_lock,
    read_journal_config,
    write_journal_config,
)

BACKUP_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "mode": "byo",
    "destination": {
        "repository": None,
        "backend": None,
        "credentials": {},
    },
    "daily_key": None,
    "recovery_key": None,
    "confirmed_recovery_key": False,
    "retention": {
        "hourly": 24,
        "daily": 7,
        "weekly": 4,
        "monthly": 12,
    },
    "offload": {
        "enabled": False,
        "budget_bytes": None,
        "floor_bytes": None,
    },
    "schedule": {
        "every": "daily",
        "enabled": False,
    },
    "last_backup": {
        "time": None,
        "snapshot_id": None,
        "status": None,
        "error_reason": None,
    },
    "last_prune": {
        "time": None,
        "status": None,
        "error_reason": None,
    },
    # Offload records use "reason" instead of "error_reason" because skipped
    # and stalled are expected outcomes, not errors.
    "last_offload": {
        "time": None,
        "status": None,
        "reason": None,
    },
    "last_verification": {
        "time": None,
        "status": None,
        "reason": None,
        "last_ok_time": None,
        "checked_subset": None,
    },
}
OFFLOAD_KEYS = ("enabled", "budget_bytes", "floor_bytes")
OFFLOAD_STATUSES = ("ok", "skipped", "stalled", "error")
RETENTION_KEYS = ("hourly", "daily", "weekly", "monthly")
VERIFICATION_STATUSES = ("ok", "skipped", "error")


@dataclass(frozen=True)
class BackupKeys:
    daily_key: str
    recovery_key: str
    recovery_key_display: str


def _merge_defaults(defaults: dict[str, Any], raw: Any) -> dict[str, Any]:
    merged = copy.deepcopy(defaults)
    if not isinstance(raw, dict):
        return merged
    for key, value in raw.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def _writable_backup_section(config: dict[str, Any]) -> dict[str, Any]:
    backup = config.get("backup")
    if not isinstance(backup, dict):
        backup = {}
        config["backup"] = backup
    return backup


def _build_backup_keys(daily_key: Any, recovery_key: Any) -> BackupKeys | None:
    if daily_key is None or recovery_key is None:
        return None
    if not isinstance(daily_key, str) or not isinstance(recovery_key, str):
        raise ValueError("backup keys must be strings when present")
    return BackupKeys(
        daily_key=daily_key,
        recovery_key=recovery_key,
        recovery_key_display=format_recovery_key_display(recovery_key),
    )


def get_backup_config() -> dict[str, Any]:
    config = read_journal_config()
    return _merge_defaults(BACKUP_DEFAULTS, config.get("backup", {}))


def get_destination() -> Destination | None:
    destination = get_backup_config()["destination"]
    repository = destination.get("repository")
    backend = destination.get("backend")
    credentials = destination.get("credentials", {})
    if repository is None or backend is None:
        return None
    if not isinstance(repository, str) or not isinstance(backend, str):
        raise ValueError("backup destination repository and backend must be strings")
    if not isinstance(credentials, dict):
        raise ValueError("backup destination credentials must be a JSON object")
    return Destination(
        repository=repository,
        backend=backend,
        credentials=dict(credentials),
    )


def get_keys() -> BackupKeys | None:
    config = get_backup_config()
    return _build_backup_keys(config["daily_key"], config["recovery_key"])


def generate_and_store_keys() -> BackupKeys:
    with hold_config_lock():
        config = read_journal_config()
        backup = _writable_backup_section(config)
        daily_key = backup.get("daily_key")
        recovery_key = backup.get("recovery_key")
        if daily_key is None:
            daily_key = generate_daily_key()
        if recovery_key is None:
            recovery_key = generate_recovery_key()
        backup["daily_key"] = daily_key
        backup["recovery_key"] = recovery_key
        keys = _build_backup_keys(daily_key, recovery_key)
        if keys is None:
            raise RuntimeError("backup key generation failed")
        write_journal_config(config)
        return keys


def set_destination(destination: Destination) -> None:
    with hold_config_lock():
        config = read_journal_config()
        backup = _writable_backup_section(config)
        backup["destination"] = {
            "repository": destination.repository,
            "backend": destination.backend,
            "credentials": dict(destination.credentials),
        }
        write_journal_config(config)


def set_enabled(enabled: bool) -> None:
    with hold_config_lock():
        config = read_journal_config()
        backup = _writable_backup_section(config)
        backup["enabled"] = enabled
        write_journal_config(config)


def set_mode(mode: str) -> None:
    if mode not in {"byo", "operated"}:
        raise ValueError("backup mode must be byo or operated")

    with hold_config_lock():
        config = read_journal_config()
        backup = _writable_backup_section(config)
        backup["mode"] = mode
        write_journal_config(config)


def set_recovery_key_confirmed(confirmed: bool = True) -> None:
    with hold_config_lock():
        config = read_journal_config()
        backup = _writable_backup_section(config)
        backup["confirmed_recovery_key"] = confirmed
        write_journal_config(config)


def set_retention(retention: dict[str, int]) -> None:
    if not isinstance(retention, dict):
        raise ValueError("backup retention must be a JSON object")
    if set(retention) != set(RETENTION_KEYS):
        raise ValueError("backup retention must include hourly, daily, weekly, monthly")
    for key in RETENTION_KEYS:
        value = retention[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("backup retention values must be non-negative integers")

    with hold_config_lock():
        config = read_journal_config()
        backup = _writable_backup_section(config)
        backup["retention"] = {key: int(retention[key]) for key in RETENTION_KEYS}
        write_journal_config(config)


def set_offload(offload: dict[str, Any]) -> None:
    if not isinstance(offload, dict):
        raise ValueError("backup offload must be a JSON object")
    if set(offload) != set(OFFLOAD_KEYS):
        raise ValueError(
            "backup offload must include enabled, budget_bytes, floor_bytes"
        )
    if not isinstance(offload["enabled"], bool):
        raise ValueError("backup offload enabled must be a boolean")
    for key in ("budget_bytes", "floor_bytes"):
        value = offload[key]
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError(
                "backup offload byte values must be positive integers or null"
            )

    with hold_config_lock():
        config = read_journal_config()
        backup = _writable_backup_section(config)
        backup["offload"] = {
            "enabled": offload["enabled"],
            "budget_bytes": offload["budget_bytes"],
            "floor_bytes": offload["floor_bytes"],
        }
        write_journal_config(config)


def set_recovery_key(recovery_key: str) -> None:
    with hold_config_lock():
        config = read_journal_config()
        backup = _writable_backup_section(config)
        backup["recovery_key"] = recovery_key
        write_journal_config(config)


def clear_backup_config() -> None:
    with hold_config_lock():
        config = read_journal_config()
        config["backup"] = copy.deepcopy(BACKUP_DEFAULTS)
        write_journal_config(config)


def record_backup_result(
    *,
    status: str,
    time: int | None,
    snapshot_id: str | None = None,
    error_reason: str | None = None,
) -> None:
    with hold_config_lock():
        config = read_journal_config()
        backup = _writable_backup_section(config)
        backup["last_backup"] = {
            "time": time,
            "snapshot_id": snapshot_id,
            "status": status,
            "error_reason": error_reason,
        }
        write_journal_config(config)


def record_prune_result(
    *,
    status: str,
    time: int | None,
    error_reason: str | None = None,
) -> None:
    with hold_config_lock():
        config = read_journal_config()
        backup = _writable_backup_section(config)
        backup["last_prune"] = {
            "time": time,
            "status": status,
            "error_reason": error_reason,
        }
        write_journal_config(config)


def record_offload_result(
    *,
    status: str,
    time: int | None,
    reason: str | None = None,
) -> None:
    """Record the last media-offload run.

    Convention: reason is None on ok. This layer only enforces the closed
    status vocabulary; callers own richer run-state validation.
    """
    if status not in OFFLOAD_STATUSES:
        raise ValueError("backup offload status must be ok, skipped, stalled, or error")

    with hold_config_lock():
        config = read_journal_config()
        backup = _writable_backup_section(config)
        backup["last_offload"] = {
            "time": time,
            "status": status,
            "reason": reason,
        }
        write_journal_config(config)


def record_verification_result(
    *,
    status: str,
    time: int | None,
    reason: str | None = None,
    checked_subset: str | None = None,
) -> None:
    """Record the last backup repository read-back verification run.

    Convention: reason is None on ok. This layer only enforces the closed
    status vocabulary; callers own richer run-state validation.
    """
    if status not in VERIFICATION_STATUSES:
        raise ValueError("backup verification status must be ok, skipped, or error")

    with hold_config_lock():
        config = read_journal_config()
        backup = _writable_backup_section(config)
        prior = backup.get("last_verification")
        prior_last_ok_time = (
            prior.get("last_ok_time") if isinstance(prior, dict) else None
        )
        # last_ok_time is a historical high-water mark; checked_subset describes
        # the latest attempt and must not overclaim after a failure.
        last_ok_time = time if status == "ok" else prior_last_ok_time
        latest_checked_subset = checked_subset if status == "ok" else None
        backup["last_verification"] = {
            "time": time,
            "status": status,
            "reason": reason,
            "last_ok_time": last_ok_time,
            "checked_subset": latest_checked_subset,
        }
        write_journal_config(config)


def status_view() -> dict[str, Any]:
    config = get_backup_config()
    destination = config["destination"]
    credentials = destination.get("credentials")
    binding = load_hosted_binding()
    hosted = {"bound": False}
    if binding is not None:
        hosted = {
            "bound": True,
            "bucket": binding.bucket,
            "prefix": binding.prefix,
        }
    return {
        "enabled": config["enabled"],
        "mode": config["mode"],
        "destination": {
            "repository": destination.get("repository"),
            "backend": destination.get("backend"),
            "credentials_set": bool(credentials),
        },
        "daily_key_set": config["daily_key"] is not None,
        "recovery_key_set": config["recovery_key"] is not None,
        "recovery_key_confirmed": bool(config["confirmed_recovery_key"]),
        "retention": config["retention"],
        "offload": config["offload"],
        "schedule": config["schedule"],
        "last_backup": config["last_backup"],
        "last_prune": config["last_prune"],
        "last_offload": config["last_offload"],
        "last_verification": config["last_verification"],
        "hosted": hosted,
    }


__all__ = [
    "BACKUP_DEFAULTS",
    "BackupKeys",
    "OFFLOAD_KEYS",
    "OFFLOAD_STATUSES",
    "VERIFICATION_STATUSES",
    "clear_backup_config",
    "generate_and_store_keys",
    "get_backup_config",
    "get_destination",
    "get_keys",
    "record_backup_result",
    "record_offload_result",
    "record_prune_result",
    "record_verification_result",
    "set_destination",
    "set_enabled",
    "set_mode",
    "set_offload",
    "set_recovery_key",
    "set_recovery_key_confirmed",
    "set_retention",
    "status_view",
]
