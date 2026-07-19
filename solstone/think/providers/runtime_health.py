# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Provider runtime health and retry-token owner records."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict, cast, get_args

from solstone.think.journal_io.atomic import atomic_replace
from solstone.think.journal_io.errors import LockTimeout
from solstone.think.journal_io.locking import hold_lock
from solstone.think.providers.install_state import PROVIDERS, ProviderName
from solstone.think.utils import get_journal

RuntimePhase = Literal[
    "not-desired",
    "observing",
    "artifact-not-ready",
    "host-blocked",
    "starting",
    "warming",
    "backoff",
    "retry-requested",
    "ready",
    "ready-proof-unavailable",
    "stop-deferred",
    "stopping",
    "stopped",
    "failed",
    "cleanup-failed",
    "state-corrupt",
    "state-unavailable",
]
ReasonCode = Literal[
    "intent-disabled",
    "intent-enabled",
    "provider-not-needed",
    "truth-observation-started",
    "truth-observation-failed",
    "observation-raced",
    "proof-observation-unavailable",
    "install-idle",
    "install-in-progress",
    "artifact-missing",
    "artifact-stale",
    "artifact-proof-failed",
    "host-admission-blocked",
    "platform-unsupported",
    "package-unavailable",
    "ram-insufficient",
    "gpu-probe-failed",
    "gpu-unavailable",
    "confidential-backend-selected",
    "launch-requested",
    "launch-spawned",
    "launch-failed",
    "warmup-timeout",
    "process-exited",
    "probe-not-ready",
    "retry-scheduled",
    "retry-token-requested",
    "launch-budget-exhausted",
    "local-wedge-provider-unavailable",
    "target-changed",
    "intent-removed",
    "duplicate-owned-process",
    "admission-exclusive-stop",
    "cleanup-succeeded",
    "cleanup-attempt-failed",
    "probe-ready",
    "ready-existing-owned-process",
    "ready-with-proof-observation-unavailable",
    "record-malformed",
    "record-unavailable",
    "stale-result-ignored",
]
RecordKind = Literal["health", "retry-token"]
InspectionStatus = Literal["ok", "corrupt", "unavailable"]


class RuntimeHealthRecord(TypedDict):
    schema_version: int
    provider: ProviderName
    revision: int
    phase: RuntimePhase
    reason_code: ReasonCode | None
    detail: dict[str, Any]
    desired_fingerprint_sha256: str | None
    incarnation: str | None
    generation: int
    attempt: int
    process: dict[str, Any] | None
    updated_at: str | None
    display_deadline_at: str | None
    owner: dict[str, Any] | None


class RuntimeRetryTokenRecord(TypedDict):
    schema_version: int
    provider: ProviderName
    revision: int
    token_id: str | None
    desired_fingerprint_sha256: str | None
    requested_at: str | None
    reason_code: ReasonCode | None
    owner: dict[str, Any] | None


class RuntimeRecordInspection(TypedDict):
    status: InspectionStatus
    provider: ProviderName
    record_kind: RecordKind
    path: str
    record: RuntimeHealthRecord | RuntimeRetryTokenRecord | None
    reason_code: ReasonCode | None
    error: str | None


class RuntimeRepairObservation(RuntimeRecordInspection, total=False):
    repair_handle: str


class RuntimeHealthError(RuntimeError):
    """Runtime health owner operation failed."""


class RuntimeHealthMalformedError(RuntimeHealthError):
    """Persisted runtime health owner record is malformed."""


class RuntimeHealthUnavailableError(RuntimeHealthError):
    """Runtime health owner record is unavailable."""


class RuntimeHealthConflictError(RuntimeHealthError):
    """Runtime health owner write lost a revision or fingerprint race."""


