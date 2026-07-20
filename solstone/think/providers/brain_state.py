# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Active-brain state contract for the single selected thinking lane.

Deliberate reason-code additions beyond the lode brief:
- checking_permit_lost: distinguishes crash-released leases from timed-out checks.
- evidence_missing/evidence_expired/evidence_not_attempted/evidence_blocked/evidence_failed:
  generic reducer fallbacks when a component does not provide a narrower reason.
- stale_result_ignored: used when a persisted ready result no longer matches the
  live active-brain fingerprint.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast, get_args

from solstone.think.journal_config import read_journal_config
from solstone.think.journal_io import atomic_replace, hold_lock, write_json
from solstone.think.journal_io.errors import MalformedDataError
from solstone.think.journal_io.lease import (
    FileLease,
    acquire_file_lease,
    assert_file_lease_owned,
    probe_file_lease_held,
)
from solstone.think.journal_io.readers import read_json
from solstone.think.models import DEFAULT_MODEL_BY_PROVIDER, LOCAL_MODEL
from solstone.think.providers.install_state import (
    canonical_fingerprint,
    fingerprint_sha256,
)
from solstone.think.providers.local_endpoint import (
    confidential_provenance_block,
    normalize_local_endpoint_url,
)
from solstone.think.providers.runtime_health import (
    RuntimeRecordInspection,
    inspect_runtime_health,
)
from solstone.think.utils import get_journal

SCHEMA_VERSION = 1
FINGERPRINT_SCHEMA_VERSION = 1
BRAIN_FILE_MODE = 0o600
FINGERPRINT_KEY_BYTES = 32
CHECKING_TTL = timedelta(minutes=10)
DEFAULT_READY_EVIDENCE_TTL = timedelta(hours=26)

CLOUD_BYO_PROVIDERS = frozenset({"anthropic", "google", "openai"})
PROVIDER_ENV_BY_NAME = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
}
COMPONENT_ORDER = ("configuration", "lane_prerequisites", "generate", "cogitate")

BrainAggregateState = Literal["ready", "checking", "blocked", "unhealthy", "unknown"]
BrainComponentStatus = Literal["ok", "blocked", "failed", "unknown", "not_attempted"]
BrainLaneId = Literal["none", "byo-cloud", "byo-endpoint", "bundled", "spp", "unknown"]
BrainInspectionStatus = Literal["ok", "corrupt", "unavailable"]
BrainDiagnosticValue = str | int | float | bool
# Brain reasons are deliberately snake_case, unlike runtime_health.py's
# kebab-case phases/reasons. They match the owner-facing status/reason style in
# provider_readiness.py and state.py; do not harmonize them with runtime health.
BrainReasonCode = Literal[
    "attestation_expired",
    "attestation_not_verified",
    "attestation_rejected",
    "checking_abandoned",
    "checking_active",
    "checking_permit_lost",
    "clock_skew_detected",
    "configuration_invalid",
    "credential_missing",
    "endpoint_contract_failed",
    "endpoint_unreachable",
    "evidence_blocked",
    "evidence_expired",
    "evidence_failed",
    "evidence_missing",
    "evidence_not_attempted",
    "fingerprint_key_unavailable",
    "fingerprint_unavailable",
    "record_malformed",
    "record_unavailable",
    "runtime_blocked",
    "runtime_failed",
    "runtime_not_ready",
    "runtime_ready_proof_unavailable",
    "runtime_state_unavailable",
    "stale_result_ignored",
    "thinking_engine_not_chosen",
    "timestamp_invalid",
]
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

BRAIN_AGGREGATE_STATES = frozenset(
    {"ready", "checking", "blocked", "unhealthy", "unknown"}
)
BRAIN_COMPONENT_STATUSES = frozenset(
    {"ok", "blocked", "failed", "unknown", "not_attempted"}
)
BRAIN_LANES = frozenset(
    {"none", "byo-cloud", "byo-endpoint", "bundled", "spp", "unknown"}
)
BRAIN_REASON_TO_AGGREGATE: dict[str, BrainAggregateState] = {
    "attestation_expired": "unknown",
    "attestation_not_verified": "unknown",
    "attestation_rejected": "unhealthy",
    "checking_abandoned": "unknown",
    "checking_active": "checking",
    "checking_permit_lost": "unknown",
    "clock_skew_detected": "unknown",
    "configuration_invalid": "unknown",
    "credential_missing": "blocked",
    "endpoint_contract_failed": "unhealthy",
    "endpoint_unreachable": "unhealthy",
    "evidence_blocked": "blocked",
    "evidence_expired": "unknown",
    "evidence_failed": "unhealthy",
    "evidence_missing": "unknown",
    "evidence_not_attempted": "unknown",
    "fingerprint_key_unavailable": "unknown",
    "fingerprint_unavailable": "unknown",
    "record_malformed": "unknown",
    "record_unavailable": "unknown",
    "runtime_blocked": "blocked",
    "runtime_failed": "unhealthy",
    "runtime_not_ready": "unknown",
    "runtime_ready_proof_unavailable": "unknown",
    "runtime_state_unavailable": "unknown",
    "stale_result_ignored": "unknown",
    "thinking_engine_not_chosen": "blocked",
    "timestamp_invalid": "unknown",
}
BRAIN_REASON_CODES = frozenset(BRAIN_REASON_TO_AGGREGATE)
RUNTIME_FAILURE_AGGREGATES: frozenset[BrainAggregateState] = frozenset(
    {"blocked", "unhealthy", "unknown"}
)

if BRAIN_AGGREGATE_STATES & BRAIN_REASON_CODES:
    raise RuntimeError("brain aggregate-state and reason-code vocabularies overlap")
if BRAIN_COMPONENT_STATUSES & BRAIN_REASON_CODES:
    raise RuntimeError("brain component-status and reason-code vocabularies overlap")

RUNTIME_PHASES = frozenset(cast(tuple[str, ...], get_args(RuntimePhase)))
# ready-proof-unavailable is non-ready for brain proof even though supervisor.py
# treats it as ready-equivalent for process gating. The brain contract requires
# actual ready proof before reporting the selected brain as ready.
RUNTIME_PHASE_TO_REASON: dict[str, BrainReasonCode | None] = {
    "ready": None,
    "artifact-not-ready": "runtime_blocked",
    "host-blocked": "runtime_blocked",
    "observing": "runtime_not_ready",
    "starting": "runtime_not_ready",
    "warming": "runtime_not_ready",
    "backoff": "runtime_not_ready",
    "retry-requested": "runtime_not_ready",
    "stop-deferred": "runtime_not_ready",
    "stopping": "runtime_not_ready",
    "stopped": "runtime_not_ready",
    "failed": "runtime_failed",
    "cleanup-failed": "runtime_failed",
    "not-desired": "runtime_state_unavailable",
    "state-corrupt": "runtime_state_unavailable",
    "state-unavailable": "runtime_state_unavailable",
    "ready-proof-unavailable": "runtime_ready_proof_unavailable",
}

