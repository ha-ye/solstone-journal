# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Backup app routes."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from flask import Blueprint, Response, jsonify, render_template, request

from solstone.apps.backup.copy import backup_copy_payload
from solstone.convey.reasons import (
    BACKUP_BUSY,
    BACKUP_NOT_CONFIRMED,
    BACKUP_OPERATION_FAILED,
    BACKUP_UNAVAILABLE,
    INVALID_CONFIG_VALUE,
    INVALID_OPERATION_FOR_STATE,
    INVALID_REQUEST_VALUE,
    MISSING_REQUIRED_FIELD,
    RECOVERY_KEY_MISMATCH,
)
from solstone.convey.utils import error_response, success_response
from solstone.think.backup.destination import (
    Destination,
    DestinationStatus,
    validate_destination,
)
from solstone.think.backup.engine import request_backup_now
from solstone.think.backup.install import ensure_restic
from solstone.think.backup.keys import confirm_recovery_key, generate_daily_key
from solstone.think.backup.repo import ResticKeyError, init_repository
from solstone.think.backup.restore import restore_journal
from solstone.think.backup.rotation import rotate_recovery_key
from solstone.think.backup.runner import reason_for_returncode
from solstone.think.backup.state import (
    generate_and_store_keys,
    get_destination,
    get_keys,
    set_destination,
    set_enabled,
    set_recovery_key_confirmed,
    set_retention,
    status_view,
)
from solstone.think.backup.teardown import teardown_backup

logger = logging.getLogger(__name__)

backup_bp = Blueprint(
    "app:backup",
    __name__,
    url_prefix="/app/backup",
    static_folder="static",
    static_url_path="/static",
)

OPERATION_KEY = "backup"
OPERATION_GRACE_SECONDS = 30.0
ENABLE_TIMEOUT = 30.0
DESTINATION_PROBE_TIMEOUT = 30.0
RETENTION_KEYS = ("hourly", "daily", "weekly", "monthly")
S3_REQUIRED = ("access_key_id", "secret_access_key")
B2_REQUIRED = ("account_id", "account_key")


@dataclass
class OperationEntry:
    kind: str
    phase: str
    reason_code: str | None
    started_monotonic: float
    ended_monotonic: float | None = None


@dataclass(frozen=True)
class OpOutcome:
    status: str
    reason_code: str | None


_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, OperationEntry] = {}


def _clear_registry() -> None:
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def _sweep_operations_locked(now: float) -> None:
    entry = _REGISTRY.get(OPERATION_KEY)
    if (
        entry is not None
        and entry.ended_monotonic is not None
        and entry.ended_monotonic + OPERATION_GRACE_SECONDS < now
    ):
        _REGISTRY.pop(OPERATION_KEY, None)


def _active_operation_locked() -> OperationEntry | None:
    entry = _REGISTRY.get(OPERATION_KEY)
    if entry is None or entry.ended_monotonic is not None:
        return None
    return entry


def _operation_payload(
    entry: OperationEntry,
    now: float | None = None,
) -> dict[str, Any]:
    ts = time.monotonic() if now is None else now
    if entry.ended_monotonic is not None:
        ts = entry.ended_monotonic
    return {
        "kind": entry.kind,
        "phase": entry.phase,
        "reason_code": entry.reason_code,
        "elapsed_ms": int(max(0.0, ts - entry.started_monotonic) * 1000),
    }


def _current_operation() -> dict[str, Any] | None:
    with _REGISTRY_LOCK:
        now = time.monotonic()
        _sweep_operations_locked(now)
        entry = _REGISTRY.get(OPERATION_KEY)
        return _operation_payload(entry, now) if entry is not None else None


def _status_snapshot() -> dict[str, Any]:
    return {**status_view(), "operation": _current_operation()}


def _run_long_op(entry: OperationEntry, thunk: Callable[[], OpOutcome]) -> None:
    try:
        outcome = thunk()
    except Exception:
        logger.exception("backup operation failed")
        outcome = OpOutcome("error", "failed")

    with _REGISTRY_LOCK:
        current = _REGISTRY.get(OPERATION_KEY)
        if current is not entry:
            return
        if outcome.status in {"ok", "done", "skipped"}:
            entry.phase = "done"
            entry.reason_code = None
        else:
            entry.phase = "error"
            entry.reason_code = outcome.reason_code or "failed"
        entry.ended_monotonic = time.monotonic()


