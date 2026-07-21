# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Persisted active-brain state for the single selected thinking lane.

Brain state owns only ``health/brain.json``, its HMAC fingerprint key, and its
refresh lease. Read-verb entry points classify existing state without repairing
or creating files; write-verb entry points are the only mutation surface.

Brain reasons are snake_case while ``runtime_health.py`` keeps kebab-case
provider runtime reasons. The boundary in this module translates runtime health
into the brain vocabulary rather than harmonizing the two owner contracts.

``ready-proof-unavailable`` is treated as non-ready for brain proof even though
``supervisor.py`` accepts it as startup-terminal and ready-adjacent for process
gating. Brain readiness requires the selected runtime's desired fingerprint to
be proved by an actual ``ready`` runtime record.

``local_runtime_state_stale`` is reserved in the vocabulary with no producer in
this phase. There is deliberately no wall-clock threshold and no synthetic stale
runtime status.

``local_runtime_fingerprint_mismatch`` is retained as recordable
lane-prerequisites evidence, but passive bundled inspection no longer produces
it; a change in the runtime's desired fingerprint projects as
``brain_config_changed``.
"""

from __future__ import annotations

import hashlib
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
    confidential_fingerprint_provenance_block,
    confidential_provenance_block,
    normalize_local_endpoint_url,
)
from solstone.think.providers.runtime_health import (
    ReasonCode,
    RuntimePhase,
    RuntimeRecordInspection,
    inspect_runtime_health,
)
from solstone.think.utils import CorruptConfigError, get_journal

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
LANE_COMPONENTS: dict[str, tuple[str, ...]] = {
    "none": ("configuration",),
    "bundled": COMPONENT_ORDER,
    "spp": COMPONENT_ORDER,
    "byo-cloud": COMPONENT_ORDER,
    "byo-endpoint": COMPONENT_ORDER,
}

BrainAggregateState = Literal["ready", "checking", "blocked", "unhealthy", "unknown"]
BrainComponentStatus = Literal["ok", "blocked", "failed", "unknown", "not_attempted"]
BrainLaneId = Literal["none", "bundled", "spp", "byo-cloud", "byo-endpoint"]
BrainInspectionStatus = Literal["ok", "corrupt", "unavailable"]
BrainDiagnosticValue = str | int | float | bool
# Brain reasons are deliberately snake_case. Runtime health reasons remain
# kebab-case and are translated at this module boundary.
BrainReasonCode = Literal[
    "brain_check_in_progress",
    "thinking_engine_not_chosen",
    "provider_key_missing",
    "endpoint_configuration_incomplete",
    "gpu_unavailable",
    "local_runtime_not_ready",
    "local_artifact_not_ready",
    "attestation_not_verified",
    "provider_key_invalid",
    "model_not_found",
    "provider_quota_exceeded",
    "provider_unavailable",
    "network_unreachable",
    "endpoint_unreachable",
    "endpoint_contract_failed",
    "chat_timeout",
    "provider_response_invalid",
    "cogitate_terminal_error",
    "attestation_rejected",
    "attestation_expired",
    "local_server_unhealthy",
    "configuration_invalid",
    "fingerprint_key_unavailable",
    "brain_record_missing",
    "brain_record_invalid",
    "brain_record_unavailable",
    "brain_record_stale",
    "brain_check_interrupted",
    "brain_config_changed",
    "brain_run_superseded",
    "probe_internal_error",
    "probe_output_starved",
    "local_runtime_state_invalid",
    "local_runtime_state_unavailable",
    "local_runtime_state_stale",
    "local_runtime_fingerprint_mismatch",
]

BRAIN_AGGREGATE_STATES = frozenset(
    {"ready", "checking", "blocked", "unhealthy", "unknown"}
)
BRAIN_COMPONENT_STATUSES = frozenset(
    {"ok", "blocked", "failed", "unknown", "not_attempted"}
)
BRAIN_LANES = frozenset(cast(tuple[str, ...], get_args(BrainLaneId)))
BRAIN_REASON_TO_AGGREGATE: dict[str, BrainAggregateState] = {
    "brain_check_in_progress": "checking",
    "thinking_engine_not_chosen": "blocked",
    "provider_key_missing": "blocked",
    "endpoint_configuration_incomplete": "blocked",
    "gpu_unavailable": "blocked",
    "local_runtime_not_ready": "blocked",
    "local_artifact_not_ready": "blocked",
    "attestation_not_verified": "blocked",
    "provider_key_invalid": "unhealthy",
    "model_not_found": "unhealthy",
    "provider_quota_exceeded": "unhealthy",
    "provider_unavailable": "unhealthy",
    "network_unreachable": "unhealthy",
    "endpoint_unreachable": "unhealthy",
    "endpoint_contract_failed": "unhealthy",
    "chat_timeout": "unhealthy",
    "provider_response_invalid": "unhealthy",
    "cogitate_terminal_error": "unhealthy",
    "attestation_rejected": "unhealthy",
    "attestation_expired": "unhealthy",
    "local_server_unhealthy": "unhealthy",
    "configuration_invalid": "unknown",
    "fingerprint_key_unavailable": "unknown",
    "brain_record_missing": "unknown",
    "brain_record_invalid": "unknown",
    "brain_record_unavailable": "unknown",
    "brain_record_stale": "unknown",
    "brain_check_interrupted": "unknown",
    "brain_config_changed": "unknown",
    "brain_run_superseded": "unknown",
    "probe_internal_error": "unknown",
    "probe_output_starved": "unknown",
    "local_runtime_state_invalid": "unknown",
    "local_runtime_state_unavailable": "unknown",
    "local_runtime_state_stale": "unknown",
    "local_runtime_fingerprint_mismatch": "unknown",
}
BRAIN_REASON_CODES = frozenset(cast(tuple[str, ...], get_args(BrainReasonCode)))
RUNTIME_FAILURE_AGGREGATES: frozenset[BrainAggregateState] = frozenset(
    {"blocked", "unhealthy", "unknown"}
)
BRAIN_EVIDENCE_REASON_CODES: dict[str, frozenset[str]] = {
    "configuration": frozenset(
        {
            "thinking_engine_not_chosen",
            "endpoint_configuration_incomplete",
        }
    ),
    "lane_prerequisites": frozenset(
        {
            "provider_key_missing",
            "gpu_unavailable",
            "local_runtime_not_ready",
            "local_artifact_not_ready",
            "attestation_not_verified",
            "attestation_rejected",
            "attestation_expired",
            "local_server_unhealthy",
            "local_runtime_state_invalid",
            "local_runtime_state_unavailable",
            "local_runtime_state_stale",
            "local_runtime_fingerprint_mismatch",
            "probe_internal_error",
        }
    ),
    "generate": frozenset(
        {
            "provider_key_invalid",
            "model_not_found",
            "provider_quota_exceeded",
            "provider_unavailable",
            "network_unreachable",
            "endpoint_unreachable",
            "endpoint_contract_failed",
            "chat_timeout",
            "provider_response_invalid",
            "local_server_unhealthy",
            "probe_internal_error",
            "probe_output_starved",
        }
    ),
    "cogitate": frozenset(
        {
            "provider_key_invalid",
            "model_not_found",
            "provider_quota_exceeded",
            "provider_unavailable",
            "network_unreachable",
            "endpoint_unreachable",
            "endpoint_contract_failed",
            "chat_timeout",
            "provider_response_invalid",
            "local_server_unhealthy",
            "probe_internal_error",
            "cogitate_terminal_error",
        }
    ),
}
BRAIN_PROJECTION_ONLY_REASON_CODES = frozenset(
    {
        "configuration_invalid",
        "fingerprint_key_unavailable",
        "brain_record_missing",
        "brain_record_invalid",
        "brain_record_unavailable",
        "brain_record_stale",
        "brain_check_in_progress",
        "brain_check_interrupted",
        "brain_config_changed",
        "brain_run_superseded",
    }
)

if BRAIN_AGGREGATE_STATES & BRAIN_REASON_CODES:
    raise RuntimeError("brain aggregate-state and reason-code vocabularies overlap")
if BRAIN_COMPONENT_STATUSES & BRAIN_REASON_CODES:
    raise RuntimeError("brain component-status and reason-code vocabularies overlap")
if set(BRAIN_REASON_TO_AGGREGATE) != BRAIN_REASON_CODES:
    raise RuntimeError("brain reason aggregate table does not match reason vocabulary")
_EVIDENCE_ALLOWED_REASON_CODES = frozenset().union(
    *BRAIN_EVIDENCE_REASON_CODES.values()
)
if len(_EVIDENCE_ALLOWED_REASON_CODES) != 26:
    raise RuntimeError("brain evidence reason partition must contain 26 reasons")
if len(BRAIN_PROJECTION_ONLY_REASON_CODES) != 10:
    raise RuntimeError("brain projection-only reason partition must contain 10 reasons")
if _EVIDENCE_ALLOWED_REASON_CODES & BRAIN_PROJECTION_ONLY_REASON_CODES:
    raise RuntimeError("brain evidence and projection-only reason partitions overlap")
if (
    _EVIDENCE_ALLOWED_REASON_CODES | BRAIN_PROJECTION_ONLY_REASON_CODES
    != BRAIN_REASON_CODES
):
    raise RuntimeError("brain reason partitions do not cover the vocabulary")

RUNTIME_PHASES = frozenset(cast(tuple[str, ...], get_args(RuntimePhase)))
RUNTIME_REASON_CODES = frozenset(cast(tuple[str, ...], get_args(ReasonCode)))
RUNTIME_PHASE_TO_REASON: dict[str, BrainReasonCode | None] = {
    "ready": None,
    "not-desired": "local_runtime_not_ready",
    "observing": "local_runtime_not_ready",
    "artifact-not-ready": "local_artifact_not_ready",
    "host-blocked": "local_runtime_not_ready",
    "starting": "local_runtime_not_ready",
    "warming": "local_runtime_not_ready",
    "backoff": "local_runtime_not_ready",
    "retry-requested": "local_runtime_not_ready",
    "ready-proof-unavailable": "local_runtime_state_unavailable",
    "stop-deferred": "local_runtime_not_ready",
    "stopping": "local_runtime_not_ready",
    "stopped": "local_runtime_not_ready",
    "failed": "local_server_unhealthy",
    "cleanup-failed": "local_server_unhealthy",
    "state-corrupt": "local_runtime_state_invalid",
    "state-unavailable": "local_runtime_state_unavailable",
}
if set(RUNTIME_PHASE_TO_REASON) != RUNTIME_PHASES:
    raise RuntimeError("brain runtime phase lattice must cover every runtime phase")

RUNTIME_REASON_TO_BRAIN_REASON: dict[str, BrainReasonCode] = {
    "gpu-unavailable": "gpu_unavailable",
    "gpu-probe-failed": "local_runtime_state_unavailable",
    "install-in-progress": "local_runtime_not_ready",
    "artifact-missing": "local_artifact_not_ready",
    "artifact-stale": "local_artifact_not_ready",
    "artifact-proof-failed": "local_artifact_not_ready",
    "record-malformed": "local_runtime_state_invalid",
    "record-unavailable": "local_runtime_state_unavailable",
    "proof-observation-unavailable": "local_runtime_state_unavailable",
    "ready-with-proof-observation-unavailable": "local_runtime_state_unavailable",
}
INCOHERENT_RUNTIME_PHASE_REASON_CODES: frozenset[tuple[str, str]] = frozenset(
    {
        ("ready", "artifact-missing"),
        ("ready", "artifact-stale"),
        ("ready", "artifact-proof-failed"),
        ("ready", "gpu-unavailable"),
        ("ready", "gpu-probe-failed"),
        ("ready", "install-in-progress"),
        ("ready", "record-malformed"),
        ("ready", "record-unavailable"),
        ("ready", "proof-observation-unavailable"),
    }
)
RUNTIME_TRANSITION_PHASES = frozenset(
    {"observing", "starting", "warming", "retry-requested"}
)

DiagnosticMetadataSchema = dict[str, frozenset[str]]
CONFIG_DIAGNOSTIC_FIELDS = frozenset({"providers.active.provider"})
DIAGNOSTIC_METADATA_SCHEMAS: dict[str, DiagnosticMetadataSchema] = {
    reason: {} for reason in BRAIN_REASON_CODES
}
DIAGNOSTIC_METADATA_SCHEMAS.update(
    {
        "configuration_invalid": {"field": CONFIG_DIAGNOSTIC_FIELDS},
        "gpu_unavailable": {
            "phase": RUNTIME_PHASES,
            "runtime_reason": RUNTIME_REASON_CODES,
        },
        "local_runtime_not_ready": {
            "phase": RUNTIME_PHASES,
            "runtime_reason": RUNTIME_REASON_CODES,
        },
        "local_artifact_not_ready": {
            "phase": RUNTIME_PHASES,
            "runtime_reason": RUNTIME_REASON_CODES,
        },
        "local_server_unhealthy": {
            "phase": RUNTIME_PHASES,
            "runtime_reason": RUNTIME_REASON_CODES,
        },
        "local_runtime_state_invalid": {
            "phase": RUNTIME_PHASES,
            "runtime_reason": RUNTIME_REASON_CODES,
        },
        "local_runtime_state_unavailable": {
            "phase": RUNTIME_PHASES,
            "runtime_reason": RUNTIME_REASON_CODES,
        },
        "local_runtime_fingerprint_mismatch": {"phase": RUNTIME_PHASES},
        "probe_internal_error": {"phase": RUNTIME_PHASES},
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
    active_lane: BrainLaneId | None
    active_provider: str | None
    active_model: str | None
    fingerprint_sha256: str | None
    runtime_transition_in_progress: bool


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
    active_lane: BrainLaneId | None
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


BrainRuntimeFailureComponent = Literal["lane_prerequisites", "generate", "cogitate"]
BrainRuntimeFailureRejectedReason = Literal[
    "reason_not_recordable",
    "component_reason_not_allowed",
    "fingerprint_mismatch",
    "fingerprint_not_available",
    "state_unavailable",
]


class BrainRuntimeFailureResult(TypedDict):
    accepted: bool
    record: BrainStateRecord | None
    rejected_reason: BrainRuntimeFailureRejectedReason | None
    error: str | None


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


def probe_brain_refresh_lease_held(*, journal_path: str | Path | None = None) -> bool:
    """Report whether a brain-refresh permit lease is currently held.

    Owner-routed read of the ``health/brain-refresh.lease`` domain artifact, so
    non-owner callers (the ``journal brain`` CLI) can tell a busy refresh apart
    from other reasons a permit was declined without importing the raw
    ``journal_io`` lease primitive. Never mutates state; may propagate the
    underlying ``OSError`` for the caller to interpret.
    """
    return probe_file_lease_held(brain_refresh_lease_path(journal_path=journal_path))


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
) -> tuple[Literal["missing", "partial", "complete"], str, str, str | None]:
    local = _local_config(config)
    endpoint_url = str(local.get("endpoint_url") or "").strip()
    served_model_id = str(local.get("served_model_id") or "").strip()
    if endpoint_url and served_model_id:
        credential = local.get("credential")
        return (
            "complete",
            normalize_local_endpoint_url(endpoint_url),
            served_model_id,
            str(credential) if credential is not None else None,
        )
    if endpoint_url or served_model_id:
        return "partial", "", "", None
    return "missing", "", "", None


def _spp_provenance_matches(config: Mapping[str, Any]) -> bool:
    endpoint_state, base_url, served_model_id, credential = _local_endpoint_from_config(
        config
    )
    if endpoint_state != "complete" or credential is None:
        return False
    block = confidential_provenance_block(dict(config))
    if not isinstance(block, Mapping):
        return False
    block_url = block.get("endpoint_url")
    block_model = block.get("served_model_id")
    block_fingerprint = block.get("credential_fingerprint_sha256")
    if not isinstance(block_url, str):
        return False
    if not isinstance(block_model, str) or block_model != served_model_id:
        return False
    if not isinstance(block_fingerprint, str):
        return False
    credential_fingerprint = hashlib.sha256(credential.encode("utf-8")).hexdigest()
    return (
        normalize_local_endpoint_url(block_url) == base_url
        and block_fingerprint == credential_fingerprint
    )


def _derive_lane(
    config: Mapping[str, Any],
) -> tuple[BrainLaneId | None, str | None, str | None]:
    provider, model = _active_config(config)
    if provider == "none":
        return "none", "none", None
    if provider in CLOUD_BYO_PROVIDERS:
        return "byo-cloud", provider, model
    if provider == "local":
        endpoint_state, _, _, _ = _local_endpoint_from_config(config)
        if endpoint_state == "missing":
            return "bundled", provider, model
        # A half-configured endpoint leaves the lane unresolved and projects
        # configuration_invalid without writing. endpoint_configuration_incomplete
        # is only valid as explicit configuration evidence on a resolved lane.
        if endpoint_state == "partial":
            return None, provider, model
        if _spp_provenance_matches(config):
            return "spp", provider, model
        if confidential_provenance_block(dict(config)) is not None:
            return None, provider, model
        return "byo-endpoint", provider, model
    return None, provider, model


def _bundled_runtime_fingerprint_sha() -> str:
    if sys.platform == "darwin":
        from solstone.think.providers import mlx_install

        target = mlx_install.target_fingerprint()
    else:
        from solstone.think.providers import local_install

        target = local_install.target_fingerprint(LOCAL_MODEL)
    return fingerprint_sha256(canonical_fingerprint(target))


def build_active_brain_fingerprint(
    config: Mapping[str, Any],
    *,
    hmac_key: bytes,
    bundled_runtime_fingerprint_sha256: str | None = None,
) -> BrainFingerprintResult:
    lane, provider, model = _derive_lane(config)
    diagnostic: dict[str, BrainDiagnosticValue] = {}
    resolved_bundled_runtime_fingerprint_sha256: str | None = None
    if lane is None:
        return {
            "ok": False,
            "fingerprint_sha256": None,
            "active_lane": None,
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
            block = confidential_fingerprint_provenance_block(dict(config))
            components["confidential"] = (
                _canonical_digest(block, hmac_key=hmac_key)
                if block is not None
                else None
            )
    elif lane == "bundled":
        if bundled_runtime_fingerprint_sha256 is None:
            try:
                resolved_bundled_runtime_fingerprint_sha256 = (
                    _bundled_runtime_fingerprint_sha()
                )
            except Exception:
                return {
                    "ok": False,
                    "fingerprint_sha256": None,
                    "active_lane": lane,
                    "active_provider": provider,
                    "active_model": model,
                    "reason_code": "local_runtime_state_unavailable",
                    "diagnostic": {},
                    "bundled_runtime_fingerprint_sha256": None,
                }
        else:
            resolved_bundled_runtime_fingerprint_sha256 = (
                bundled_runtime_fingerprint_sha256
            )
        components["bundled_runtime"] = resolved_bundled_runtime_fingerprint_sha256

    fingerprint = fingerprint_sha256(canonical_fingerprint(components))
    return {
        "ok": True,
        "fingerprint_sha256": fingerprint,
        "active_lane": lane,
        "active_provider": provider,
        "active_model": model,
        "reason_code": None,
        "diagnostic": diagnostic,
        "bundled_runtime_fingerprint_sha256": (
            resolved_bundled_runtime_fingerprint_sha256
        ),
    }


def _projection(
    aggregate_state: BrainAggregateState,
    reason_code: BrainReasonCode | None,
    *,
    active_lane: BrainLaneId | None = None,
    active_provider: str | None = None,
    active_model: str | None = None,
    fingerprint_sha256: str | None = None,
    runtime_transition_in_progress: bool = False,
) -> BrainProjection:
    return {
        "aggregate_state": aggregate_state,
        "reason_code": reason_code,
        "active_lane": active_lane,
        "active_provider": active_provider,
        "active_model": active_model,
        "fingerprint_sha256": fingerprint_sha256,
        "runtime_transition_in_progress": runtime_transition_in_progress,
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


def _component_status_for_reason(reason_code: BrainReasonCode) -> BrainComponentStatus:
    aggregate = BRAIN_REASON_TO_AGGREGATE[reason_code]
    if aggregate == "blocked":
        return "blocked"
    if aggregate == "unhealthy":
        return "failed"
    if aggregate == "unknown":
        return "unknown"
    raise BrainStateValidationError(
        "reason_code", "checking reason is not valid component evidence"
    )


def _lane_applicable_components(lane: BrainLaneId) -> tuple[str, ...]:
    return LANE_COMPONENTS[lane]


def _component_reason(component: BrainEvidenceComponent) -> BrainReasonCode | None:
    return component.get("reason_code")


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


def _validate_component(
    value: Any, path: str, *, component_name: str
) -> BrainEvidenceComponent | None:
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
    status = cast(BrainComponentStatus, status)
    observed_at = _parse_timestamp(value.get("observed_at"), f"{path}.observed_at")
    reason = _validate_reason(
        value.get("reason_code"), f"{path}.reason_code", nullable=True
    )
    if status == "ok":
        if reason is not None:
            raise BrainStateValidationError(
                f"{path}.reason_code", "ok evidence requires null reason"
            )
    elif reason is None:
        raise BrainStateValidationError(
            f"{path}.reason_code", "non-ok evidence requires reason"
        )
    elif status != "not_attempted":
        if reason not in BRAIN_EVIDENCE_REASON_CODES[component_name]:
            raise BrainStateValidationError(
                f"{path}.reason_code", "reason not allowed for evidence component"
            )
        if status != _component_status_for_reason(reason):
            raise BrainStateValidationError(
                f"{path}.status", "component status does not match reason aggregate"
            )
    diagnostic = _validate_diagnostic(
        value.get("diagnostic", {}), f"{path}.diagnostic", reason
    )

    component: BrainEvidenceComponent = {
        "status": status,
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
    evidence: BrainEvidenceRecord = {
        "configuration": _validate_component(
            value.get("configuration"),
            f"{path}.configuration",
            component_name="configuration",
        ),
        "lane_prerequisites": _validate_component(
            value.get("lane_prerequisites"),
            f"{path}.lane_prerequisites",
            component_name="lane_prerequisites",
        ),
        "generate": _validate_component(
            value.get("generate"), f"{path}.generate", component_name="generate"
        ),
        "cogitate": _validate_component(
            value.get("cogitate"), f"{path}.cogitate", component_name="cogitate"
        ),
    }
    causal_reason: BrainReasonCode | None = None
    for causal_name in ("configuration", "lane_prerequisites"):
        causal = evidence[causal_name]
        if causal is not None and causal["status"] != "ok":
            causal_reason = causal.get("reason_code")
            break
    for component_name in ("generate", "cogitate"):
        component = evidence[component_name]
        if component is None or component["status"] != "not_attempted":
            continue
        if causal_reason is None:
            raise BrainStateValidationError(
                f"{path}.{component_name}.status",
                "not_attempted requires a non-ok prerequisite",
            )
        if component.get("reason_code") != causal_reason:
            raise BrainStateValidationError(
                f"{path}.{component_name}.reason_code",
                "not_attempted reason must repeat causal prerequisite reason",
            )
    for component_name in ("configuration", "lane_prerequisites"):
        component = evidence[component_name]
        if component is not None and component["status"] == "not_attempted":
            raise BrainStateValidationError(
                f"{path}.{component_name}.status",
                "not_attempted is only valid for generate/cogitate",
            )
    return evidence


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
    fingerprint_sha = _validate_hex(
        record.get("fingerprint_sha256"), "fingerprint_sha256"
    )
    evidence = _validate_evidence(record.get("evidence"), "evidence")
    runtime_failure_marker = _validate_runtime_failure_marker(
        record.get("runtime_failure_marker"), "runtime_failure_marker"
    )
    updated_at = _parse_timestamp(record.get("updated_at"), "updated_at")
    validated: BrainStateRecord = {
        "schema_version": SCHEMA_VERSION,
        "revision": revision,
        "aggregate_state": cast(BrainAggregateState, aggregate),
        "reason_code": reason,
        "active_lane": cast(BrainLaneId, lane),
        "active_provider": provider,
        "active_model": model,
        "fingerprint_sha256": fingerprint_sha,
        "checking": checking,
        "evidence": evidence,
        "runtime_failure_marker": runtime_failure_marker,
        "diagnostic": diagnostic,
        "updated_at": updated_at.isoformat(),
    }
    if (aggregate == "checking") != (checking is not None):
        raise BrainStateValidationError(
            "checking", "checking aggregate requires matching checking marker"
        )
    if aggregate == "ready" and reason is not None:
        raise BrainStateValidationError("reason_code", "ready requires null reason")
    if aggregate != "ready":
        if reason is None:
            raise BrainStateValidationError(
                "reason_code", "non-ready aggregate requires reason"
            )
        if BRAIN_REASON_TO_AGGREGATE[reason] != aggregate:
            raise BrainStateValidationError(
                "reason_code", "reason aggregate does not match record aggregate"
            )
    if aggregate == "checking" and reason != "brain_check_in_progress":
        raise BrainStateValidationError(
            "reason_code", "checking aggregate requires brain_check_in_progress"
        )
    if runtime_failure_marker is not None:
        marker_reason = runtime_failure_marker["reason_code"]
        if (
            marker_reason in BRAIN_PROJECTION_ONLY_REASON_CODES
            or BRAIN_REASON_TO_AGGREGATE[marker_reason] == "checking"
        ):
            raise BrainStateValidationError(
                "runtime_failure_marker.reason_code",
                "runtime failure marker reason is not recordable",
            )
    reduced_aggregate, reduced_reason = _reduce_evidence(
        evidence,
        updated_at,
        active_lane=cast(BrainLaneId, lane),
        checking=checking,
        refresh_permit_active=True,
        runtime_failure_reason=_active_runtime_failure_reason(validated),
    )
    if reduced_reason == "brain_record_invalid":
        raise BrainStateValidationError(
            "evidence", "missing lane-applicable evidence without higher-priority cause"
        )
    if aggregate != reduced_aggregate or reason != reduced_reason:
        raise BrainStateValidationError(
            "aggregate_state", "record aggregate/reason does not match evidence"
        )
    return validated


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


def _record_timestamp_invalid(record: BrainStateRecord, now: datetime) -> bool:
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
                return True
        for value in expiry_timestamps:
            _parse_timestamp(value, "timestamp")
    except BrainStateValidationError:
        return True
    return False


def _active_runtime_failure_reason(record: BrainStateRecord) -> BrainReasonCode | None:
    marker = record["runtime_failure_marker"]
    if marker is None:
        return None
    checking = record["checking"]
    if checking is None:
        if marker["revision"] == record["revision"]:
            return marker["reason_code"]
        return None
    if marker["marker_id"] == checking["runtime_failure_marker_seen"]:
        return None
    marker_recorded_at = _parse_timestamp(
        marker["recorded_at"], "runtime_failure_marker.recorded_at"
    )
    checking_started_at = _parse_timestamp(
        checking["started_at"], "checking.started_at"
    )
    if (
        marker["revision"] >= checking["checking_revision"]
        or marker_recorded_at >= checking_started_at
    ):
        return marker["reason_code"]
    return None


def _candidate(
    priority: int,
    component_name: str,
    reason_code: BrainReasonCode,
) -> tuple[int, int, BrainAggregateState, BrainReasonCode]:
    return (
        priority,
        COMPONENT_ORDER.index(component_name)
        if component_name in COMPONENT_ORDER
        else -1,
        BRAIN_REASON_TO_AGGREGATE[reason_code],
        reason_code,
    )


def _component_candidate(
    component_name: str,
    component: BrainEvidenceComponent | None,
    now: datetime,
) -> tuple[int, int, BrainAggregateState, BrainReasonCode] | None:
    if component is None:
        return _candidate(4, component_name, "brain_record_invalid")
    status = component["status"]
    if status == "ok":
        expires_at = _parse_timestamp(
            component.get("expires_at"), f"evidence.{component_name}.expires_at"
        )
        if now >= expires_at:
            return _candidate(4, component_name, "brain_record_stale")
        return None
    if status == "not_attempted":
        return None
    reason = _component_reason(component)
    if reason is None:
        return _candidate(4, component_name, "brain_record_invalid")
    if status == "failed":
        return _candidate(2, component_name, reason)
    if status == "blocked":
        return _candidate(3, component_name, reason)
    return _candidate(4, component_name, reason)


def _runtime_component(
    reason_code: BrainReasonCode, now: datetime
) -> BrainEvidenceComponent:
    return _component(
        _component_status_for_reason(reason_code),
        now,
        reason_code=reason_code,
    )


def _reduce_evidence(
    evidence: BrainEvidenceRecord,
    now: datetime,
    *,
    active_lane: BrainLaneId,
    checking: BrainCheckingRecord | None = None,
    refresh_permit_active: bool = True,
    runtime_failure_reason: BrainReasonCode | None = None,
    runtime_reason: BrainReasonCode | None = None,
) -> tuple[BrainAggregateState, BrainReasonCode | None]:
    candidates: list[tuple[int, int, BrainAggregateState, BrainReasonCode]] = []
    if runtime_failure_reason is not None:
        candidates.append(_candidate(0, "lane_prerequisites", runtime_failure_reason))
    if checking is not None:
        expires_at = _parse_timestamp(checking["expires_at"], "checking.expires_at")
        if now < expires_at and refresh_permit_active:
            candidates.append(_candidate(1, "configuration", "brain_check_in_progress"))
        else:
            candidates.append(_candidate(4, "configuration", "brain_check_interrupted"))

    effective_evidence: dict[str, BrainEvidenceComponent | None] = dict(evidence)
    if runtime_reason is not None:
        effective_evidence["lane_prerequisites"] = _runtime_component(
            runtime_reason, now
        )
    for component_name in _lane_applicable_components(active_lane):
        candidate = _component_candidate(
            component_name, effective_evidence[component_name], now
        )
        if candidate is not None:
            candidates.append(candidate)
    if candidates:
        _priority, _component_order, aggregate, reason = min(candidates)
        return aggregate, reason
    return "ready", None


def _valid_runtime_fingerprint_sha256(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 64:
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return value


def _bundled_runtime_projection_inputs(
    runtime_health: RuntimeRecordInspection | None,
) -> tuple[BrainReasonCode | None, str | None]:
    if runtime_health is None:
        return "local_runtime_state_unavailable", None
    if runtime_health["status"] == "corrupt":
        return "local_runtime_state_invalid", None
    if runtime_health["status"] == "unavailable":
        return "local_runtime_state_unavailable", None
    if runtime_health["status"] != "ok":
        return "local_runtime_state_unavailable", None
    record = runtime_health["record"]
    if not isinstance(record, Mapping):
        return "local_runtime_state_unavailable", None
    phase = record.get("phase")
    if not isinstance(phase, str) or phase not in RUNTIME_PHASE_TO_REASON:
        return "local_runtime_state_invalid", None
    runtime_reason = record.get("reason_code")
    if (
        isinstance(runtime_reason, str)
        and (
            phase,
            runtime_reason,
        )
        in INCOHERENT_RUNTIME_PHASE_REASON_CODES
    ):
        return "local_runtime_state_invalid", None
    desired = _valid_runtime_fingerprint_sha256(
        record.get("desired_fingerprint_sha256")
    )
    if isinstance(runtime_reason, str):
        mapped_reason = RUNTIME_REASON_TO_BRAIN_REASON.get(runtime_reason)
        if mapped_reason is not None:
            return mapped_reason, desired
    phase_reason = RUNTIME_PHASE_TO_REASON[phase]
    if phase_reason is not None:
        return phase_reason, desired
    if desired is None:
        return "local_runtime_state_invalid", None
    return None, desired


def _runtime_transition_in_progress(
    active_lane: BrainLaneId | None,
    runtime_health: RuntimeRecordInspection | None,
) -> bool:
    if (
        active_lane != "bundled"
        or runtime_health is None
        or runtime_health["status"] != "ok"
    ):
        return False
    record = runtime_health["record"]
    if not isinstance(record, Mapping):
        return False
    phase = record.get("phase")
    runtime_reason = record.get("reason_code")
    return phase in RUNTIME_TRANSITION_PHASES or runtime_reason == "install-in-progress"


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
    lease can project ``unknown / brain_check_interrupted`` before the
    ten-minute checking expiry. The expiry remains the authoritative backstop.
    """

    now = _utc(now)
    lane, provider, model = _derive_lane(config)
    if lane is None:
        return _projection(
            "unknown",
            "configuration_invalid",
            active_lane=None,
            active_provider=provider,
            active_model=model,
            fingerprint_sha256=record["fingerprint_sha256"] if record else None,
        )
    if record is None:
        return _projection(
            "unknown",
            "brain_record_missing",
            active_lane=lane,
            active_provider=provider,
            active_model=model,
        )
    if lane == "none" and record["active_lane"] == "none":
        aggregate, reason = _reduce_evidence(
            record["evidence"],
            now,
            active_lane=record["active_lane"],
            checking=record["checking"],
            refresh_permit_active=refresh_permit_active,
            runtime_failure_reason=_active_runtime_failure_reason(record),
        )
        return _projection(
            aggregate,
            reason,
            active_lane=record["active_lane"],
            active_provider=record["active_provider"],
            active_model=record["active_model"],
            fingerprint_sha256=record["fingerprint_sha256"],
        )
    if hmac_key is None:
        return _projection(
            "unknown",
            "fingerprint_key_unavailable",
            active_lane=record["active_lane"],
            active_provider=record["active_provider"],
            active_model=record["active_model"],
            fingerprint_sha256=record["fingerprint_sha256"],
        )
    if _record_timestamp_invalid(record, now):
        return _projection(
            "unknown",
            "brain_record_invalid",
            active_lane=record["active_lane"],
            active_provider=record["active_provider"],
            active_model=record["active_model"],
            fingerprint_sha256=record["fingerprint_sha256"],
        )
    runtime_reason: BrainReasonCode | None = None
    injected_bundled_runtime_fingerprint_sha256: str | None = None
    runtime_transition = _runtime_transition_in_progress(lane, runtime_health)
    if lane == "bundled":
        runtime_reason, injected_bundled_runtime_fingerprint_sha256 = (
            _bundled_runtime_projection_inputs(runtime_health)
        )
        if injected_bundled_runtime_fingerprint_sha256 is None:
            aggregate, reason = _reduce_evidence(
                record["evidence"],
                now,
                active_lane=record["active_lane"],
                checking=record["checking"],
                refresh_permit_active=refresh_permit_active,
                runtime_failure_reason=_active_runtime_failure_reason(record),
                runtime_reason=runtime_reason,
            )
            return _projection(
                aggregate,
                reason,
                active_lane=record["active_lane"],
                active_provider=record["active_provider"],
                active_model=record["active_model"],
                fingerprint_sha256=record["fingerprint_sha256"],
                runtime_transition_in_progress=runtime_transition,
            )
    fingerprint = build_active_brain_fingerprint(
        config,
        hmac_key=hmac_key,
        bundled_runtime_fingerprint_sha256=injected_bundled_runtime_fingerprint_sha256,
    )
    if not fingerprint["ok"]:
        reason = fingerprint["reason_code"] or "configuration_invalid"
        return _projection(
            BRAIN_REASON_TO_AGGREGATE[reason],
            reason,
            active_lane=fingerprint["active_lane"],
            active_provider=fingerprint["active_provider"],
            active_model=fingerprint["active_model"],
            fingerprint_sha256=record["fingerprint_sha256"],
        )
    if record["fingerprint_sha256"] != fingerprint["fingerprint_sha256"]:
        return _projection(
            "unknown",
            "brain_config_changed",
            active_lane=fingerprint["active_lane"],
            active_provider=fingerprint["active_provider"],
            active_model=fingerprint["active_model"],
            fingerprint_sha256=record["fingerprint_sha256"],
        )
    aggregate, reason = _reduce_evidence(
        record["evidence"],
        now,
        active_lane=record["active_lane"],
        checking=record["checking"],
        refresh_permit_active=refresh_permit_active,
        runtime_failure_reason=_active_runtime_failure_reason(record),
        runtime_reason=runtime_reason,
    )
    return _projection(
        aggregate,
        reason,
        active_lane=record["active_lane"],
        active_provider=record["active_provider"],
        active_model=record["active_model"],
        fingerprint_sha256=record["fingerprint_sha256"],
        runtime_transition_in_progress=runtime_transition,
    )