SCHEMA_VERSION = 1
RUNTIME_RECORD_MODE = 0o600
REPAIR_HANDLE_DOMAIN = "solstone-runtime-health-repair-v1"
RUNTIME_PHASES: frozenset[str] = frozenset(get_args(RuntimePhase))
REASON_CODE_GROUPS: dict[str, frozenset[str]] = {
    "intent": frozenset(
        {
            "intent-disabled",
            "intent-enabled",
            "provider-not-needed",
        }
    ),
    "observation": frozenset(
        {
            "truth-observation-started",
            "truth-observation-failed",
            "observation-raced",
            "proof-observation-unavailable",
        }
    ),
    "artifacts": frozenset(
        {
            "install-idle",
            "install-in-progress",
            "artifact-missing",
            "artifact-stale",
            "artifact-proof-failed",
        }
    ),
    "host": frozenset(
        {
            "host-admission-blocked",
            "platform-unsupported",
            "package-unavailable",
            "ram-insufficient",
            "gpu-probe-failed",
            "gpu-unavailable",
            "confidential-backend-selected",
        }
    ),
    "start": frozenset(
        {
            "launch-requested",
            "launch-spawned",
            "launch-failed",
            "warmup-timeout",
            "process-exited",
            "probe-not-ready",
        }
    ),
    "retry": frozenset(
        {
            "retry-scheduled",
            "retry-token-requested",
            "launch-budget-exhausted",
            "local-wedge-provider-unavailable",
        }
    ),
    "stop": frozenset(
        {
            "target-changed",
            "intent-removed",
            "duplicate-owned-process",
            "admission-exclusive-stop",
            "cleanup-succeeded",
            "cleanup-attempt-failed",
        }
    ),
    "ready": frozenset(
        {
            "probe-ready",
            "ready-existing-owned-process",
            "ready-with-proof-observation-unavailable",
        }
    ),
    "state": frozenset(
        {
            "record-malformed",
            "record-unavailable",
            "stale-result-ignored",
        }
    ),
}
REASON_CODES: frozenset[str] = frozenset().union(*REASON_CODE_GROUPS.values())

if RUNTIME_PHASES & REASON_CODES:
    raise RuntimeError("runtime health phase and reason-code vocabularies overlap")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def runtime_directory(*, journal_path: str | Path | None = None) -> Path:
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return root / "health" / "providers" / "runtime"


def runtime_health_path(
    provider: str,
    *,
    journal_path: str | Path | None = None,
) -> Path:
    validated = _validate_provider(provider)
    return runtime_directory(journal_path=journal_path) / f"{validated}.json"


def runtime_retry_token_path(
    provider: str,
    *,
    journal_path: str | Path | None = None,
) -> Path:
    validated = _validate_provider(provider)
    return (
        runtime_directory(journal_path=journal_path) / f"{validated}.retry-token.json"
    )


def runtime_operation_path(
    provider: str,
    *,
    journal_path: str | Path | None = None,
) -> Path:
    validated = _validate_provider(provider)
    return runtime_directory(journal_path=journal_path) / f"{validated}.operation"


def runtime_operation_lock_path(
    provider: str,
    *,
    journal_path: str | Path | None = None,
) -> Path:
    operation = runtime_operation_path(provider, journal_path=journal_path)
    return operation.parent / f"{operation.name}.lock"


def make_synthetic_runtime_health(provider: str) -> RuntimeHealthRecord:
    validated = _validate_provider(provider)
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": validated,
        "revision": 0,
        "phase": "stopped",
        "reason_code": None,
        "detail": {},
        "desired_fingerprint_sha256": None,
        "incarnation": None,
        "generation": 0,
        "attempt": 0,
        "process": None,
        "updated_at": None,
        "display_deadline_at": None,
        "owner": None,
    }


def make_synthetic_retry_token(provider: str) -> RuntimeRetryTokenRecord:
    validated = _validate_provider(provider)
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": validated,
        "revision": 0,
        "token_id": None,
        "desired_fingerprint_sha256": None,
        "requested_at": None,
        "reason_code": None,
        "owner": None,
    }