def _start_long_op(
    kind: str,
    phase: str,
    thunk: Callable[[], OpOutcome],
) -> tuple[dict[str, Any], int] | tuple[Response, int]:
    with _REGISTRY_LOCK:
        now = time.monotonic()
        _sweep_operations_locked(now)
        if _active_operation_locked() is not None:
            return error_response(BACKUP_BUSY)
        entry = OperationEntry(
            kind=kind,
            phase=phase,
            reason_code=None,
            started_monotonic=now,
        )
        _REGISTRY[OPERATION_KEY] = entry
        operation = _operation_payload(entry, now)

    thread = threading.Thread(target=_run_long_op, args=(entry, thunk), daemon=True)
    thread.start()
    return {"success": True, "operation": operation}, 202


def _json_body() -> dict[str, Any] | None:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else None


def _required_string(
    payload: dict[str, Any],
    key: str,
) -> str | tuple[Response, int]:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        return error_response(MISSING_REQUIRED_FIELD, detail=f"missing {key}")
    return value.strip()


def _credential_source(payload: dict[str, Any]) -> dict[str, Any]:
    credentials = payload.get("credentials")
    return credentials if isinstance(credentials, dict) else payload


def _destination_from_payload() -> Destination | tuple[Response, int]:
    payload = _json_body()
    if payload is None:
        return error_response(MISSING_REQUIRED_FIELD, detail="missing request body")

    repository = _required_string(payload, "repository")
    if not isinstance(repository, str):
        return repository

    backend = _required_string(payload, "backend")
    if not isinstance(backend, str):
        return backend
    if backend not in {"s3", "b2"}:
        return error_response(INVALID_REQUEST_VALUE, detail="unsupported backend")

    source = _credential_source(payload)
    required = S3_REQUIRED if backend == "s3" else B2_REQUIRED
    credentials: dict[str, str] = {}
    for key in required:
        value = source.get(key)
        if not isinstance(value, str) or not value.strip():
            return error_response(MISSING_REQUIRED_FIELD, detail=f"missing {key}")
        credentials[key] = value.strip()

    return Destination(repository=repository, backend=backend, credentials=credentials)


def _destination_status_payload(status: DestinationStatus) -> dict[str, Any]:
    return {
        "reachable": status.reachable,
        "repo_exists": status.repo_exists,
        "reason_code": status.reason_code,
        "message": status.message,
    }


def _result_outcome(result: Any) -> OpOutcome:
    return OpOutcome(
        status=str(getattr(result, "status", "error")),
        reason_code=getattr(result, "reason_code", None),
    )


def _enable_thunk() -> OpOutcome:
    destination = get_destination()
    keys = get_keys()
    if destination is None or keys is None:
        return OpOutcome("error", "failed")

    try:
        restic_path = ensure_restic()
    except Exception:
        logger.exception("backup restic setup failed")
        return OpOutcome("error", "restic_unavailable")

    try:
        set_enabled(True)
    except Exception:
        logger.exception("backup enable flag update failed")
        return OpOutcome("error", "failed")

    try:
        init_repository(
            destination,
            daily_key=keys.daily_key,
            recovery_key=keys.recovery_key,
            restic_path=restic_path,
            timeout=ENABLE_TIMEOUT,
        )
    except ResticKeyError as exc:
        return OpOutcome("error", reason_for_returncode(exc.returncode))
    except RuntimeError:
        logger.exception("backup repository setup failed")
        return OpOutcome("error", "failed")

    if not request_backup_now():
        logger.warning("backup request could not be queued after setup")
    return OpOutcome("ok", None)


@backup_bp.route("/")
def index() -> str:
    return render_template(
        "app.html",
        backup_copy=backup_copy_payload(),
        backup_initial=_status_snapshot(),
    )


@backup_bp.route("/status")
def status() -> tuple[Response, int]:
    return jsonify({"success": True, **_status_snapshot()}), 200


@backup_bp.route("/keys/generate", methods=["POST"])
def generate_keys() -> tuple[Any, int]:
    keys = generate_and_store_keys()
    return success_response({"recovery_key_display": keys.recovery_key_display})