def inspect_brain_state(
    now: datetime,
    *,
    journal_path: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> BrainStateInspection:
    now = _utc(now)
    try:
        path = brain_state_path(journal_path=journal_path)
    except Exception as exc:
        return _runtime_failure_result(
            accepted=False, rejected_reason="state_unavailable", error=str(exc)
        )
    status: BrainInspectionStatus = "ok"
    record_reason: BrainReasonCode | None = None
    error: str | None = None
    try:
        record = _read_record_unlocked(path)
    except OSError as exc:
        record = None
        status = "unavailable"
        record_reason = "brain_record_unavailable"
        error = str(exc)
    except (BrainStateValidationError, MalformedDataError, json.JSONDecodeError) as exc:
        record = None
        status = "corrupt"
        record_reason = "brain_record_invalid"
        error = str(exc)
    if record is None and record_reason is None:
        status = "unavailable"
        record_reason = "brain_record_missing"
    if config is None:
        try:
            config = read_journal_config(journal_path)
        except (CorruptConfigError, OSError) as exc:
            projection = _projection("unknown", "configuration_invalid")
            return {
                "status": status,
                "path": str(path),
                "record": record,
                "projection": projection,
                "reason_code": "configuration_invalid",
                "error": str(exc),
            }
    lane, provider, model = _derive_lane(config)
    if lane is None:
        projection = _projection(
            "unknown",
            "configuration_invalid",
            active_lane=None,
            active_provider=provider,
            active_model=model,
            fingerprint_sha256=record["fingerprint_sha256"] if record else None,
        )
        return {
            "status": status,
            "path": str(path),
            "record": record,
            "projection": projection,
            "reason_code": "configuration_invalid",
            "error": error,
        }
    if record is None:
        projection = _projection(
            "unknown",
            record_reason or "brain_record_missing",
            active_lane=lane,
            active_provider=provider,
            active_model=model,
        )
        return {
            "status": status,
            "path": str(path),
            "record": None,
            "projection": projection,
            "reason_code": record_reason,
            "error": error,
        }
    if lane == "none":
        projection = project_brain_state(
            record,
            now,
            config=config,
            hmac_key=None,
            refresh_permit_active=False,
            runtime_health=None,
        )
        return {
            "status": status,
            "path": str(path),
            "record": record,
            "projection": projection,
            "reason_code": projection["reason_code"],
            "error": error,
        }
    hmac_key = _load_existing_fingerprint_key(journal_path=journal_path)
    if hmac_key is None:
        projection = _projection(
            "unknown",
            "fingerprint_key_unavailable",
            active_lane=record["active_lane"],
            active_provider=record["active_provider"],
            active_model=record["active_model"],
            fingerprint_sha256=record["fingerprint_sha256"],
        )
        return {
            "status": status,
            "path": str(path),
            "record": record,
            "projection": projection,
            "reason_code": "fingerprint_key_unavailable",
            "error": error,
        }
    runtime_health: RuntimeRecordInspection | None = None
    if lane == "bundled":
        try:
            runtime_health = inspect_runtime_health("local", journal_path=journal_path)
        except Exception as exc:
            runtime_health = {
                "status": "unavailable",
                "provider": "local",
                "record_kind": "health",
                "path": str(
                    Path(journal_path or get_journal())
                    / "health"
                    / "providers"
                    / "runtime"
                    / "local.json"
                ),
                "record": None,
                "reason_code": "record-unavailable",
                "error": str(exc),
            }
    refresh_permit_active = False
    if record["checking"] is not None:
        try:
            refresh_permit_active = probe_file_lease_held(
                brain_refresh_lease_path(journal_path=journal_path)
            )
        except OSError as exc:
            projection = _projection(
                "unknown",
                "brain_check_interrupted",
                active_lane=record["active_lane"],
                active_provider=record["active_provider"],
                active_model=record["active_model"],
                fingerprint_sha256=record["fingerprint_sha256"],
            )
            return {
                "status": status,
                "path": str(path),
                "record": record,
                "projection": projection,
                "reason_code": "brain_check_interrupted",
                "error": str(exc),
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
    now: datetime,
    *,
    path: Path,
    active_provider: str | None,
    active_model: str | None,
) -> None:
    with hold_lock(path, mode=BRAIN_FILE_MODE):
        try:
            current = _read_record_unlocked(path)
        except (BrainStateValidationError, MalformedDataError, json.JSONDecodeError):
            current = None
        revision = _next_revision(current)
        reason: BrainReasonCode = "thinking_engine_not_chosen"
        evidence = _empty_evidence()
        evidence["configuration"] = _component(
            "blocked", now, reason_code="thinking_engine_not_chosen"
        )
        record = _record(
            revision=revision,
            aggregate_state="blocked",
            reason_code=reason,
            active_lane="none",
            active_provider=active_provider,
            active_model=active_model,
            fingerprint_sha256=None,
            checking=None,
            evidence=evidence,
            runtime_failure_marker=None,
            diagnostic={},
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
    try:
        config = read_journal_config(journal_path)
    except (CorruptConfigError, OSError):
        return None
    lane, provider, model = _derive_lane(config)
    path = brain_state_path(journal_path=journal_path)
    if lane is None:
        return None
    if lane == "none":
        _begin_nonrefresh_record(
            now,
            path=path,
            active_provider=provider,
            active_model=model,
        )
        return None
    lease = acquire_file_lease(brain_refresh_lease_path(journal_path=journal_path))
    if lease is None:
        return None
    try:
        try:
            key = _load_or_generate_fingerprint_key(journal_path=journal_path)
        except Exception:
            lease.release()
            return None
        fingerprint = build_active_brain_fingerprint(config, hmac_key=key)
        if fingerprint["active_lane"] is None or not fingerprint["ok"]:
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
                reason_code="brain_check_in_progress",
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


def read_active_brain_fingerprint_sha256(
    *, journal_path: str | Path | None = None
) -> str | None:
    config = read_journal_config(journal_path)
    key = _load_existing_fingerprint_key(journal_path=journal_path)
    if key is None:
        return None
    fingerprint = build_active_brain_fingerprint(config, hmac_key=key)
    if not fingerprint["ok"]:
        return None
    return fingerprint["fingerprint_sha256"]


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
        assert fingerprint["active_lane"] is not None
        aggregate, reason = _reduce_evidence(
            evidence,
            now,
            active_lane=fingerprint["active_lane"],
            checking=checking,
            runtime_failure_reason=(
                runtime_failure_marker["reason_code"]
                if runtime_failure_marker is not None
                and runtime_failure_marker["revision"] == revision
                else None
            ),
        )
    else:
        reason = fingerprint["reason_code"] or "configuration_invalid"
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


def _runtime_failure_result(
    *,
    accepted: bool,
    record: BrainStateRecord | None = None,
    rejected_reason: BrainRuntimeFailureRejectedReason | None = None,
    error: str | None = None,
) -> BrainRuntimeFailureResult:
    return {
        "accepted": accepted,
        "record": record,
        "rejected_reason": rejected_reason,
        "error": error,
    }


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
    if current["revision"] != permit.checking_revision:
        raise BrainStateConflictError("brain refresh record revision changed")
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
    target_component = next(
        (
            component_name
            for component_name in COMPONENT_ORDER
            if reason_code in BRAIN_EVIDENCE_REASON_CODES[component_name]
        ),
        None,
    )
    if target_component is None:
        raise BrainStateConflictError("brain abandon reason is not recordable evidence")
    try:
        with hold_lock(path, mode=BRAIN_FILE_MODE):
            current = _read_record_unlocked(path)
            _assert_finish_allowed(permit, current, now)
            assert current is not None
            revision = _next_revision(current)
            aggregate = BRAIN_REASON_TO_AGGREGATE[reason_code]
            evidence = dict(current["evidence"])
            evidence[target_component] = _component(
                _component_status_for_reason(reason_code),
                now,
                reason_code=reason_code,
                diagnostic=diagnostic,
            )
            record = _record(
                revision=revision,
                aggregate_state=aggregate,
                reason_code=reason_code,
                active_lane=current["active_lane"],
                active_provider=current["active_provider"],
                active_model=current["active_model"],
                fingerprint_sha256=current["fingerprint_sha256"],
                checking=None,
                evidence=cast(BrainEvidenceRecord, evidence),
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
    expected_fingerprint_sha256: str,
    component: BrainRuntimeFailureComponent,
    diagnostic: Mapping[str, BrainDiagnosticValue] | None = None,
    journal_path: str | Path | None = None,
) -> BrainRuntimeFailureResult:
    try:
        now = _utc(now)
    except ValueError as exc:
        return _runtime_failure_result(
            accepted=False, rejected_reason="state_unavailable", error=str(exc)
        )
    path = brain_state_path(journal_path=journal_path)
    if (
        reason_code not in BRAIN_REASON_CODES
        or reason_code in BRAIN_PROJECTION_ONLY_REASON_CODES
        or BRAIN_REASON_TO_AGGREGATE[reason_code] not in RUNTIME_FAILURE_AGGREGATES
    ):
        return _runtime_failure_result(
            accepted=False, rejected_reason="reason_not_recordable"
        )
    if component not in {"lane_prerequisites", "generate", "cogitate"}:
        return _runtime_failure_result(
            accepted=False, rejected_reason="component_reason_not_allowed"
        )
    if reason_code not in BRAIN_EVIDENCE_REASON_CODES[component]:
        return _runtime_failure_result(
            accepted=False, rejected_reason="component_reason_not_allowed"
        )
    try:
        diagnostic = _validate_diagnostic(diagnostic or {}, "diagnostic", reason_code)
    except BrainStateValidationError as exc:
        return _runtime_failure_result(
            accepted=False, rejected_reason="reason_not_recordable", error=str(exc)
        )
    try:
        _validate_hex(expected_fingerprint_sha256, "expected_fingerprint_sha256")
    except BrainStateValidationError as exc:
        return _runtime_failure_result(
            accepted=False, rejected_reason="fingerprint_mismatch", error=str(exc)
        )
    try:
        with hold_lock(path, mode=BRAIN_FILE_MODE):
            try:
                current = _read_record_unlocked(path)
                current_readable = True
            except OSError as exc:
                return _runtime_failure_result(
                    accepted=False,
                    rejected_reason="state_unavailable",
                    error=str(exc),
                )
            except (
                BrainStateValidationError,
                MalformedDataError,
                json.JSONDecodeError,
            ):
                current = None
                current_readable = False
            try:
                _config, key, fingerprint = _load_fingerprint_for_write(
                    journal_path=journal_path
                )
            except (CorruptConfigError, OSError, BrainStateValidationError) as exc:
                return _runtime_failure_result(
                    accepted=False,
                    rejected_reason="fingerprint_not_available",
                    error=str(exc),
                )
            if key is None or fingerprint is None or not fingerprint["ok"]:
                return _runtime_failure_result(
                    accepted=False,
                    rejected_reason="fingerprint_not_available",
                )
            if fingerprint["fingerprint_sha256"] != expected_fingerprint_sha256:
                return _runtime_failure_result(
                    accepted=False,
                    rejected_reason="fingerprint_mismatch",
                )
            assert fingerprint["active_lane"] is not None
            revision = _next_revision(current) if current_readable else 1
            marker = _runtime_failure_marker(revision, reason_code, now)
            if (
                current is not None
                and current["fingerprint_sha256"] == fingerprint["fingerprint_sha256"]
            ):
                evidence = dict(current["evidence"])
            else:
                evidence = _empty_evidence()
            evidence[component] = _component(
                _component_status_for_reason(reason_code),
                now,
                reason_code=reason_code,
                diagnostic=diagnostic,
            )
            record = _record_from_evidence(
                evidence=cast(BrainEvidenceRecord, evidence),
                fingerprint=fingerprint,
                revision=revision,
                now=now,
                checking=None,
                runtime_failure_marker=marker,
                diagnostic=diagnostic,
            )
            return _runtime_failure_result(
                accepted=True, record=_write_record(path, record)
            )
    except Exception as exc:
        return _runtime_failure_result(
            accepted=False, rejected_reason="state_unavailable", error=str(exc)
        )


def runtime_phase_reason(phase: RuntimePhase) -> BrainReasonCode | None:
    return RUNTIME_PHASE_TO_REASON[phase]


def derive_active_brain_lane(config: Mapping[str, Any]) -> BrainLaneId | None:
    """Derive the canonical active-brain lane from an already-read config."""

    lane, _, _ = _derive_lane(config)
    return lane


__all__ = [
    "BRAIN_AGGREGATE_STATES",
    "BRAIN_COMPONENT_STATUSES",
    "BRAIN_EVIDENCE_REASON_CODES",
    "BRAIN_LANES",
    "BRAIN_PROJECTION_ONLY_REASON_CODES",
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
    "BrainRuntimeFailureComponent",
    "BrainRuntimeFailureMarker",
    "BrainRuntimeFailureResult",
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
    "derive_active_brain_lane",
    "finish_brain_refresh",
    "inspect_brain_state",
    "project_brain_state",
    "read_active_brain_fingerprint_sha256",
    "record_brain_runtime_failure",
    "runtime_phase_reason",
    "validate_brain_state_record",
]