def read_runtime_health(
    provider: str,
    *,
    journal_path: str | Path | None = None,
) -> RuntimeHealthRecord:
    """Read runtime health; absent records are synthetic and read-only."""
    validated = _validate_provider(provider)
    path = runtime_health_path(validated, journal_path=journal_path)
    return _read_health_unlocked(path, validated)


def read_retry_token(
    provider: str,
    *,
    journal_path: str | Path | None = None,
) -> RuntimeRetryTokenRecord:
    """Read retry-token state; absent records are synthetic and read-only."""
    validated = _validate_provider(provider)
    path = runtime_retry_token_path(validated, journal_path=journal_path)
    return _read_retry_unlocked(path, validated)


def inspect_runtime_health(
    provider: str,
    *,
    journal_path: str | Path | None = None,
) -> RuntimeRecordInspection:
    """Inspect health record readability without exposing repair handles."""
    validated = _validate_provider(provider)
    path = runtime_health_path(validated, journal_path=journal_path)
    try:
        record = _read_health_unlocked(path, validated)
    except RuntimeHealthMalformedError as exc:
        return _inspection(
            status="corrupt",
            provider=validated,
            record_kind="health",
            path=path,
            reason_code="record-malformed",
            error=str(exc),
        )
    except RuntimeHealthUnavailableError as exc:
        return _inspection(
            status="unavailable",
            provider=validated,
            record_kind="health",
            path=path,
            reason_code="record-unavailable",
            error=str(exc),
        )
    return _inspection(
        status="ok",
        provider=validated,
        record_kind="health",
        path=path,
        record=record,
    )


def inspect_retry_token(
    provider: str,
    *,
    journal_path: str | Path | None = None,
) -> RuntimeRecordInspection:
    """Inspect retry-token readability without exposing repair handles."""
    validated = _validate_provider(provider)
    path = runtime_retry_token_path(validated, journal_path=journal_path)
    try:
        record = _read_retry_unlocked(path, validated)
    except RuntimeHealthMalformedError as exc:
        return _inspection(
            status="corrupt",
            provider=validated,
            record_kind="retry-token",
            path=path,
            reason_code="record-malformed",
            error=str(exc),
        )
    except RuntimeHealthUnavailableError as exc:
        return _inspection(
            status="unavailable",
            provider=validated,
            record_kind="retry-token",
            path=path,
            reason_code="record-unavailable",
            error=str(exc),
        )
    return _inspection(
        status="ok",
        provider=validated,
        record_kind="retry-token",
        path=path,
        record=record,
    )


def write_runtime_health(
    record: RuntimeHealthRecord,
    *,
    expected_desired_fingerprint_sha256: str | None = None,
    journal_path: str | Path | None = None,
) -> RuntimeHealthRecord:
    """Write runtime health under the provider operation lock."""
    incoming = _coerce_health(record)
    provider = incoming["provider"]
    path = runtime_health_path(provider, journal_path=journal_path)
    lock_target = runtime_operation_path(provider, journal_path=journal_path)
    try:
        with hold_lock(lock_target, mode=RUNTIME_RECORD_MODE):
            current = _read_health_unlocked(path, provider)
            _assert_expected(
                current,
                incoming["revision"],
                expected_desired_fingerprint_sha256,
            )
            stored = {**incoming, "revision": current["revision"] + 1}
            _write_json_unlocked(path, _persistable_health(stored))
            return stored
    except LockTimeout as exc:
        raise RuntimeHealthUnavailableError(
            f"runtime health lock unavailable: {lock_target}"
        ) from exc
    except OSError as exc:
        raise RuntimeHealthUnavailableError(
            f"runtime health write unavailable: {path}"
        ) from exc


