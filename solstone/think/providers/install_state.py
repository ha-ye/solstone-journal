# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Provider install status records under journal/health/providers."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast, get_args

from solstone.think.journal_config import JournalConfigMutation, mutate_journal_config
from solstone.think.journal_io.atomic import atomic_replace
from solstone.think.journal_io.locking import hold_lock
from solstone.think.utils import get_journal

ProviderName = Literal["local", "parakeet"]
InstallState = Literal[
    "idle",
    "resolving",
    "downloading",
    "verifying",
    "installing",
    "installed",
    "failed",
]


class InstallStatus(TypedDict):
    schema_version: int
    provider: ProviderName
    revision: int
    install_state: InstallState
    attempt_id: str | None
    target_fingerprint_json: str | None
    target_fingerprint_sha256: str | None
    started_at: str | None
    last_transition_at: str | None
    last_progress_at: str | None
    completed_at: str | None
    progress_bytes_received: int | None
    progress_bytes_total: int | None
    install_error: str | None
    error_code: str | None
    owner: dict[str, Any] | None
    name: NotRequired[str]


class InstallStateError(RuntimeError):
    """Provider install status is invalid or cannot transition."""


class InstallStatusMalformedError(InstallStateError):
    """Persisted provider install status is malformed."""


class InstallStatusConflictError(InstallStateError):
    """Install status write lost a revision or attempt race."""


SCHEMA_VERSION = 1
PROVIDERS: frozenset[str] = frozenset({"local", "parakeet"})
PROGRESS_COALESCE_SECONDS = 1.0
IN_FLIGHT_STATES: frozenset[InstallState] = frozenset(
    {"resolving", "downloading", "verifying", "installing"}
)
TERMINAL_STATES: frozenset[InstallState] = frozenset({"idle", "installed", "failed"})
_INSTALL_STATES = frozenset(get_args(InstallState))
_STATUS_MODE = 0o600
_LEGACY_STATUS_KEYS = frozenset(
    {
        "install_state",
        "last_transition_at",
        "last_progress_at",
        "progress_bytes_received",
        "progress_bytes_total",
        "install_error",
    }
)
_LAST_PROGRESS_WRITE_MONOTONIC: dict[tuple[str, str], float] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_fingerprint(data: dict[str, Any]) -> str:
    """Return canonical JSON text for a provider install target fingerprint."""
    normalized = _normalize_fingerprint_value(data)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def fingerprint_sha256(fingerprint_json: str) -> str:
    return hashlib.sha256(fingerprint_json.encode("utf-8")).hexdigest()


def provider_status_path(
    provider: str,
    *,
    journal_path: str | Path | None = None,
) -> Path:
    validated = _validate_provider(provider)
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return root / "health" / "providers" / f"{validated}.json"


def make_idle_status(name: str) -> InstallStatus:
    provider = _validate_provider(name)
    return _with_legacy_name(
        {
            "schema_version": SCHEMA_VERSION,
            "provider": provider,
            "revision": 0,
            "install_state": "idle",
            "attempt_id": None,
            "target_fingerprint_json": None,
            "target_fingerprint_sha256": None,
            "started_at": None,
            "last_transition_at": None,
            "last_progress_at": None,
            "completed_at": None,
            "progress_bytes_received": None,
            "progress_bytes_total": None,
            "install_error": None,
            "error_code": None,
            "owner": None,
        }
    )


def begin_install_attempt(
    provider: str,
    fingerprint: dict[str, Any],
    *,
    initial_state: InstallState = "resolving",
    owner: dict[str, Any] | None = None,
    journal_path: str | Path | None = None,
) -> InstallStatus:
    """Start a new install attempt for provider and target fingerprint."""
    if initial_state not in IN_FLIGHT_STATES:
        raise ValueError("initial install attempt state must be in-flight")
    fingerprint_json = canonical_fingerprint(fingerprint)
    status = read_install_status(name=provider, journal_path=journal_path)
    status["target_fingerprint_json"] = fingerprint_json
    status["target_fingerprint_sha256"] = fingerprint_sha256(fingerprint_json)
    status["attempt_id"] = uuid.uuid4().hex
    status["owner"] = owner
    return write_install_status(
        transition_state(status, new_state=initial_state),
        journal_path=journal_path,
    )


