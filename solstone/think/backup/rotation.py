# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Recovery-key rotation engine hook for solstone backup."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from solstone.think.backup.destination import assemble_backend_env, validate_destination
from solstone.think.backup.install import ensure_restic
from solstone.think.backup.keys import (
    confirm_recovery_key,
    format_recovery_key_display,
    generate_recovery_key,
)
from solstone.think.backup.repo import (
    ResticKeyError,
    _add_recovery_key,
    _capture_current_key_id,
    _remove_key,
)
from solstone.think.backup.runner import reason_for_returncode
from solstone.think.backup.state import (
    get_destination,
    get_keys,
    set_recovery_key,
    set_recovery_key_confirmed,
)

logger = logging.getLogger("solstone.backup.rotation")

ROTATION_TIMEOUT_SECONDS = 5 * 60


@dataclass(frozen=True)
class RotationResult:
    status: str
    reason_code: str | None
    recovery_key: str | None
    recovery_key_display: str | None


def _rotation_error(
    reason_code: str,
    *,
    returncode: int | None = None,
) -> RotationResult:
    logger.warning(
        "backup recovery-key rotation completed returncode=%s reason_code=%s",
        returncode,
        reason_code,
    )
    return RotationResult(
        status="error",
        reason_code=reason_code,
        recovery_key=None,
        recovery_key_display=None,
    )


def _restic_key_error(exc: ResticKeyError) -> RotationResult:
    return _rotation_error(
        reason_for_returncode(exc.returncode),
        returncode=exc.returncode,
    )


def rotate_recovery_key() -> RotationResult:
    destination = get_destination()
    keys = get_keys()
    if destination is None or keys is None:
        return RotationResult(
            status="skipped",
            reason_code=None,
            recovery_key=None,
            recovery_key_display=None,
        )

    try:
        restic_path = ensure_restic()
    except Exception:
        return _rotation_error("restic_unavailable")

    try:
        assemble_backend_env(destination)
    except (KeyError, ValueError):
        return _rotation_error("failed")

    try:
        old_id = _capture_current_key_id(
            destination,
            password=keys.recovery_key,
            restic_path=restic_path,
            timeout=ROTATION_TIMEOUT_SECONDS,
        )
    except ResticKeyError as exc:
        return _restic_key_error(exc)
    except RuntimeError:
        return _rotation_error("failed")

    new_canonical = generate_recovery_key()
    try:
        _add_recovery_key(
            destination,
            daily_key=keys.daily_key,
            recovery_key=new_canonical,
            restic_path=restic_path,
            timeout=ROTATION_TIMEOUT_SECONDS,
        )
    except ResticKeyError as exc:
        return _restic_key_error(exc)
    except RuntimeError:
        return _rotation_error("failed")

    new_display = format_recovery_key_display(new_canonical)
    if not confirm_recovery_key(new_display, new_canonical):
        return _rotation_error("failed")

    status = validate_destination(
        destination,
        new_canonical,
        restic_path=restic_path,
        timeout=ROTATION_TIMEOUT_SECONDS,
    )
    if not (status.repo_exists and status.reason_code == "repo_exists"):
        return _rotation_error(status.reason_code or "failed")

    try:
        _remove_key(
            destination,
            password=keys.daily_key,
            key_id=old_id,
            restic_path=restic_path,
            timeout=ROTATION_TIMEOUT_SECONDS,
        )
    except ResticKeyError as exc:
        return _restic_key_error(exc)
    except RuntimeError:
        return _rotation_error("failed")

    set_recovery_key(new_canonical)
    set_recovery_key_confirmed(False)
    logger.info(
        "backup recovery-key rotation completed returncode=%s reason_code=ok",
        0,
    )
    return RotationResult(
        status="ok",
        reason_code=None,
        recovery_key=new_canonical,
        recovery_key_display=new_display,
    )


__all__ = [
    "ROTATION_TIMEOUT_SECONDS",
    "RotationResult",
    "rotate_recovery_key",
]