DiagnosticMetadataSchema = dict[str, frozenset[str]]
CONFIG_DIAGNOSTIC_FIELDS = frozenset({"providers.active.provider"})
DIAGNOSTIC_METADATA_SCHEMAS: dict[str, DiagnosticMetadataSchema] = {
    reason: {} for reason in BRAIN_REASON_CODES
}
DIAGNOSTIC_METADATA_SCHEMAS.update(
    {
        "configuration_invalid": {"field": CONFIG_DIAGNOSTIC_FIELDS},
        "runtime_blocked": {"phase": RUNTIME_PHASES},
        "runtime_failed": {"phase": RUNTIME_PHASES},
        "runtime_not_ready": {"phase": RUNTIME_PHASES},
        "runtime_ready_proof_unavailable": {"phase": RUNTIME_PHASES},
        "runtime_state_unavailable": {"phase": RUNTIME_PHASES},
    }
)

BRAIN_TOP_LEVEL_FIELDS = {
    "schema_version",
    "revision",
    "aggregate_state",
    "reason_code",
    "active_lane",
    "active_provider",
    "active_model",
    "fingerprint_sha256",
    "checking",
    "evidence",
    "runtime_failure_marker",
    "diagnostic",
    "updated_at",
}
BRAIN_CHECKING_FIELDS = {
    "run_id",
    "started_at",
    "expires_at",
    "fingerprint_sha256",
    "checking_revision",
    "runtime_failure_marker_seen",
}
BRAIN_EVIDENCE_FIELDS = set(COMPONENT_ORDER)
BRAIN_EVIDENCE_COMPONENT_FIELDS = {
    "status",
    "reason_code",
    "observed_at",
    "expires_at",
    "diagnostic",
}
BRAIN_RUNTIME_FAILURE_MARKER_FIELDS = {
    "marker_id",
    "revision",
    "recorded_at",
    "reason_code",
}


class BrainEvidenceComponent(TypedDict):
    status: BrainComponentStatus
    observed_at: str
    reason_code: NotRequired[BrainReasonCode | None]
    expires_at: NotRequired[str | None]
    diagnostic: NotRequired[dict[str, BrainDiagnosticValue]]


class BrainEvidenceRecord(TypedDict):
    configuration: BrainEvidenceComponent | None
    lane_prerequisites: BrainEvidenceComponent | None
    generate: BrainEvidenceComponent | None
    cogitate: BrainEvidenceComponent | None


class BrainCheckingRecord(TypedDict):
    run_id: str
    started_at: str
    expires_at: str
    fingerprint_sha256: str | None
    checking_revision: int
    runtime_failure_marker_seen: str | None


class BrainRuntimeFailureMarker(TypedDict):
    marker_id: str
    revision: int
    recorded_at: str
    reason_code: BrainReasonCode


class BrainStateRecord(TypedDict):
    schema_version: int
    revision: int
    aggregate_state: BrainAggregateState
    reason_code: BrainReasonCode | None
    active_lane: BrainLaneId
    active_provider: str | None
    active_model: str | None
    fingerprint_sha256: str | None
    checking: BrainCheckingRecord | None
    evidence: BrainEvidenceRecord
    runtime_failure_marker: BrainRuntimeFailureMarker | None
    diagnostic: dict[str, BrainDiagnosticValue]
    updated_at: str


class BrainProjection(TypedDict):
    aggregate_state: BrainAggregateState
    reason_code: BrainReasonCode | None
    active_lane: BrainLaneId
    active_provider: str | None
    active_model: str | None
    fingerprint_sha256: str | None


class BrainStateInspection(TypedDict):
    status: BrainInspectionStatus
    path: str
    record: BrainStateRecord | None
    projection: BrainProjection
    reason_code: BrainReasonCode | None
    error: str | None


class BrainFingerprintResult(TypedDict):
    ok: bool
    fingerprint_sha256: str | None
    active_lane: BrainLaneId
    active_provider: str | None
    active_model: str | None
    reason_code: BrainReasonCode | None
    diagnostic: dict[str, BrainDiagnosticValue]
    bundled_runtime_fingerprint_sha256: NotRequired[str | None]


class BrainProbeOutcome(TypedDict):
    configuration: BrainEvidenceComponent | None
    lane_prerequisites: BrainEvidenceComponent | None
    generate: BrainEvidenceComponent | None
    cogitate: BrainEvidenceComponent | None


class BrainStateValidationError(ValueError):
    """Raised when a persisted brain state record violates the closed schema."""

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


class BrainStateConflictError(RuntimeError):
    """Raised when a stale refresh permit attempts to finalize."""


@dataclass
class BrainRefreshPermit:
    """Held active-brain refresh permit."""

    run_id: str
    started_at: datetime
    expires_at: datetime
    fingerprint_sha256: str
    checking_revision: int
    runtime_failure_marker_seen: str | None
    lease: FileLease

    @property
    def owned(self) -> bool:
        return self.lease.owned

    def release(self) -> None:
        self.lease.release()


def brain_state_path(*, journal_path: str | Path | None = None) -> Path:
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return root / "health" / "brain.json"


def brain_fingerprint_key_path(*, journal_path: str | Path | None = None) -> Path:
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return root / "health" / "brain-fingerprint.key"


def brain_refresh_lease_path(*, journal_path: str | Path | None = None) -> Path:
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return root / "health" / "brain-refresh.lease"


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("brain state timestamps require timezone-aware datetimes")
    return now.astimezone(timezone.utc)


def _iso(now: datetime) -> str:
    return _utc(now).isoformat()