def transition_state(
    status: InstallStatus,
    *,
    new_state: InstallState,
    error: str | None = None,
    error_code: str | None = None,
) -> InstallStatus:
    if new_state not in _INSTALL_STATES:
        raise ValueError(f"unknown install state: {new_state}")
    current = _coerce_status(status)
    timestamp = now_iso()
    next_attempt_id = current["attempt_id"]
    if current["install_state"] in TERMINAL_STATES and new_state in IN_FLIGHT_STATES:
        next_attempt_id = uuid.uuid4().hex
    elif next_attempt_id is None and new_state != "idle":
        next_attempt_id = uuid.uuid4().hex

    is_terminal = new_state in TERMINAL_STATES
    return _with_legacy_name(
        {
            "schema_version": SCHEMA_VERSION,
            "provider": current["provider"],
            "revision": current["revision"],
            "install_state": new_state,
            "attempt_id": None if new_state == "idle" else next_attempt_id,
            "target_fingerprint_json": current["target_fingerprint_json"],
            "target_fingerprint_sha256": current["target_fingerprint_sha256"],
            "started_at": (
                timestamp
                if current["install_state"] in TERMINAL_STATES
                and new_state in IN_FLIGHT_STATES
                else current["started_at"]
            ),
            "last_transition_at": timestamp,
            "last_progress_at": timestamp if new_state in IN_FLIGHT_STATES else None,
            "completed_at": timestamp if is_terminal and new_state != "idle" else None,
            "progress_bytes_received": (
                None if is_terminal else current["progress_bytes_received"]
            ),
            "progress_bytes_total": (
                None if is_terminal else current["progress_bytes_total"]
            ),
            "install_error": error if new_state == "failed" else None,
            "error_code": error_code if new_state == "failed" else None,
            "owner": current["owner"],
        }
    )


def bump_progress(
    status: InstallStatus,
    *,
    received: int | None = None,
    total: int | None = None,
) -> InstallStatus:
    current = _coerce_status(status)
    if current["install_state"] not in IN_FLIGHT_STATES:
        raise ValueError("install progress can only be bumped for in-flight states")
    return _with_legacy_name(
        {
            **current,
            "last_progress_at": now_iso(),
            "progress_bytes_received": (
                _nonnegative_int(received)
                if received is not None
                else current["progress_bytes_received"]
            ),
            "progress_bytes_total": (
                _nonnegative_int(total)
                if total is not None
                else current["progress_bytes_total"]
            ),
        }
    )


def read_install_status(
    *,
    scope: str = "bundled",
    name: str,
    journal_path: str | Path | None = None,
) -> InstallStatus:
    """Read provider install status; absent status is synthetic idle."""
    _validate_scope(scope)
    provider = _validate_provider(name)
    path = provider_status_path(provider, journal_path=journal_path)
    if not path.exists():
        return make_idle_status(provider)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise InstallStatusMalformedError(f"malformed install status: {path}") from exc
    if not isinstance(data, dict):
        raise InstallStatusMalformedError(f"install status must be an object: {path}")
    return _with_legacy_name(_coerce_status(data, provider=provider))


def write_install_status(
    status: InstallStatus,
    *,
    scope: str = "bundled",
    journal_path: str | Path | None = None,
) -> InstallStatus:
    """Write provider install status under a sidecar flock."""
    _validate_scope(scope)
    incoming = _coerce_status(status)
    path = provider_status_path(incoming["provider"], journal_path=journal_path)
    with hold_lock(path, mode=_STATUS_MODE):
        current = _read_current_unlocked(path, incoming["provider"])
        accepted = _accept_transition(current, incoming)
        if accepted is current:
            return _with_legacy_name(current)
        stored = {**accepted, "revision": current["revision"] + 1}
        atomic_replace(
            path,
            json.dumps(_persistable_status(stored), indent=2, sort_keys=True) + "\n",
            mode=_STATUS_MODE,
        )
        _record_progress_write(stored)
        return _with_legacy_name(stored)


def record_interrupted_install(
    provider: str,
    *,
    attempt_id: str,
    target_fingerprint_sha256: str | None,
    reason: str = "install_interrupted",
    journal_path: str | Path | None = None,
) -> InstallStatus:
    """Mark an interrupted in-flight attempt failed after the caller owns the lease."""
    current = read_install_status(name=provider, journal_path=journal_path)
    if current["install_state"] not in IN_FLIGHT_STATES:
        raise InstallStatusConflictError("only in-flight installs can be interrupted")
    if current["attempt_id"] != attempt_id:
        raise InstallStatusConflictError("interrupted attempt id does not match")
    if current["target_fingerprint_sha256"] != target_fingerprint_sha256:
        raise InstallStatusConflictError(
            "interrupted target fingerprint does not match"
        )
    return write_install_status(
        transition_state(
            current,
            new_state="failed",
            error=reason,
            error_code=reason,
        ),
        journal_path=journal_path,
    )