@backup_bp.route("/recovery-key/reveal", methods=["POST"])
def reveal_recovery_key() -> tuple[Any, int]:
    keys = get_keys()
    if keys is None:
        return error_response(
            INVALID_OPERATION_FOR_STATE,
            detail="no recovery key yet",
        )
    return success_response({"recovery_key_display": keys.recovery_key_display})


@backup_bp.route("/confirm", methods=["POST"])
def confirm_recovery() -> tuple[Any, int]:
    payload = _json_body()
    if payload is None:
        return error_response(MISSING_REQUIRED_FIELD, detail="missing request body")
    entered = payload.get("recovery_key")
    if not isinstance(entered, str) or not entered.strip():
        return error_response(MISSING_REQUIRED_FIELD, detail="missing recovery_key")

    keys = get_keys()
    if keys is None:
        return error_response(
            INVALID_OPERATION_FOR_STATE,
            detail="no recovery key yet",
        )
    if not confirm_recovery_key(entered, keys.recovery_key):
        return error_response(RECOVERY_KEY_MISMATCH)

    set_recovery_key_confirmed(True)
    return success_response(_status_snapshot())


@backup_bp.route("/enable", methods=["POST"])
def enable() -> tuple[dict[str, Any], int] | tuple[Response, int]:
    if get_destination() is None:
        return error_response(
            INVALID_OPERATION_FOR_STATE,
            detail="configure a destination first",
        )
    if not status_view()["recovery_key_confirmed"]:
        return error_response(BACKUP_NOT_CONFIRMED)
    if get_keys() is None:
        return error_response(
            INVALID_OPERATION_FOR_STATE,
            detail="no recovery key yet",
        )

    return _start_long_op("enable", "setting_up", _enable_thunk)


@backup_bp.route("/destination", methods=["POST"])
def destination() -> tuple[Any, int]:
    destination_result = _destination_from_payload()
    if isinstance(destination_result, tuple):
        return destination_result

    set_destination(destination_result)
    keys = get_keys()
    password = keys.daily_key if keys is not None else generate_daily_key()
    try:
        destination_status = validate_destination(
            destination_result,
            password,
            restic_path=ensure_restic(),
            timeout=DESTINATION_PROBE_TIMEOUT,
        )
    except Exception:
        logger.exception("backup destination probe failed")
        return error_response(BACKUP_OPERATION_FAILED)

    return success_response(
        {
            **_status_snapshot(),
            "destination_status": _destination_status_payload(destination_status),
        }
    )


@backup_bp.route("/backup-now", methods=["POST"])
def backup_now() -> tuple[Any, int]:
    if not request_backup_now():
        return error_response(BACKUP_UNAVAILABLE)
    return success_response(_status_snapshot())


@backup_bp.route("/recovery-key/rotate", methods=["POST"])
def rotate_recovery() -> tuple[dict[str, Any], int] | tuple[Response, int]:
    return _start_long_op(
        "rotate",
        "rotating",
        lambda: _result_outcome(rotate_recovery_key()),
    )


@backup_bp.route("/retention", methods=["POST"])
def retention() -> tuple[Any, int]:
    payload = _json_body()
    if payload is None:
        return error_response(INVALID_CONFIG_VALUE, detail="missing request body")
    try:
        set_retention({key: payload[key] for key in RETENTION_KEYS})
    except (KeyError, TypeError, ValueError):
        return error_response(INVALID_CONFIG_VALUE)
    return success_response(_status_snapshot())


@backup_bp.route("/teardown", methods=["POST"])
def teardown() -> tuple[dict[str, Any], int] | tuple[Response, int]:
    return _start_long_op(
        "teardown",
        "tearing_down",
        lambda: _result_outcome(teardown_backup()),
    )


@backup_bp.route("/restore", methods=["POST"])
def restore() -> tuple[dict[str, Any], int] | tuple[Response, int]:
    payload = _json_body()
    if payload is None:
        return error_response(MISSING_REQUIRED_FIELD, detail="missing request body")
    entered = payload.get("recovery_key")
    if not isinstance(entered, str) or not entered.strip():
        return error_response(MISSING_REQUIRED_FIELD, detail="missing recovery_key")

    destination_result = _destination_from_payload()
    if isinstance(destination_result, tuple):
        return destination_result

    recovery_key = entered.strip()
    return _start_long_op(
        "restore",
        "restoring",
        lambda: _result_outcome(restore_journal(destination_result, recovery_key)),
    )