def request_retry_token(
    provider: str,
    *,
    desired_fingerprint_sha256: str | None,
    reason_code: str = "retry-token-requested",
    owner: dict[str, Any] | None = None,
    journal_path: str | Path | None = None,
) -> RuntimeRetryTokenRecord:
    """Record or coalesce one outstanding retry token."""
    validated = _validate_provider(provider)
    reason = _optional_reason_code(reason_code)
    _validate_owner(owner)
    path = runtime_retry_token_path(validated, journal_path=journal_path)
    lock_target = runtime_operation_path(validated, journal_path=journal_path)
    try:
        with hold_lock(lock_target, mode=RUNTIME_RECORD_MODE):
            current = _read_retry_unlocked(path, validated)
            if (
                current["token_id"] is not None
                and current["desired_fingerprint_sha256"] == desired_fingerprint_sha256
            ):
                token_id = current["token_id"]
            else:
                token_id = uuid.uuid4().hex
            stored: RuntimeRetryTokenRecord = {
                "schema_version": SCHEMA_VERSION,
                "provider": validated,
                "revision": current["revision"] + 1,
                "token_id": token_id,
                "desired_fingerprint_sha256": desired_fingerprint_sha256,
                "requested_at": now_iso(),
                "reason_code": reason,
                "owner": owner,
            }
            _write_json_unlocked(path, _persistable_retry(stored))
            return stored
    except LockTimeout as exc:
        raise RuntimeHealthUnavailableError(
            f"runtime retry-token lock unavailable: {lock_target}"
        ) from exc
    except OSError as exc:
        raise RuntimeHealthUnavailableError(
            f"runtime retry-token write unavailable: {path}"
        ) from exc


def request_runtime_retry(
    provider: str,
    *,
    expected_health_revision: int,
    expected_retry_revision: int,
    desired_fingerprint_sha256: str,
    owner: dict[str, Any] | None = None,
    journal_path: str | Path | None = None,
) -> RuntimeRetryTokenRecord:
    """Request one retry for a current terminal runtime failure.

    This is the owner-facing compare-and-set operation. Internal supervisor
    recovery may still use ``request_retry_token`` directly, but routes must
    not compose health and token reads around that lower-level primitive.
    """
    validated = _validate_provider(provider)
    _validate_owner(owner)
    health_path = runtime_health_path(validated, journal_path=journal_path)
    retry_path = runtime_retry_token_path(validated, journal_path=journal_path)
    lock_target = runtime_operation_path(validated, journal_path=journal_path)
    try:
        with hold_lock(lock_target, mode=RUNTIME_RECORD_MODE):
            health = _read_health_unlocked(health_path, validated)
            retry = _read_retry_unlocked(retry_path, validated)
            if health["revision"] != expected_health_revision:
                raise RuntimeHealthConflictError("stale runtime health revision")
            if retry["revision"] != expected_retry_revision:
                raise RuntimeHealthConflictError("stale retry-token revision")
            if health["desired_fingerprint_sha256"] != desired_fingerprint_sha256:
                raise RuntimeHealthConflictError("runtime desired fingerprint changed")
            if health["phase"] != "failed":
                raise RuntimeHealthConflictError(
                    "runtime retry requires a terminal failure"
                )
            if (
                retry["token_id"] is not None
                and retry["desired_fingerprint_sha256"] == desired_fingerprint_sha256
            ):
                raise RuntimeHealthConflictError("runtime retry already requested")

            stored: RuntimeRetryTokenRecord = {
                "schema_version": SCHEMA_VERSION,
                "provider": validated,
                "revision": retry["revision"] + 1,
                "token_id": uuid.uuid4().hex,
                "desired_fingerprint_sha256": desired_fingerprint_sha256,
                "requested_at": now_iso(),
                "reason_code": "retry-token-requested",
                "owner": owner,
            }
            _write_json_unlocked(retry_path, _persistable_retry(stored))
            return stored
    except LockTimeout as exc:
        raise RuntimeHealthUnavailableError(
            f"runtime retry lock unavailable: {lock_target}"
        ) from exc
    except OSError as exc:
        raise RuntimeHealthUnavailableError(
            f"runtime retry write unavailable: {retry_path}"
        ) from exc