def migrate_legacy_provider_install_state(
    *,
    journal_path: str | Path | None = None,
) -> dict[str, int]:
    """Remove legacy provider install status fields from journal config."""

    def apply(config: dict[str, Any]) -> JournalConfigMutation[dict[str, int]]:
        removed = 0
        bundled = config.get("providers", {}).get("bundled")
        if isinstance(bundled, dict):
            for provider in PROVIDERS:
                record = bundled.get(provider)
                if not isinstance(record, dict):
                    continue
                for key in _LEGACY_STATUS_KEYS:
                    if key in record:
                        record.pop(key, None)
                        removed += 1
        return JournalConfigMutation(changed=removed > 0, value={"removed": removed})

    return mutate_journal_config(apply, journal_path=journal_path).value


def _read_current_unlocked(path: Path, provider: ProviderName) -> InstallStatus:
    if not path.exists():
        return make_idle_status(provider)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise InstallStatusMalformedError(f"malformed install status: {path}") from exc
    if not isinstance(data, dict):
        raise InstallStatusMalformedError(f"install status must be an object: {path}")
    return _with_legacy_name(_coerce_status(data, provider=provider))


def _accept_transition(
    current: InstallStatus,
    incoming: InstallStatus,
) -> InstallStatus:
    if incoming["provider"] != current["provider"]:
        raise InstallStatusConflictError("provider mismatch")

    same_attempt = (
        incoming["attempt_id"] is not None
        and incoming["attempt_id"] == current["attempt_id"]
    )
    if current["install_state"] in TERMINAL_STATES and same_attempt:
        return current
    if current["install_state"] in TERMINAL_STATES:
        if incoming["revision"] != current["revision"]:
            raise InstallStatusConflictError("stale install status revision")
        _validate_new_attempt_from_terminal(current, incoming)
        return incoming
    if current["install_state"] in IN_FLIGHT_STATES:
        if not same_attempt:
            raise InstallStatusConflictError(
                "different attempt while install in-flight"
            )
        if incoming["revision"] != current["revision"]:
            raise InstallStatusConflictError("stale install status revision")
        if incoming["install_state"] in IN_FLIGHT_STATES:
            return incoming if _should_write_in_flight(current, incoming) else current
        if incoming["install_state"] in {"installed", "failed"}:
            return incoming
    raise InstallStatusConflictError("illegal install status transition")


def _validate_new_attempt_from_terminal(
    current: InstallStatus,
    incoming: InstallStatus,
) -> None:
    if incoming["install_state"] == "idle":
        return
    if incoming["attempt_id"] is None:
        raise InstallStatusConflictError("non-idle install status requires attempt id")
    if incoming["install_state"] in IN_FLIGHT_STATES:
        if incoming["attempt_id"] == current["attempt_id"]:
            raise InstallStatusConflictError("new in-flight attempt reused attempt id")
        return
    if incoming["install_state"] in {"installed", "failed"}:
        return
    raise InstallStatusConflictError("new attempt must be in-flight or terminal")


def _should_write_in_flight(
    current: InstallStatus,
    incoming: InstallStatus,
) -> bool:
    if current["install_state"] != incoming["install_state"]:
        return True
    if current["progress_bytes_total"] != incoming["progress_bytes_total"]:
        return True
    if (
        current["progress_bytes_received"] == incoming["progress_bytes_received"]
        and current["last_progress_at"] == incoming["last_progress_at"]
    ):
        return False
    attempt_id = incoming["attempt_id"]
    if attempt_id is None:
        return True
    key = (incoming["provider"], attempt_id)
    last = _LAST_PROGRESS_WRITE_MONOTONIC.get(key)
    now = time.monotonic()
    return last is None or now - last >= PROGRESS_COALESCE_SECONDS


def _record_progress_write(status: InstallStatus) -> None:
    attempt_id = status["attempt_id"]
    if attempt_id is None or status["install_state"] not in IN_FLIGHT_STATES:
        return
    _LAST_PROGRESS_WRITE_MONOTONIC[(status["provider"], attempt_id)] = time.monotonic()