def _parse_timestamp(value: Any, path: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise BrainStateValidationError(path, "timestamp must be a non-empty string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BrainStateValidationError(path, "timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BrainStateValidationError(path, "timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def _hash(value: bytes, *, hmac_key: bytes) -> str:
    return hmac.digest(hmac_key, value, "sha256").hex()


def _canonical_digest(value: Any, *, hmac_key: bytes) -> str:
    payload = canonical_fingerprint(cast(dict[str, Any], {"value": value}))
    return _hash(payload.encode("utf-8"), hmac_key=hmac_key)


def _read_fingerprint_key(path: Path) -> bytes:
    key = path.read_bytes()
    if len(key) != FINGERPRINT_KEY_BYTES:
        raise ValueError("brain fingerprint key has invalid length")
    return key


def _load_or_generate_fingerprint_key(
    *, journal_path: str | Path | None = None
) -> bytes:
    path = brain_fingerprint_key_path(journal_path=journal_path)
    with hold_lock(path, mode=BRAIN_FILE_MODE):
        try:
            key = _read_fingerprint_key(path)
        except FileNotFoundError:
            key = secrets.token_bytes(FINGERPRINT_KEY_BYTES)
        atomic_replace(path, key, mode=BRAIN_FILE_MODE)
        return _read_fingerprint_key(path)


def _load_existing_fingerprint_key(
    *, journal_path: str | Path | None = None
) -> bytes | None:
    path = brain_fingerprint_key_path(journal_path=journal_path)
    try:
        return _read_fingerprint_key(path)
    except (OSError, ValueError):
        return None


def _active_config(config: Mapping[str, Any]) -> tuple[str, str]:
    providers = config.get("providers")
    active: Any = {}
    if isinstance(providers, Mapping):
        active = providers.get("active", {})
    if not isinstance(active, Mapping):
        return "none", ""
    provider = active.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        return "none", ""
    provider = provider.strip()
    if provider == "none":
        return "none", ""
    model = active.get("model")
    if isinstance(model, str) and model.strip():
        return provider, model.strip()
    return provider, DEFAULT_MODEL_BY_PROVIDER.get(provider, "")


def _local_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    providers = config.get("providers")
    if not isinstance(providers, Mapping):
        return {}
    local = providers.get("local", {})
    return local if isinstance(local, Mapping) else {}


def _local_endpoint_from_config(
    config: Mapping[str, Any],
) -> tuple[bool, str, str, str | None]:
    local = _local_config(config)
    endpoint_url = str(local.get("endpoint_url") or "").strip()
    served_model_id = str(local.get("served_model_id") or "").strip()
    if endpoint_url and served_model_id:
        credential = local.get("credential")
        return (
            False,
            normalize_local_endpoint_url(endpoint_url),
            served_model_id,
            str(credential) if credential is not None else None,
        )
    return True, "", "", None


def _derive_lane(
    config: Mapping[str, Any],
) -> tuple[BrainLaneId, str | None, str | None]:
    provider, model = _active_config(config)
    if provider == "none":
        return "none", "none", None
    if provider in CLOUD_BYO_PROVIDERS:
        return "byo-cloud", provider, model
    if provider == "local":
        is_bundled, _, _, _ = _local_endpoint_from_config(config)
        if is_bundled:
            return "bundled", provider, model
        if confidential_provenance_block(dict(config)) is not None:
            return "spp", provider, model
        return "byo-endpoint", provider, model
    return "unknown", provider, model


def _bundled_runtime_fingerprint_sha() -> str:
    if sys.platform == "darwin":
        from solstone.think.providers import mlx_install

        target = mlx_install.target_fingerprint()
    else:
        from solstone.think.providers import local_install

        target = local_install.target_fingerprint(LOCAL_MODEL)
    return fingerprint_sha256(canonical_fingerprint(target))


def build_active_brain_fingerprint(
    config: Mapping[str, Any], *, hmac_key: bytes
) -> BrainFingerprintResult:
    lane, provider, model = _derive_lane(config)
    diagnostic: dict[str, BrainDiagnosticValue] = {}
    bundled_runtime_fingerprint_sha256: str | None = None
    if lane == "unknown":
        return {
            "ok": False,
            "fingerprint_sha256": None,
            "active_lane": lane,
            "active_provider": provider,
            "active_model": model,
            "reason_code": "configuration_invalid",
            "diagnostic": {"field": "providers.active.provider"},
            "bundled_runtime_fingerprint_sha256": None,
        }

    components: dict[str, Any] = {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "lane": lane,
        "active": {"provider": provider, "model": model or ""},
    }
    env_config = config.get("env", {})
    if not isinstance(env_config, Mapping):
        env_config = {}

    if lane == "byo-cloud" and provider is not None:
        env_key = PROVIDER_ENV_BY_NAME[provider]
        credential = str(env_config.get(env_key) or "")
        components["cloud_credential"] = (
            _hash(credential.encode("utf-8"), hmac_key=hmac_key) if credential else None
        )
    elif lane in {"byo-endpoint", "spp"}:
        _, base_url, served_model_id, credential = _local_endpoint_from_config(config)
        components["local_endpoint"] = {
            "base_url": base_url,
            "served_model_id": served_model_id,
            "credential": (
                _hash(credential.encode("utf-8"), hmac_key=hmac_key)
                if credential
                else None
            ),
        }
        if lane == "spp":
            block = confidential_provenance_block(dict(config))
            components["confidential"] = (
                _canonical_digest(block, hmac_key=hmac_key)
                if block is not None
                else None
            )
    elif lane == "bundled":
        try:
            bundled_runtime_fingerprint_sha256 = _bundled_runtime_fingerprint_sha()
            components["bundled_runtime"] = bundled_runtime_fingerprint_sha256
        except Exception:
            return {
                "ok": False,
                "fingerprint_sha256": None,
                "active_lane": lane,
                "active_provider": provider,
                "active_model": model,
                "reason_code": "fingerprint_unavailable",
                "diagnostic": {},
                "bundled_runtime_fingerprint_sha256": None,
            }

    fingerprint = fingerprint_sha256(canonical_fingerprint(components))
    return {
        "ok": True,
        "fingerprint_sha256": fingerprint,
        "active_lane": lane,
        "active_provider": provider,
        "active_model": model,
        "reason_code": None,
        "diagnostic": diagnostic,
        "bundled_runtime_fingerprint_sha256": bundled_runtime_fingerprint_sha256,
    }


def _projection(
    aggregate_state: BrainAggregateState,
    reason_code: BrainReasonCode | None,
    *,
    active_lane: BrainLaneId = "unknown",
    active_provider: str | None = None,
    active_model: str | None = None,
    fingerprint_sha256: str | None = None,
) -> BrainProjection:
    return {
        "aggregate_state": aggregate_state,
        "reason_code": reason_code,
        "active_lane": active_lane,
        "active_provider": active_provider,
        "active_model": active_model,
        "fingerprint_sha256": fingerprint_sha256,
    }


def _empty_evidence() -> BrainEvidenceRecord:
    return {
        "configuration": None,
        "lane_prerequisites": None,
        "generate": None,
        "cogitate": None,
    }


def _component(
    status: BrainComponentStatus,
    now: datetime,
    *,
    reason_code: BrainReasonCode | None = None,
    diagnostic: Mapping[str, BrainDiagnosticValue] | None = None,
    expires_at: datetime | None = None,
) -> BrainEvidenceComponent:
    item: BrainEvidenceComponent = {
        "status": status,
        "observed_at": _iso(now),
    }
    if reason_code is not None:
        item["reason_code"] = reason_code
    if diagnostic:
        item["diagnostic"] = dict(diagnostic)
    if expires_at is not None:
        item["expires_at"] = _iso(expires_at)
    return item


def _record(
    *,
    revision: int,
    aggregate_state: BrainAggregateState,
    reason_code: BrainReasonCode | None,
    active_lane: BrainLaneId,
    active_provider: str | None,
    active_model: str | None,
    fingerprint_sha256: str | None,
    checking: BrainCheckingRecord | None,
    evidence: BrainEvidenceRecord,
    runtime_failure_marker: BrainRuntimeFailureMarker | None,
    diagnostic: Mapping[str, BrainDiagnosticValue] | None,
    now: datetime,
) -> BrainStateRecord:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": revision,
        "aggregate_state": aggregate_state,
        "reason_code": reason_code,
        "active_lane": active_lane,
        "active_provider": active_provider,
        "active_model": active_model,
        "fingerprint_sha256": fingerprint_sha256,
        "checking": checking,
        "evidence": evidence,
        "runtime_failure_marker": runtime_failure_marker,
        "diagnostic": dict(diagnostic or {}),
        "updated_at": _iso(now),
    }


def _validate_hex(value: Any, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64:
        raise BrainStateValidationError(path, "expected SHA-256 hex string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise BrainStateValidationError(path, "expected SHA-256 hex string") from exc
    return value


def _validate_diagnostic(
    value: Any,
    path: str,
    reason_code: BrainReasonCode | None,
) -> dict[str, BrainDiagnosticValue]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise BrainStateValidationError(path, "diagnostic must be an object")
    allowed = DIAGNOSTIC_METADATA_SCHEMAS.get(reason_code or "", {})
    found: dict[str, BrainDiagnosticValue] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or key not in allowed:
            raise BrainStateValidationError(
                f"{path}.{key}", "diagnostic key not allowed"
            )
        allowed_values = allowed[key]
        if not isinstance(raw, str):
            raise BrainStateValidationError(
                f"{path}.{key}", "diagnostic value must be an enum string"
            )
        if raw not in allowed_values:
            raise BrainStateValidationError(
                f"{path}.{key}", "diagnostic enum value not allowed"
            )
        found[key] = raw
    return found


def _validate_reason(
    value: Any, path: str, *, nullable: bool
) -> BrainReasonCode | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or value not in BRAIN_REASON_CODES:
        raise BrainStateValidationError(path, "unknown reason code")
    return cast(BrainReasonCode, value)


def _validate_component(value: Any, path: str) -> BrainEvidenceComponent | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise BrainStateValidationError(path, "evidence component must be an object")
    unknown = set(value) - BRAIN_EVIDENCE_COMPONENT_FIELDS
    if unknown:
        raise BrainStateValidationError(
            f"{path}.{sorted(unknown)[0]}", "unknown evidence field"
        )
    status = value.get("status")
    if not isinstance(status, str) or status not in BRAIN_COMPONENT_STATUSES:
        raise BrainStateValidationError(f"{path}.status", "unknown component status")
    observed_at = _parse_timestamp(value.get("observed_at"), f"{path}.observed_at")
    reason = _validate_reason(
        value.get("reason_code"), f"{path}.reason_code", nullable=True
    )
    diagnostic = _validate_diagnostic(
        value.get("diagnostic", {}), f"{path}.diagnostic", reason
    )

    component: BrainEvidenceComponent = {
        "status": cast(BrainComponentStatus, status),
        "observed_at": observed_at.isoformat(),
    }
    if reason is not None:
        component["reason_code"] = reason
    if diagnostic:
        component["diagnostic"] = diagnostic
    expires_value = value.get("expires_at")
    if status == "ok" and expires_value is None:
        raise BrainStateValidationError(
            f"{path}.expires_at", "ok evidence requires expiry"
        )
    if expires_value is not None:
        component["expires_at"] = _parse_timestamp(
            expires_value, f"{path}.expires_at"
        ).isoformat()
    return component


def _validate_evidence(value: Any, path: str) -> BrainEvidenceRecord:
    if not isinstance(value, Mapping):
        raise BrainStateValidationError(path, "evidence must be an object")
    unknown = set(value) - BRAIN_EVIDENCE_FIELDS
    if unknown:
        raise BrainStateValidationError(
            f"{path}.{sorted(unknown)[0]}", "unknown evidence key"
        )
    return {
        "configuration": _validate_component(
            value.get("configuration"), f"{path}.configuration"
        ),
        "lane_prerequisites": _validate_component(
            value.get("lane_prerequisites"), f"{path}.lane_prerequisites"
        ),
        "generate": _validate_component(value.get("generate"), f"{path}.generate"),
        "cogitate": _validate_component(value.get("cogitate"), f"{path}.cogitate"),
    }


def _validate_checking(value: Any, path: str) -> BrainCheckingRecord | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise BrainStateValidationError(path, "checking must be an object")
    unknown = set(value) - BRAIN_CHECKING_FIELDS
    if unknown:
        raise BrainStateValidationError(
            f"{path}.{sorted(unknown)[0]}", "unknown checking key"
        )
    run_id = value.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise BrainStateValidationError(f"{path}.run_id", "run_id must be non-empty")
    checking_revision = value.get("checking_revision")
    if not isinstance(checking_revision, int) or isinstance(checking_revision, bool):
        raise BrainStateValidationError(
            f"{path}.checking_revision", "checking_revision must be an integer"
        )
    marker_seen = value.get("runtime_failure_marker_seen")
    if marker_seen is not None and not isinstance(marker_seen, str):
        raise BrainStateValidationError(
            f"{path}.runtime_failure_marker_seen", "marker must be string or null"
        )
    return {
        "run_id": run_id,
        "started_at": _parse_timestamp(
            value.get("started_at"), f"{path}.started_at"
        ).isoformat(),
        "expires_at": _parse_timestamp(
            value.get("expires_at"), f"{path}.expires_at"
        ).isoformat(),
        "fingerprint_sha256": _validate_hex(
            value.get("fingerprint_sha256"), f"{path}.fingerprint_sha256"
        ),
        "checking_revision": checking_revision,
        "runtime_failure_marker_seen": marker_seen,
    }


def _validate_runtime_failure_marker(
    value: Any, path: str
) -> BrainRuntimeFailureMarker | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise BrainStateValidationError(
            path, "runtime failure marker must be an object"
        )
    unknown = set(value) - BRAIN_RUNTIME_FAILURE_MARKER_FIELDS
    if unknown:
        raise BrainStateValidationError(
            f"{path}.{sorted(unknown)[0]}", "unknown runtime failure marker key"
        )
    marker_id = value.get("marker_id")
    revision = value.get("revision")
    if not isinstance(marker_id, str) or not marker_id:
        raise BrainStateValidationError(
            f"{path}.marker_id", "marker_id must be non-empty"
        )
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise BrainStateValidationError(f"{path}.revision", "revision must be integer")
    reason = _validate_reason(
        value.get("reason_code"), f"{path}.reason_code", nullable=False
    )
    assert reason is not None
    return {
        "marker_id": marker_id,
        "revision": revision,
        "recorded_at": _parse_timestamp(
            value.get("recorded_at"), f"{path}.recorded_at"
        ).isoformat(),
        "reason_code": reason,
    }


def validate_brain_state_record(record: Mapping[str, Any]) -> BrainStateRecord:
    unknown = set(record) - BRAIN_TOP_LEVEL_FIELDS
    if unknown:
        raise BrainStateValidationError(sorted(unknown)[0], "unknown top-level field")
    schema_version = record.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise BrainStateValidationError("schema_version", "unsupported schema version")
    revision = record.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise BrainStateValidationError(
            "revision", "revision must be non-negative integer"
        )
    aggregate = record.get("aggregate_state")
    if not isinstance(aggregate, str) or aggregate not in BRAIN_AGGREGATE_STATES:
        raise BrainStateValidationError("aggregate_state", "unknown aggregate state")
    reason = _validate_reason(record.get("reason_code"), "reason_code", nullable=True)
    lane = record.get("active_lane")
    if not isinstance(lane, str) or lane not in BRAIN_LANES:
        raise BrainStateValidationError("active_lane", "unknown brain lane")
    provider = record.get("active_provider")
    model = record.get("active_model")
    if provider is not None and not isinstance(provider, str):
        raise BrainStateValidationError(
            "active_provider", "provider must be string or null"
        )
    if model is not None and not isinstance(model, str):
        raise BrainStateValidationError("active_model", "model must be string or null")
    diagnostic = _validate_diagnostic(
        record.get("diagnostic", {}), "diagnostic", reason
    )
    checking = _validate_checking(record.get("checking"), "checking")
    if (aggregate == "checking") != (checking is not None):
        raise BrainStateValidationError(
            "checking", "checking aggregate requires matching checking marker"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": revision,
        "aggregate_state": cast(BrainAggregateState, aggregate),
        "reason_code": reason,
        "active_lane": cast(BrainLaneId, lane),
        "active_provider": provider,
        "active_model": model,
        "fingerprint_sha256": _validate_hex(
            record.get("fingerprint_sha256"), "fingerprint_sha256"
        ),
        "checking": checking,
        "evidence": _validate_evidence(record.get("evidence"), "evidence"),
        "runtime_failure_marker": _validate_runtime_failure_marker(
            record.get("runtime_failure_marker"), "runtime_failure_marker"
        ),
        "diagnostic": diagnostic,
        "updated_at": _parse_timestamp(
            record.get("updated_at"), "updated_at"
        ).isoformat(),
    }


def _read_record_unlocked(path: Path) -> BrainStateRecord | None:
    raw = read_json(path, default=None)
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise BrainStateValidationError("record", "brain state must be an object")
    return validate_brain_state_record(raw)


def _write_record(path: Path, record: BrainStateRecord) -> BrainStateRecord:
    validate_brain_state_record(record)
    write_json(path, record, mode=BRAIN_FILE_MODE, sort_keys=True)
    return record


def _next_revision(record: BrainStateRecord | None) -> int:
    return (record["revision"] if record is not None else 0) + 1


def _runtime_failure_marker_id(record: BrainStateRecord | None) -> str | None:
    if record is None or record["runtime_failure_marker"] is None:
        return None
    return record["runtime_failure_marker"]["marker_id"]


def _check_clock(record: BrainStateRecord, now: datetime) -> BrainReasonCode | None:
    timestamps = [record["updated_at"]]
    expiry_timestamps: list[str] = []
    checking = record["checking"]
    if checking is not None:
        timestamps.append(checking["started_at"])
        expiry_timestamps.append(checking["expires_at"])
    marker = record["runtime_failure_marker"]
    if marker is not None:
        timestamps.append(marker["recorded_at"])
    for component_name in COMPONENT_ORDER:
        component = record["evidence"][component_name]
        if component is None:
            continue
        timestamps.append(component["observed_at"])
        expires = component.get("expires_at")
        if expires:
            expiry_timestamps.append(expires)
    try:
        for value in timestamps:
            if _parse_timestamp(value, "timestamp") > now:
                return "clock_skew_detected"
        for value in expiry_timestamps:
            _parse_timestamp(value, "timestamp")
    except BrainStateValidationError:
        return "timestamp_invalid"
    return None


def _component_reason(
    component: BrainEvidenceComponent,
    fallback: BrainReasonCode,
) -> BrainReasonCode:
    return component.get("reason_code") or fallback


def _reduce_evidence(
    evidence: BrainEvidenceRecord,
    now: datetime,
) -> tuple[BrainAggregateState, BrainReasonCode | None]:
    present = False
    for component_name in COMPONENT_ORDER:
        component = evidence[component_name]
        if component is None:
            continue
        present = True
        status = component["status"]
        if status == "ok":
            expires_at = _parse_timestamp(
                component.get("expires_at"), f"evidence.{component_name}.expires_at"
            )
            if now >= expires_at:
                return "unknown", _component_reason(component, "evidence_expired")
            continue
        if status == "blocked":
            reason = _component_reason(component, "evidence_blocked")
            return BRAIN_REASON_TO_AGGREGATE[reason], reason
        if status == "failed":
            reason = _component_reason(component, "evidence_failed")
            return BRAIN_REASON_TO_AGGREGATE[reason], reason
        if status == "not_attempted":
            reason = _component_reason(component, "evidence_not_attempted")
            return BRAIN_REASON_TO_AGGREGATE[reason], reason
        reason = _component_reason(component, "evidence_missing")
        return BRAIN_REASON_TO_AGGREGATE[reason], reason
    if not present:
        return "unknown", "evidence_missing"
    return "ready", None


def _bundled_runtime_reason(
    runtime_health: RuntimeRecordInspection | None,
    bundled_runtime_fingerprint_sha256: str | None,
) -> BrainReasonCode | None:
    if runtime_health is None or runtime_health["status"] != "ok":
        return "runtime_state_unavailable"
    record = runtime_health["record"]
    if not isinstance(record, Mapping):
        return "runtime_state_unavailable"
    phase = record.get("phase")
    if not isinstance(phase, str) or phase not in RUNTIME_PHASE_TO_REASON:
        return "runtime_state_unavailable"
    reason = RUNTIME_PHASE_TO_REASON[phase]
    if reason is not None:
        return reason
    desired = record.get("desired_fingerprint_sha256")
    if not isinstance(desired, str) or desired != bundled_runtime_fingerprint_sha256:
        return "runtime_state_unavailable"
    return None


def project_brain_state(
    record: BrainStateRecord | None,
    now: datetime,
    *,
    config: Mapping[str, Any],
    hmac_key: bytes | None,
    refresh_permit_active: bool,
    runtime_health: RuntimeRecordInspection | None,
) -> BrainProjection:
    """Project persisted brain state against live config and lease evidence.

    The refresh-permit probe is an optimization for crash-released locks: a free
    lease can project `unknown / checking_permit_lost` before the ten-minute
    checking expiry. The expiry remains the authoritative backstop; stale green
    never depends on the probe alone because `now >= checking.expires_at` also
    projects `unknown`.
    """

    now = _utc(now)
    if record is None:
        return _projection("unknown", "record_unavailable")
    if hmac_key is None:
        return _projection(
            "unknown",
            "fingerprint_key_unavailable",
            active_lane=record["active_lane"],
            active_provider=record["active_provider"],
            active_model=record["active_model"],
            fingerprint_sha256=record["fingerprint_sha256"],
        )
    clock_reason = _check_clock(record, now)
    if clock_reason is not None:
        return _projection(
            "unknown",
            clock_reason,
            active_lane=record["active_lane"],
            active_provider=record["active_provider"],
            active_model=record["active_model"],
            fingerprint_sha256=record["fingerprint_sha256"],
        )
    fingerprint = build_active_brain_fingerprint(config, hmac_key=hmac_key)
    if not fingerprint["ok"]:
        return _projection(
            BRAIN_REASON_TO_AGGREGATE[
                fingerprint["reason_code"] or "fingerprint_unavailable"
            ],
            fingerprint["reason_code"] or "fingerprint_unavailable",
            active_lane=fingerprint["active_lane"],
            active_provider=fingerprint["active_provider"],
            active_model=fingerprint["active_model"],
            fingerprint_sha256=record["fingerprint_sha256"],
        )
    if record["fingerprint_sha256"] != fingerprint["fingerprint_sha256"]:
        return _projection(
            "unknown",
            "stale_result_ignored",
            active_lane=fingerprint["active_lane"],
            active_provider=fingerprint["active_provider"],
            active_model=fingerprint["active_model"],
            fingerprint_sha256=record["fingerprint_sha256"],
        )
    checking = record["checking"]
    if checking is not None:
        expires_at = _parse_timestamp(checking["expires_at"], "checking.expires_at")
        if now >= expires_at:
            return _projection(
                "unknown",
                "checking_abandoned",
                active_lane=record["active_lane"],
                active_provider=record["active_provider"],
                active_model=record["active_model"],
                fingerprint_sha256=record["fingerprint_sha256"],
            )
        if not refresh_permit_active:
            return _projection(
                "unknown",
                "checking_permit_lost",
                active_lane=record["active_lane"],
                active_provider=record["active_provider"],
                active_model=record["active_model"],
                fingerprint_sha256=record["fingerprint_sha256"],
            )
        return _projection(
            "checking",
            "checking_active",
            active_lane=record["active_lane"],
            active_provider=record["active_provider"],
            active_model=record["active_model"],
            fingerprint_sha256=record["fingerprint_sha256"],
        )
    if record["active_lane"] == "none":
        return _projection(
            "blocked",
            "thinking_engine_not_chosen",
            active_lane=record["active_lane"],
            active_provider=record["active_provider"],
            active_model=record["active_model"],
            fingerprint_sha256=record["fingerprint_sha256"],
        )
    if record["active_lane"] == "bundled":
        runtime_reason = _bundled_runtime_reason(
            runtime_health,
            fingerprint.get("bundled_runtime_fingerprint_sha256"),
        )
        if runtime_reason is not None:
            return _projection(
                BRAIN_REASON_TO_AGGREGATE[runtime_reason],
                runtime_reason,
                active_lane=record["active_lane"],
                active_provider=record["active_provider"],
                active_model=record["active_model"],
                fingerprint_sha256=record["fingerprint_sha256"],
            )
    aggregate, reason = _reduce_evidence(record["evidence"], now)
    return _projection(
        aggregate,
        reason,
        active_lane=record["active_lane"],
        active_provider=record["active_provider"],
        active_model=record["active_model"],
        fingerprint_sha256=record["fingerprint_sha256"],
    )


def inspect_brain_state(
    now: datetime, *, journal_path: str | Path | None = None
) -> BrainStateInspection:
    now = _utc(now)
    path = brain_state_path(journal_path=journal_path)
    hmac_key = _load_existing_fingerprint_key(journal_path=journal_path)
    config = read_journal_config(journal_path)
    lane, _provider, _model = _derive_lane(config)
    runtime_health = (
        inspect_runtime_health("local", journal_path=journal_path)
        if lane == "bundled"
        else None
    )
    refresh_permit_active = probe_file_lease_held(
        brain_refresh_lease_path(journal_path=journal_path)
    )
    try:
        record = _read_record_unlocked(path)
    except (BrainStateValidationError, MalformedDataError, json.JSONDecodeError) as exc:
        projection = _projection("unknown", "record_malformed")
        return {
            "status": "corrupt",
            "path": str(path),
            "record": None,
            "projection": projection,
            "reason_code": "record_malformed",
            "error": str(exc),
        }
    if record is None:
        projection = project_brain_state(
            None,
            now,
            config=config,
            hmac_key=hmac_key,
            refresh_permit_active=refresh_permit_active,
            runtime_health=runtime_health,
        )
        return {
            "status": "unavailable",
            "path": str(path),
            "record": None,
            "projection": projection,
            "reason_code": "record_unavailable",
            "error": None,
        }
    projection = project_brain_state(
        record,
        now,
        config=config,
        hmac_key=hmac_key,
        refresh_permit_active=refresh_permit_active,
        runtime_health=runtime_health,
    )
    return {
        "status": "ok",
        "path": str(path),
        "record": record,
        "projection": projection,
        "reason_code": projection["reason_code"],
        "error": None,
    }


def _begin_nonrefresh_record(
    fingerprint: BrainFingerprintResult,
    now: datetime,
    *,
    path: Path,
) -> None:
    with hold_lock(path, mode=BRAIN_FILE_MODE):
        current = _read_record_unlocked(path)
        revision = _next_revision(current)
        if fingerprint["active_lane"] == "none":
            aggregate: BrainAggregateState = "blocked"
            reason: BrainReasonCode | None = "thinking_engine_not_chosen"
            evidence = _empty_evidence()
            evidence["configuration"] = _component(
                "blocked", now, reason_code="thinking_engine_not_chosen"
            )
        else:
            reason = fingerprint["reason_code"] or "fingerprint_unavailable"
            aggregate = BRAIN_REASON_TO_AGGREGATE[reason]
            evidence = _empty_evidence()
            evidence["configuration"] = _component(
                "unknown",
                now,
                reason_code=reason,
                diagnostic=fingerprint["diagnostic"],
            )
        record = _record(
            revision=revision,
            aggregate_state=aggregate,
            reason_code=reason,
            active_lane=fingerprint["active_lane"],
            active_provider=fingerprint["active_provider"],
            active_model=fingerprint["active_model"],
            fingerprint_sha256=fingerprint["fingerprint_sha256"],
            checking=None,
            evidence=evidence,
            runtime_failure_marker=current["runtime_failure_marker"]
            if current
            else None,
            diagnostic=fingerprint["diagnostic"],
            now=now,
        )
        _write_record(path, record)


def begin_brain_refresh(
    now: datetime,
    *,
    run_id: str | None = None,
    journal_path: str | Path | None = None,
) -> BrainRefreshPermit | None:
    now = _utc(now)
    lease = acquire_file_lease(brain_refresh_lease_path(journal_path=journal_path))
    if lease is None:
        return None
    try:
        try:
            key = _load_or_generate_fingerprint_key(journal_path=journal_path)
        except Exception:
            lease.release()
            return None
        config = read_journal_config(journal_path)
        fingerprint = build_active_brain_fingerprint(config, hmac_key=key)
        path = brain_state_path(journal_path=journal_path)
        if fingerprint["active_lane"] in {"none", "unknown"} or not fingerprint["ok"]:
            _begin_nonrefresh_record(fingerprint, now, path=path)
            lease.release()
            return None
        run_id = run_id or uuid.uuid4().hex
        expires_at = now + CHECKING_TTL
        with hold_lock(path, mode=BRAIN_FILE_MODE):
            current = _read_record_unlocked(path)
            revision = _next_revision(current)
            marker_seen = _runtime_failure_marker_id(current)
            checking: BrainCheckingRecord = {
                "run_id": run_id,
                "started_at": _iso(now),
                "expires_at": _iso(expires_at),
                "fingerprint_sha256": fingerprint["fingerprint_sha256"],
                "checking_revision": revision,
                "runtime_failure_marker_seen": marker_seen,
            }
            record = _record(
                revision=revision,
                aggregate_state="checking",
                reason_code="checking_active",
                active_lane=fingerprint["active_lane"],
                active_provider=fingerprint["active_provider"],
                active_model=fingerprint["active_model"],
                fingerprint_sha256=fingerprint["fingerprint_sha256"],
                checking=checking,
                evidence=_empty_evidence(),
                runtime_failure_marker=current["runtime_failure_marker"]
                if current
                else None,
                diagnostic={},
                now=now,
            )
            _write_record(path, record)
        assert fingerprint["fingerprint_sha256"] is not None
        return BrainRefreshPermit(
            run_id=run_id,
            started_at=now,
            expires_at=expires_at,
            fingerprint_sha256=fingerprint["fingerprint_sha256"],
            checking_revision=revision,
            runtime_failure_marker_seen=marker_seen,
            lease=lease,
        )
    except BaseException:
        lease.release()
        raise


def _runtime_failure_marker(
    revision: int,
    reason_code: BrainReasonCode,
    now: datetime,
) -> BrainRuntimeFailureMarker:
    return {
        "marker_id": uuid.uuid4().hex,
        "revision": revision,
        "recorded_at": _iso(now),
        "reason_code": reason_code,
    }


def _load_fingerprint_for_write(
    *, journal_path: str | Path | None
) -> tuple[Mapping[str, Any], bytes | None, BrainFingerprintResult | None]:
    config = read_journal_config(journal_path)
    key = _load_existing_fingerprint_key(journal_path=journal_path)
    if key is None:
        return config, None, None
    return config, key, build_active_brain_fingerprint(config, hmac_key=key)


def _record_from_evidence(
    *,
    evidence: BrainEvidenceRecord,
    fingerprint: BrainFingerprintResult,
    revision: int,
    now: datetime,
    checking: BrainCheckingRecord | None,
    runtime_failure_marker: BrainRuntimeFailureMarker | None,
    diagnostic: Mapping[str, BrainDiagnosticValue] | None = None,
) -> BrainStateRecord:
    if fingerprint["ok"]:
        aggregate, reason = _reduce_evidence(evidence, now)
    else:
        reason = fingerprint["reason_code"] or "fingerprint_unavailable"
        aggregate = BRAIN_REASON_TO_AGGREGATE[reason]
    return _record(
        revision=revision,
        aggregate_state=aggregate,
        reason_code=reason,
        active_lane=fingerprint["active_lane"],
        active_provider=fingerprint["active_provider"],
        active_model=fingerprint["active_model"],
        fingerprint_sha256=fingerprint["fingerprint_sha256"],
        checking=checking,
        evidence=evidence,
        runtime_failure_marker=runtime_failure_marker,
        diagnostic=diagnostic or fingerprint["diagnostic"],
        now=now,
    )


def _validate_probe_outcome(outcome: BrainProbeOutcome) -> BrainEvidenceRecord:
    return _validate_evidence(outcome, "outcome")


def _assert_finish_allowed(
    permit: BrainRefreshPermit,
    current: BrainStateRecord | None,
    now: datetime,
) -> None:
    assert_file_lease_owned(permit.lease)
    if now >= permit.expires_at:
        raise BrainStateConflictError("brain refresh permit expired")
    if current is None or current["checking"] is None:
        raise BrainStateConflictError("brain refresh checking marker is absent")
    checking = current["checking"]
    if checking["run_id"] != permit.run_id:
        raise BrainStateConflictError("brain refresh run id changed")
    if checking["checking_revision"] != permit.checking_revision:
        raise BrainStateConflictError("brain refresh revision changed")
    if checking["runtime_failure_marker_seen"] != permit.runtime_failure_marker_seen:
        raise BrainStateConflictError("brain runtime failure marker changed")


def finish_brain_refresh(
    permit: BrainRefreshPermit,
    outcome: BrainProbeOutcome,
    now: datetime,
    *,
    journal_path: str | Path | None = None,
) -> BrainStateRecord:
    now = _utc(now)
    path = brain_state_path(journal_path=journal_path)
    try:
        evidence = _validate_probe_outcome(outcome)
        _config, key, fingerprint = _load_fingerprint_for_write(
            journal_path=journal_path
        )
        if key is None or fingerprint is None:
            raise BrainStateConflictError("brain fingerprint key is unavailable")
        with hold_lock(path, mode=BRAIN_FILE_MODE):
            current = _read_record_unlocked(path)
            _assert_finish_allowed(permit, current, now)
            if fingerprint["fingerprint_sha256"] != permit.fingerprint_sha256:
                raise BrainStateConflictError("brain fingerprint changed")
            assert current is not None
            revision = _next_revision(current)
            record = _record_from_evidence(
                evidence=evidence,
                fingerprint=fingerprint,
                revision=revision,
                now=now,
                checking=None,
                runtime_failure_marker=None,
            )
            return _write_record(path, record)
    finally:
        permit.release()


def abandon_brain_refresh(
    permit: BrainRefreshPermit,
    reason_code: BrainReasonCode,
    now: datetime,
    *,
    diagnostic: Mapping[str, BrainDiagnosticValue] | None = None,
    journal_path: str | Path | None = None,
) -> BrainStateRecord:
    now = _utc(now)
    path = brain_state_path(journal_path=journal_path)
    try:
        with hold_lock(path, mode=BRAIN_FILE_MODE):
            current = _read_record_unlocked(path)
            _assert_finish_allowed(permit, current, now)
            assert current is not None
            revision = _next_revision(current)
            aggregate = BRAIN_REASON_TO_AGGREGATE[reason_code]
            record = _record(
                revision=revision,
                aggregate_state=aggregate,
                reason_code=reason_code,
                active_lane=current["active_lane"],
                active_provider=current["active_provider"],
                active_model=current["active_model"],
                fingerprint_sha256=current["fingerprint_sha256"],
                checking=None,
                evidence=current["evidence"],
                runtime_failure_marker=current["runtime_failure_marker"],
                diagnostic=_validate_diagnostic(
                    diagnostic or {}, "diagnostic", reason_code
                ),
                now=now,
            )
            return _write_record(path, record)
    finally:
        permit.release()


def record_brain_runtime_failure(
    reason_code: BrainReasonCode,
    now: datetime,
    *,
    diagnostic: Mapping[str, BrainDiagnosticValue] | None = None,
    journal_path: str | Path | None = None,
) -> BrainStateRecord:
    now = _utc(now)
    path = brain_state_path(journal_path=journal_path)
    if BRAIN_REASON_TO_AGGREGATE[reason_code] not in RUNTIME_FAILURE_AGGREGATES:
        raise ValueError("brain runtime failure reason must be non-checking")
    diagnostic = _validate_diagnostic(diagnostic or {}, "diagnostic", reason_code)
    with hold_lock(path, mode=BRAIN_FILE_MODE):
        current = _read_record_unlocked(path)
        config, _key, fingerprint = _load_fingerprint_for_write(
            journal_path=journal_path
        )
        if fingerprint is None:
            lane, provider, model = _derive_lane(config)
            fingerprint = {
                "ok": False,
                "fingerprint_sha256": None,
                "active_lane": lane,
                "active_provider": provider,
                "active_model": model,
                "reason_code": "fingerprint_key_unavailable",
                "diagnostic": {},
            }
        revision = _next_revision(current)
        marker = _runtime_failure_marker(revision, reason_code, now)
        evidence = _empty_evidence()
        evidence["lane_prerequisites"] = _component(
            "failed",
            now,
            reason_code=reason_code,
            diagnostic=diagnostic,
        )
        record = _record_from_evidence(
            evidence=evidence,
            fingerprint=fingerprint,
            revision=revision,
            now=now,
            checking=None,
            runtime_failure_marker=marker,
            diagnostic=diagnostic,
        )
        return _write_record(path, record)


def runtime_phase_reason(phase: RuntimePhase) -> BrainReasonCode | None:
    return RUNTIME_PHASE_TO_REASON[phase]


__all__ = [
    "BRAIN_AGGREGATE_STATES",
    "BRAIN_COMPONENT_STATUSES",
    "BRAIN_REASON_CODES",
    "BRAIN_REASON_TO_AGGREGATE",
    "CHECKING_TTL",
    "DEFAULT_READY_EVIDENCE_TTL",
    "BrainAggregateState",
    "BrainComponentStatus",
    "BrainDiagnosticValue",
    "BrainEvidenceComponent",
    "BrainEvidenceRecord",
    "BrainFingerprintResult",
    "BrainInspectionStatus",
    "BrainLaneId",
    "BrainProbeOutcome",
    "BrainProjection",
    "BrainReasonCode",
    "BrainRefreshPermit",
    "BrainRuntimeFailureMarker",
    "BrainStateConflictError",
    "BrainStateInspection",
    "BrainStateRecord",
    "BrainStateValidationError",
    "abandon_brain_refresh",
    "brain_fingerprint_key_path",
    "brain_refresh_lease_path",
    "brain_state_path",
    "begin_brain_refresh",
    "build_active_brain_fingerprint",
    "finish_brain_refresh",
    "inspect_brain_state",
    "project_brain_state",
    "record_brain_runtime_failure",
    "runtime_phase_reason",
    "validate_brain_state_record",
]