def consume_retry_token(
    provider: str,
    *,
    token_id: str,
    revision: int,
    desired_fingerprint_sha256: str | None,
    journal_path: str | Path | None = None,
) -> RuntimeRetryTokenRecord:
    """Atomically consume a matching outstanding retry token."""
    validated = _validate_provider(provider)
    path = runtime_retry_token_path(validated, journal_path=journal_path)
    lock_target = runtime_operation_path(validated, journal_path=journal_path)
    try:
        with hold_lock(lock_target, mode=RUNTIME_RECORD_MODE):
            current = _read_retry_unlocked(path, validated)
            if current["revision"] != revision:
                raise RuntimeHealthConflictError("stale retry-token revision")
            if current["token_id"] != token_id:
                raise RuntimeHealthConflictError("retry-token id changed")
            if current["desired_fingerprint_sha256"] != desired_fingerprint_sha256:
                raise RuntimeHealthConflictError(
                    "retry-token desired fingerprint changed"
                )
            stored = {
                **make_synthetic_retry_token(validated),
                "revision": current["revision"] + 1,
            }
            _write_json_unlocked(path, _persistable_retry(stored))
            return stored
    except LockTimeout as exc:
        raise RuntimeHealthUnavailableError(
            f"runtime retry-token lock unavailable: {lock_target}"
        ) from exc
    except OSError as exc:
        raise RuntimeHealthUnavailableError(
            f"runtime retry-token write unavailable: {path}"
        ) from exc


def observe_runtime_repair(
    provider: str,
    *,
    record_kind: RecordKind,
    journal_path: str | Path | None = None,
) -> RuntimeRepairObservation:
    """Observe a record for explicit repair; only this API returns the handle."""
    validated = _validate_provider(provider)
    kind = _validate_record_kind(record_kind)
    path = _record_path(validated, kind, journal_path=journal_path)
    try:
        raw = _read_record_bytes(path)
    except FileNotFoundError:
        record: RuntimeHealthRecord | RuntimeRetryTokenRecord
        record = (
            make_synthetic_runtime_health(validated)
            if kind == "health"
            else make_synthetic_retry_token(validated)
        )
        return _repair_observation(
            status="ok",
            provider=validated,
            record_kind=kind,
            path=path,
            record=record,
        )
    except OSError as exc:
        return _repair_observation(
            status="unavailable",
            provider=validated,
            record_kind=kind,
            path=path,
            reason_code="record-unavailable",
            error=f"runtime {kind} record unavailable: {path}: {exc}",
        )

    try:
        record = _coerce_record_bytes(raw, provider=validated, record_kind=kind)
    except RuntimeHealthMalformedError as exc:
        try:
            stat_result = path.stat()
        except OSError as stat_exc:
            return _repair_observation(
                status="unavailable",
                provider=validated,
                record_kind=kind,
                path=path,
                reason_code="record-unavailable",
                error=f"runtime {kind} record stat unavailable: {path}: {stat_exc}",
            )
        repair_handle = _derive_repair_handle(
            provider=validated,
            record_kind=kind,
            path=path,
            stat_result=stat_result,
            raw=raw,
        )
        return _repair_observation(
            status="corrupt",
            provider=validated,
            record_kind=kind,
            path=path,
            reason_code="record-malformed",
            error=str(exc),
            repair_handle=repair_handle,
        )
    return _repair_observation(
        status="ok",
        provider=validated,
        record_kind=kind,
        path=path,
        record=record,
    )


