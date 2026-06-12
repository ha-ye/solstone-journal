# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Turn Off & Delete engine hook for sol private backup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from solstone.think.backup.destination import assemble_backend_env
from solstone.think.backup.install import ensure_restic
from solstone.think.backup.runner import reason_for_returncode, run_restic
from solstone.think.backup.state import clear_backup_config, get_destination, get_keys

logger = logging.getLogger("solstone.backup.teardown")

TEARDOWN_TIMEOUT_SECONDS = 2 * 60 * 60


@dataclass(frozen=True)
class TeardownResult:
    status: str
    reason_code: str | None


def _teardown_error(
    reason_code: str,
    *,
    returncode: int | None = None,
) -> TeardownResult:
    logger.warning(
        "backup teardown completed returncode=%s reason_code=%s",
        returncode,
        reason_code,
    )
    return TeardownResult(status="error", reason_code=reason_code)


def _snapshot_ids(parsed: Any) -> list[str] | None:
    if not isinstance(parsed, list):
        return None
    ids: list[str] = []
    for record in parsed:
        if not isinstance(record, dict):
            return None
        snapshot_id = record.get("id")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            return None
        ids.append(snapshot_id)
    return ids


def teardown_backup() -> TeardownResult:
    destination = get_destination()
    keys = get_keys()
    if destination is None or keys is None:
        return TeardownResult(status="skipped", reason_code=None)

    try:
        backend_env = assemble_backend_env(destination)
    except (KeyError, ValueError):
        return _teardown_error("failed")

    try:
        restic_path = ensure_restic()
    except Exception:
        return _teardown_error("restic_unavailable")

    snapshots = run_restic(
        ["snapshots"],
        repository=destination.repository,
        password=keys.daily_key,
        restic_path=restic_path,
        backend_env=backend_env,
        json=True,
        timeout=TEARDOWN_TIMEOUT_SECONDS,
    )
    if snapshots.returncode != 0:
        return _teardown_error(
            reason_for_returncode(snapshots.returncode),
            returncode=snapshots.returncode,
        )

    ids = _snapshot_ids(snapshots.json)
    if ids is None:
        return _teardown_error("failed", returncode=snapshots.returncode)

    if ids:
        forget = run_restic(
            ["forget", *ids, "--prune"],
            repository=destination.repository,
            password=keys.daily_key,
            restic_path=restic_path,
            backend_env=backend_env,
            timeout=TEARDOWN_TIMEOUT_SECONDS,
        )
        if forget.returncode != 0:
            return _teardown_error(
                reason_for_returncode(forget.returncode),
                returncode=forget.returncode,
            )

    clear_backup_config()
    logger.info("backup teardown completed returncode=%s reason_code=ok", 0)
    return TeardownResult(status="ok", reason_code=None)


__all__ = [
    "TEARDOWN_TIMEOUT_SECONDS",
    "TeardownResult",
    "teardown_backup",
]