def _coerce_status(
    data: dict[str, Any] | InstallStatus,
    *,
    provider: ProviderName | None = None,
) -> InstallStatus:
    raw_provider = provider or data.get("provider") or data.get("name")
    validated_provider = _validate_provider(raw_provider)
    state = data.get("install_state")
    if state not in _INSTALL_STATES:
        raise InstallStatusMalformedError(
            f"invalid install_state for {validated_provider}"
        )
    schema_version = data.get("schema_version", SCHEMA_VERSION)
    if schema_version != SCHEMA_VERSION:
        raise InstallStatusMalformedError(
            f"unsupported install status schema_version for {validated_provider}"
        )
    revision = data.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise InstallStatusMalformedError(f"invalid revision for {validated_provider}")
    attempt_id = data.get("attempt_id")
    if attempt_id is not None and not isinstance(attempt_id, str):
        raise InstallStatusMalformedError(
            f"invalid attempt_id for {validated_provider}"
        )
    fingerprint_json = data.get("target_fingerprint_json")
    fingerprint_sha = data.get("target_fingerprint_sha256")
    if fingerprint_json is not None and not isinstance(fingerprint_json, str):
        raise InstallStatusMalformedError("target_fingerprint_json must be a string")
    if fingerprint_sha is not None and not isinstance(fingerprint_sha, str):
        raise InstallStatusMalformedError("target_fingerprint_sha256 must be a string")
    owner = data.get("owner")
    if owner is not None and not isinstance(owner, dict):
        raise InstallStatusMalformedError("owner must be an object or null")
    return _with_legacy_name(
        {
            "schema_version": SCHEMA_VERSION,
            "provider": validated_provider,
            "revision": revision,
            "install_state": cast(InstallState, state),
            "attempt_id": attempt_id,
            "target_fingerprint_json": fingerprint_json,
            "target_fingerprint_sha256": fingerprint_sha,
            "started_at": _optional_str(data.get("started_at")),
            "last_transition_at": _optional_str(data.get("last_transition_at")),
            "last_progress_at": _optional_str(data.get("last_progress_at")),
            "completed_at": _optional_str(data.get("completed_at")),
            "progress_bytes_received": _optional_int(
                data.get("progress_bytes_received")
            ),
            "progress_bytes_total": _optional_int(data.get("progress_bytes_total")),
            "install_error": _optional_str(data.get("install_error")),
            "error_code": _optional_str(data.get("error_code")),
            "owner": owner,
        }
    )


def _persistable_status(status: InstallStatus) -> dict[str, Any]:
    return {key: value for key, value in status.items() if key != "name"}


def _with_legacy_name(status: dict[str, Any]) -> InstallStatus:
    status["name"] = status["provider"]
    return cast(InstallStatus, status)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InstallStatusMalformedError("expected string or null")
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InstallStatusMalformedError("expected integer or null")
    return value


def _nonnegative_int(value: int | None) -> int | None:
    if value is None:
        return None
    result = int(value)
    return max(0, result)


def _validate_provider(value: object) -> ProviderName:
    if value not in PROVIDERS:
        raise ValueError(f"provider install status must be one of: {sorted(PROVIDERS)}")
    return cast(ProviderName, value)


def _validate_scope(scope: str) -> None:
    if scope != "bundled":
        raise ValueError("install status scope must be 'bundled'")


def _normalize_fingerprint_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalize_fingerprint_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        normalized_items = [_normalize_fingerprint_value(item) for item in value]
        return sorted(
            normalized_items,
            key=lambda item: json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
        )
    if isinstance(value, Path):
        return str(value)
    return value


__all__ = [
    "InstallState",
    "InstallStateError",
    "InstallStatus",
    "InstallStatusConflictError",
    "InstallStatusMalformedError",
    "IN_FLIGHT_STATES",
    "PROGRESS_COALESCE_SECONDS",
    "PROVIDERS",
    "ProviderName",
    "SCHEMA_VERSION",
    "TERMINAL_STATES",
    "begin_install_attempt",
    "bump_progress",
    "canonical_fingerprint",
    "fingerprint_sha256",
    "make_idle_status",
    "migrate_legacy_provider_install_state",
    "now_iso",
    "provider_status_path",
    "read_install_status",
    "record_interrupted_install",
    "transition_state",
    "write_install_status",
]