def repair_corrupt_record(
    provider: str,
    *,
    record_kind: RecordKind,
    repair_handle: str,
    journal_path: str | Path | None = None,
) -> RuntimeHealthRecord | RuntimeRetryTokenRecord:
    """Replace a matching corrupt record with the synthetic absent state."""
    validated = _validate_provider(provider)
    kind = _validate_record_kind(record_kind)
    path = _record_path(validated, kind, journal_path=journal_path)
    lock_target = runtime_operation_path(validated, journal_path=journal_path)
    try:
        with hold_lock(lock_target, mode=RUNTIME_RECORD_MODE):
            raw = _read_record_bytes(path)
            stat_result = path.stat()
            try:
                _coerce_record_bytes(raw, provider=validated, record_kind=kind)
            except RuntimeHealthMalformedError:
                current_handle = _derive_repair_handle(
                    provider=validated,
                    record_kind=kind,
                    path=path,
                    stat_result=stat_result,
                    raw=raw,
                )
            else:
                raise RuntimeHealthConflictError("record is no longer corrupt")
            if current_handle != repair_handle:
                raise RuntimeHealthConflictError("stale repair handle")
            if kind == "health":
                replacement = make_synthetic_runtime_health(validated)
                _write_json_unlocked(path, _persistable_health(replacement))
                return replacement
            replacement = make_synthetic_retry_token(validated)
            _write_json_unlocked(path, _persistable_retry(replacement))
            return replacement
    except FileNotFoundError as exc:
        raise RuntimeHealthConflictError("record is no longer present") from exc
    except LockTimeout as exc:
        raise RuntimeHealthUnavailableError(
            f"runtime repair lock unavailable: {lock_target}"
        ) from exc
    except OSError as exc:
        raise RuntimeHealthUnavailableError(
            f"runtime repair unavailable: {path}"
        ) from exc


def _read_health_unlocked(path: Path, provider: ProviderName) -> RuntimeHealthRecord:
    try:
        raw = _read_record_bytes(path)
    except FileNotFoundError:
        return make_synthetic_runtime_health(provider)
    except OSError as exc:
        raise RuntimeHealthUnavailableError(
            f"runtime health record unavailable: {path}"
        ) from exc
    return cast(
        RuntimeHealthRecord,
        _coerce_record_bytes(raw, provider=provider, record_kind="health"),
    )


def _read_retry_unlocked(path: Path, provider: ProviderName) -> RuntimeRetryTokenRecord:
    try:
        raw = _read_record_bytes(path)
    except FileNotFoundError:
        return make_synthetic_retry_token(provider)
    except OSError as exc:
        raise RuntimeHealthUnavailableError(
            f"runtime retry-token record unavailable: {path}"
        ) from exc
    return cast(
        RuntimeRetryTokenRecord,
        _coerce_record_bytes(raw, provider=provider, record_kind="retry-token"),
    )


def _coerce_record_bytes(
    raw: bytes,
    *,
    provider: ProviderName,
    record_kind: RecordKind,
) -> RuntimeHealthRecord | RuntimeRetryTokenRecord:
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeHealthMalformedError(
            f"malformed runtime {record_kind} record for {provider}"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeHealthMalformedError(
            f"runtime {record_kind} record must be an object for {provider}"
        )
    if record_kind == "health":
        return _coerce_health(data, provider=provider)
    return _coerce_retry(data, provider=provider)


def _coerce_health(
    data: dict[str, Any] | RuntimeHealthRecord,
    *,
    provider: ProviderName | None = None,
) -> RuntimeHealthRecord:
    raw_provider = provider or data.get("provider")
    validated = _validate_provider(raw_provider)
    schema_version = data.get("schema_version", SCHEMA_VERSION)
    if schema_version != SCHEMA_VERSION:
        raise RuntimeHealthMalformedError(
            f"unsupported runtime health schema_version for {validated}"
        )
    phase = data.get("phase")
    if phase not in RUNTIME_PHASES:
        raise RuntimeHealthMalformedError(f"invalid runtime phase for {validated}")
    detail = data.get("detail", {})
    if not isinstance(detail, dict):
        raise RuntimeHealthMalformedError("runtime health detail must be an object")
    process = data.get("process")
    if process is not None and not isinstance(process, dict):
        raise RuntimeHealthMalformedError("runtime health process must be object/null")
    owner = data.get("owner")
    _validate_owner(owner)
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": validated,
        "revision": _nonnegative_int(data.get("revision", 0), "revision"),
        "phase": cast(RuntimePhase, phase),
        "reason_code": _optional_reason_code(data.get("reason_code")),
        "detail": detail,
        "desired_fingerprint_sha256": _optional_str(
            data.get("desired_fingerprint_sha256"),
            "desired_fingerprint_sha256",
        ),
        "incarnation": _optional_str(data.get("incarnation"), "incarnation"),
        "generation": _nonnegative_int(data.get("generation", 0), "generation"),
        "attempt": _nonnegative_int(data.get("attempt", 0), "attempt"),
        "process": process,
        "updated_at": _optional_str(data.get("updated_at"), "updated_at"),
        "display_deadline_at": _optional_str(
            data.get("display_deadline_at"),
            "display_deadline_at",
        ),
        "owner": owner,
    }


