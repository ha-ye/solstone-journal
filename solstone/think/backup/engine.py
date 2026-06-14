# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Scheduled restic backup and prune engine for solstone backup."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from solstone.think.backup.destination import (
    Destination,
    assemble_backend_env,
)
from solstone.think.backup.install import ensure_restic
from solstone.think.backup.runner import (
    reason_for_returncode,
    run_restic,
    select_summary,
)
from solstone.think.backup.state import (
    BackupKeys,
    get_backup_config,
    get_destination,
    get_keys,
    record_backup_result,
    record_prune_result,
)
from solstone.think.callosum import callosum_send
from solstone.think.utils import get_journal

logger = logging.getLogger("solstone.backup.engine")

BACKUP_EXCLUDES = ("*.sqlite*", "health", "indexer", "cache", ".cache")
PRUNE_MAX_REPACK_SIZE = "1G"
UNLOCK_TIMEOUT_SECONDS = 5 * 60
BACKUP_TIMEOUT_SECONDS = 6 * 60 * 60
PRUNE_TIMEOUT_SECONDS = 2 * 60 * 60
BACKUP_MAX_RUNTIME = "7h"
PRUNE_MAX_RUNTIME = "3h"
BACKUP_RUN_CMD = ["journal", "maintenance", "run", "backup:run"]


@dataclass(frozen=True)
class BackupResult:
    status: str
    snapshot_id: str | None
    error_reason: str | None


@dataclass(frozen=True)
class PruneResult:
    status: str
    error_reason: str | None


@dataclass(frozen=True)
class _Runtime:
    destination: Destination
    keys: BackupKeys
    restic_path: Path


class _ResticUnavailable(RuntimeError):
    """Raised when the pinned restic binary cannot be acquired."""


def _resolve_runtime() -> _Runtime | None:
    config = get_backup_config()
    if config["enabled"] is not True:
        return None

    destination = get_destination()
    keys = get_keys()
    if destination is None or keys is None:
        return None

    try:
        restic_path = ensure_restic()
    except Exception as exc:
        raise _ResticUnavailable from exc

    return _Runtime(destination=destination, keys=keys, restic_path=restic_path)


def _backup_args() -> list[str]:
    args = ["backup", str(get_journal())]
    for pattern in BACKUP_EXCLUDES:
        args.extend(["--exclude", pattern])
    return args


def _assemble_backend_env(
    destination: Destination,
    *,
    operation: str,
) -> dict[str, str] | None:
    try:
        return assemble_backend_env(destination)
    except (KeyError, ValueError):
        logger.warning(
            "backup %s backend config invalid returncode=%s reason_code=%s",
            operation,
            None,
            "failed",
        )
        return None


