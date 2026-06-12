# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Restore engine hook for sol private backup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from solstone.think.backup.destination import Destination, assemble_backend_env
from solstone.think.backup.install import ensure_restic
from solstone.think.backup.keys import parse_recovery_key
from solstone.think.backup.runner import (
    reason_for_returncode,
    run_restic,
    select_summary,
)
from solstone.think.backup.state import (
    get_backup_config,
    set_destination,
    set_recovery_key,
    set_recovery_key_confirmed,
)
from solstone.think.indexer.journal import scan_journal
from solstone.think.utils import get_journal

logger = logging.getLogger("solstone.backup.restore")

RESTORE_LIST_TIMEOUT_SECONDS = 5 * 60
RESTORE_TIMEOUT_SECONDS = 6 * 60 * 60
RESTORE_CHECK_TIMEOUT_SECONDS = 60 * 60


@dataclass(frozen=True)
class RestoreResult:
    status: str
    reason_code: str | None
    integrity_ok: bool
    resumable: bool
    bytes_restored: int | None


def _restore_error(
    reason_code: str,
    *,
    returncode: int | None = None,
) -> RestoreResult:
    logger.warning(
        "backup restore completed returncode=%s reason_code=%s",
        returncode,
        reason_code,
    )
    return RestoreResult(
        status="error",
        reason_code=reason_code,
        integrity_ok=False,
        resumable=False,
        bytes_restored=None,
    )


def _original_path_from_snapshots(parsed: Any) -> str | None:
    if not isinstance(parsed, list) or not parsed:
        return None
    first = parsed[0]
    if not isinstance(first, dict):
        return None
    paths = first.get("paths")
    if not isinstance(paths, list) or not paths:
        return None
    original_path = paths[0]
    return original_path if isinstance(original_path, str) and original_path else None


def _bytes_restored(parsed: Any) -> int | None:
    summary = select_summary(parsed)
    if summary is None:
        return None
    value = summary.get("bytes_restored")
    return value if type(value) is int else None


def restore_journal(
    destination: Destination,
    entered_recovery_key: str,
) -> RestoreResult:
    try:
        canonical = parse_recovery_key(entered_recovery_key)
    except ValueError:
        return _restore_error("invalid_key")

    try:
        backend_env = assemble_backend_env(destination)
    except (KeyError, ValueError):
        return _restore_error("failed")

    try:
        restic_path = ensure_restic()
    except Exception:
        return _restore_error("restic_unavailable")

    snapshots = run_restic(
        ["snapshots", "latest"],
        repository=destination.repository,
        password=canonical,
        restic_path=restic_path,
        backend_env=backend_env,
        json=True,
        timeout=RESTORE_LIST_TIMEOUT_SECONDS,
    )
    if snapshots.returncode != 0:
        return _restore_error(
            reason_for_returncode(snapshots.returncode),
            returncode=snapshots.returncode,
        )

    original_path = _original_path_from_snapshots(snapshots.json)
    if original_path is None:
        return _restore_error("failed", returncode=snapshots.returncode)

    journal = get_journal()
    restore = run_restic(
        ["restore", f"latest:{original_path}", "--target", str(journal)],
        repository=destination.repository,
        password=canonical,
        restic_path=restic_path,
        backend_env=backend_env,
        json=True,
        timeout=RESTORE_TIMEOUT_SECONDS,
    )
    if restore.returncode != 0:
        return _restore_error(
            reason_for_returncode(restore.returncode),
            returncode=restore.returncode,
        )

    restored_size = _bytes_restored(restore.json)
    check = run_restic(
        ["check"],
        repository=destination.repository,
        password=canonical,
        restic_path=restic_path,
        backend_env=backend_env,
        timeout=RESTORE_CHECK_TIMEOUT_SECONDS,
    )
    integrity_ok = check.returncode == 0
    if not integrity_ok:
        logger.warning(
            "backup restore integrity check completed returncode=%s reason_code=%s",
            check.returncode,
            reason_for_returncode(check.returncode),
        )

    set_destination(destination)
    set_recovery_key(canonical)
    set_recovery_key_confirmed(True)
    daily_key = get_backup_config()["daily_key"]
    resumable = isinstance(daily_key, str) and bool(daily_key)

    scan_journal(str(journal), full=True)

    logger.info(
        "backup restore completed returncode=%s reason_code=ok",
        restore.returncode,
    )
    return RestoreResult(
        status="ok",
        reason_code=None,
        integrity_ok=integrity_ok,
        resumable=resumable,
        bytes_restored=restored_size,
    )


__all__ = [
    "RESTORE_CHECK_TIMEOUT_SECONDS",
    "RESTORE_LIST_TIMEOUT_SECONDS",
    "RESTORE_TIMEOUT_SECONDS",
    "RestoreResult",
    "restore_journal",
]