def _coerce_retry(
    data: dict[str, Any] | RuntimeRetryTokenRecord,
    *,
    provider: ProviderName | None = None,
) -> RuntimeRetryTokenRecord:
    raw_provider = provider or data.get("provider")
    validated = _validate_provider(raw_provider)
    schema_version = data.get("schema_version", SCHEMA_VERSION)
    if schema_version != SCHEMA_VERSION:
        raise RuntimeHealthMalformedError(
            f"unsupported runtime retry-token schema_version for {validated}"
        )
    owner = data.get("owner")
    _validate_owner(owner)
    token_id = _optional_str(data.get("token_id"), "token_id")
    reason_code = _optional_reason_code(data.get("reason_code"))
    desired_fingerprint_sha256 = _optional_str(
        data.get("desired_fingerprint_sha256"),
        "desired_fingerprint_sha256",
    )
    requested_at = _optional_str(data.get("requested_at"), "requested_at")
    if token_id is None:
        if (
            desired_fingerprint_sha256 is not None
            or requested_at is not None
            or reason_code is not None
            or owner is not None
        ):
            raise RuntimeHealthMalformedError(
                "cleared retry-token record cannot carry token fields"
            )
    elif requested_at is None or reason_code is None:
        raise RuntimeHealthMalformedError(
            "outstanding retry-token requires requested_at and reason_code"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "provider": validated,
        "revision": _nonnegative_int(data.get("revision", 0), "revision"),
        "token_id": token_id,
        "desired_fingerprint_sha256": desired_fingerprint_sha256,
        "requested_at": requested_at,
        "reason_code": reason_code,
        "owner": owner,
    }


def _persistable_health(record: RuntimeHealthRecord) -> dict[str, Any]:
    return dict(_coerce_health(record))


def _persistable_retry(record: RuntimeRetryTokenRecord) -> dict[str, Any]:
    return dict(_coerce_retry(record))


def _write_json_unlocked(path: Path, record: dict[str, Any]) -> None:
    atomic_replace(
        path,
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        mode=RUNTIME_RECORD_MODE,
    )


def _assert_expected(
    current: RuntimeHealthRecord,
    expected_revision: int,
    expected_desired_fingerprint_sha256: str | None,
) -> None:
    if current["revision"] != expected_revision:
        raise RuntimeHealthConflictError("stale runtime health revision")
    if (
        expected_desired_fingerprint_sha256 is not None
        and current["desired_fingerprint_sha256"] != expected_desired_fingerprint_sha256
    ):
        raise RuntimeHealthConflictError("stale runtime health desired fingerprint")


def _inspection(
    *,
    status: InspectionStatus,
    provider: ProviderName,
    record_kind: RecordKind,
    path: Path,
    record: RuntimeHealthRecord | RuntimeRetryTokenRecord | None = None,
    reason_code: ReasonCode | None = None,
    error: str | None = None,
) -> RuntimeRecordInspection:
    return {
        "status": status,
        "provider": provider,
        "record_kind": record_kind,
        "path": str(path),
        "record": record,
        "reason_code": reason_code,
        "error": error,
    }