def _recover_stale_lock(
    runtime: _Runtime,
    backend_env: Mapping[str, str | None],
) -> None:
    result = run_restic(
        ["unlock"],
        repository=runtime.destination.repository,
        password=runtime.keys.daily_key,
        restic_path=runtime.restic_path,
        backend_env=backend_env,
        timeout=UNLOCK_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        logger.debug(
            "backup stale lock recovery completed returncode=%s reason_code=ok",
            result.returncode,
        )
        return

    logger.warning(
        "backup stale lock recovery completed returncode=%s reason_code=%s",
        result.returncode,
        reason_for_returncode(result.returncode),
    )


def _record_backup_error(
    *,
    reason: str,
    snapshot_id: str | None = None,
) -> BackupResult:
    record_backup_result(
        status="error",
        time=int(time.time()),
        snapshot_id=snapshot_id,
        error_reason=reason,
    )
    return BackupResult(status="error", snapshot_id=snapshot_id, error_reason=reason)


def _record_prune_error(*, reason: str) -> PruneResult:
    record_prune_result(
        status="error",
        time=int(time.time()),
        error_reason=reason,
    )
    return PruneResult(status="error", error_reason=reason)


def run_backup() -> BackupResult:
    try:
        runtime = _resolve_runtime()
    except _ResticUnavailable:
        logger.warning(
            "backup completed returncode=%s reason_code=%s",
            None,
            "restic_unavailable",
        )
        return _record_backup_error(reason="restic_unavailable")

    if runtime is None:
        return BackupResult(status="skipped", snapshot_id=None, error_reason=None)

    backend_env = _assemble_backend_env(runtime.destination, operation="run")
    if backend_env is None:
        return _record_backup_error(reason="failed")

    _recover_stale_lock(runtime, backend_env)
    result = run_restic(
        _backup_args(),
        repository=runtime.destination.repository,
        password=runtime.keys.daily_key,
        restic_path=runtime.restic_path,
        backend_env=backend_env,
        json=True,
        timeout=BACKUP_TIMEOUT_SECONDS,
    )
    summary = select_summary(result.json)
    snapshot_id = None
    if summary is not None:
        raw_snapshot_id = summary.get("snapshot_id")
        if isinstance(raw_snapshot_id, str) and raw_snapshot_id:
            snapshot_id = raw_snapshot_id

    if result.returncode == 0 and snapshot_id is not None:
        record_backup_result(
            status="ok",
            time=int(time.time()),
            snapshot_id=snapshot_id,
            error_reason=None,
        )
        logger.info(
            "backup completed returncode=%s reason_code=ok",
            result.returncode,
        )
        return BackupResult(status="ok", snapshot_id=snapshot_id, error_reason=None)

    reason = (
        "unknown"
        if result.returncode == 0
        else reason_for_returncode(result.returncode)
    )
    logger.warning(
        "backup completed returncode=%s reason_code=%s",
        result.returncode,
        reason,
    )
    partial_snapshot_id = snapshot_id if result.returncode == 3 else None
    return _record_backup_error(reason=reason, snapshot_id=partial_snapshot_id)


def run_prune() -> PruneResult:
    try:
        runtime = _resolve_runtime()
    except _ResticUnavailable:
        logger.warning(
            "backup prune completed returncode=%s reason_code=%s",
            None,
            "restic_unavailable",
        )
        return _record_prune_error(reason="restic_unavailable")

    if runtime is None:
        return PruneResult(status="skipped", error_reason=None)

    backend_env = _assemble_backend_env(runtime.destination, operation="prune")
    if backend_env is None:
        return _record_prune_error(reason="failed")

    _recover_stale_lock(runtime, backend_env)
    retention = get_backup_config()["retention"]
    result = run_restic(
        [
            "forget",
            "--keep-hourly",
            str(retention.get("hourly", 24)),
            "--keep-daily",
            str(retention.get("daily", 7)),
            "--keep-weekly",
            str(retention.get("weekly", 4)),
            "--keep-monthly",
            str(retention.get("monthly", 12)),
            "--prune",
        ],
        repository=runtime.destination.repository,
        password=runtime.keys.daily_key,
        restic_path=runtime.restic_path,
        backend_env=backend_env,
        timeout=PRUNE_TIMEOUT_SECONDS,
        max_repack_size=PRUNE_MAX_REPACK_SIZE,
    )

    if result.returncode == 0:
        record_prune_result(
            status="ok",
            time=int(time.time()),
            error_reason=None,
        )
        logger.info(
            "backup prune completed returncode=%s reason_code=ok",
            result.returncode,
        )
        return PruneResult(status="ok", error_reason=None)

    reason = reason_for_returncode(result.returncode)
    logger.warning(
        "backup prune completed returncode=%s reason_code=%s",
        result.returncode,
        reason,
    )
    return _record_prune_error(reason=reason)


def request_backup_now() -> bool:
    return callosum_send("supervisor", "request", cmd=BACKUP_RUN_CMD)


__all__ = [
    "BACKUP_MAX_RUNTIME",
    "BACKUP_TIMEOUT_SECONDS",
    "BackupResult",
    "PRUNE_MAX_REPACK_SIZE",
    "PRUNE_MAX_RUNTIME",
    "PRUNE_TIMEOUT_SECONDS",
    "PruneResult",
    "UNLOCK_TIMEOUT_SECONDS",
    "request_backup_now",
    "run_backup",
    "run_prune",
]