def _repair_observation(
    *,
    status: InspectionStatus,
    provider: ProviderName,
    record_kind: RecordKind,
    path: Path,
    record: RuntimeHealthRecord | RuntimeRetryTokenRecord | None = None,
    reason_code: ReasonCode | None = None,
    error: str | None = None,
    repair_handle: str | None = None,
) -> RuntimeRepairObservation:
    result: RuntimeRepairObservation = {
        "status": status,
        "provider": provider,
        "record_kind": record_kind,
        "path": str(path),
        "record": record,
        "reason_code": reason_code,
        "error": error,
    }
    if repair_handle is not None:
        result["repair_handle"] = repair_handle
    return result


def _derive_repair_handle(
    *,
    provider: ProviderName,
    record_kind: RecordKind,
    path: Path,
    stat_result: os.stat_result,
    raw: bytes,
) -> str:
    stat_identity = {
        "dev": getattr(stat_result, "st_dev", None),
        "ino": getattr(stat_result, "st_ino", None),
        "size": getattr(stat_result, "st_size", None),
        "mtime_ns": getattr(stat_result, "st_mtime_ns", None),
        "ctime_ns": getattr(stat_result, "st_ctime_ns", None),
    }
    identity = {
        "provider": provider,
        "record_kind": record_kind,
        "path": str(path),
        "stat": stat_identity,
    }
    digest = hashlib.sha256()
    digest.update(REPAIR_HANDLE_DOMAIN.encode("utf-8"))
    digest.update(b"\0")
    digest.update(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    digest.update(b"\0")
    digest.update(raw)
    return digest.hexdigest()


def _record_path(
    provider: ProviderName,
    record_kind: RecordKind,
    *,
    journal_path: str | Path | None = None,
) -> Path:
    if record_kind == "health":
        return runtime_health_path(provider, journal_path=journal_path)
    return runtime_retry_token_path(provider, journal_path=journal_path)


def _read_record_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _optional_str(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeHealthMalformedError(f"{name} must be a string or null")
    return value


def _optional_reason_code(value: Any) -> ReasonCode | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in REASON_CODES:
        raise RuntimeHealthMalformedError("invalid runtime health reason_code")
    return cast(ReasonCode, value)


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeHealthMalformedError(f"{name} must be a nonnegative integer")
    return value


def _validate_owner(value: object) -> None:
    if value is not None and not isinstance(value, dict):
        raise RuntimeHealthMalformedError("owner must be an object or null")


def _validate_provider(value: object) -> ProviderName:
    if value not in PROVIDERS:
        raise ValueError(f"runtime provider must be one of: {sorted(PROVIDERS)}")
    return cast(ProviderName, value)


def _validate_record_kind(value: object) -> RecordKind:
    if value not in {"health", "retry-token"}:
        raise ValueError("runtime record_kind must be one of: health, retry-token")
    return cast(RecordKind, value)


__all__ = [
    "InspectionStatus",
    "REASON_CODE_GROUPS",
    "REASON_CODES",
    "REPAIR_HANDLE_DOMAIN",
    "RUNTIME_PHASES",
    "RUNTIME_RECORD_MODE",
    "ReasonCode",
    "RecordKind",
    "RuntimeHealthConflictError",
    "RuntimeHealthError",
    "RuntimeHealthMalformedError",
    "RuntimeHealthRecord",
    "RuntimeHealthUnavailableError",
    "RuntimePhase",
    "RuntimeRecordInspection",
    "RuntimeRepairObservation",
    "RuntimeRetryTokenRecord",
    "SCHEMA_VERSION",
    "consume_retry_token",
    "inspect_retry_token",
    "inspect_runtime_health",
    "make_synthetic_retry_token",
    "make_synthetic_runtime_health",
    "now_iso",
    "observe_runtime_repair",
    "read_retry_token",
    "read_runtime_health",
    "repair_corrupt_record",
    "request_retry_token",
    "request_runtime_retry",
    "runtime_directory",
    "runtime_health_path",
    "runtime_operation_lock_path",
    "runtime_operation_path",
    "runtime_retry_token_path",
    "write_runtime_health",
]
