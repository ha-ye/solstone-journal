# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Closed v1 machine vocabulary for sandbox production probes.

The reason strings deliberately are not imported from ``envelope.py``. That
module owns CLI-envelope capability residuals; this module owns the separate
append-only production-probe contract, and extending the envelope vocabulary is
out of scope for this lode.
"""

from __future__ import annotations

from pathlib import Path

CONTRACT_VERSION = 1

CAPABILITY_SCOUT = "scout"
CAPABILITY_SPL = "spl"
CAPABILITY_SPB = "spb"
CAPABILITY_SPP = "spp"
CAPABILITY_RUNTIME = "runtime"

CAPABILITY_ORDER: tuple[str, ...] = (
    CAPABILITY_SCOUT,
    CAPABILITY_SPL,
    CAPABILITY_SPB,
    CAPABILITY_SPP,
    CAPABILITY_RUNTIME,
)

HEALTH_DIR_NAME = "health"
SANDBOX_PROFILE_DIR_NAME = "sandbox-profile"
LEDGER_FILE_NAME = "probes-v1.jsonl"
LOCK_FILE_NAME = "probes-v1.lock"
ATTEMPTS_DIR_NAME = "probe-attempts-v1"
ATTEMPT_DIR_MODE = 0o700

MAX_ATTEMPTS = 64
MAX_LEDGER_BYTES = 1_048_576

RECORD_TYPE_ATTEMPT_STARTED = "attempt_started"
RECORD_TYPE_PROOF_TERMINAL = "proof_terminal"
RECORD_TYPE_ATTEMPT_TERMINAL = "attempt_terminal"
RECORD_TYPES: tuple[str, ...] = (
    RECORD_TYPE_ATTEMPT_STARTED,
    RECORD_TYPE_PROOF_TERMINAL,
    RECORD_TYPE_ATTEMPT_TERMINAL,
)

PROOF_STATE_PASSED = "passed"
PROOF_STATE_FAILED = "failed"
PROOF_STATE_NOT_RUN = "not_run"
PROOF_TERMINAL_STATES: tuple[str, ...] = (
    PROOF_STATE_PASSED,
    PROOF_STATE_FAILED,
    PROOF_STATE_NOT_RUN,
)

CLEANUP_STATE_VERIFIED = "verified"
CLEANUP_STATE_RETAINED_SYNTHETIC = "retained_synthetic"
CLEANUP_STATE_UNVERIFIED = "unverified"
CLEANUP_STATES: tuple[str, ...] = (
    CLEANUP_STATE_VERIFIED,
    CLEANUP_STATE_RETAINED_SYNTHETIC,
    CLEANUP_STATE_UNVERIFIED,
)

ATTEMPT_STATE_OK = "ok"
ATTEMPT_STATE_DEGRADED = "degraded"
ATTEMPT_STATE_CANCELLED = "cancelled"
ATTEMPT_STATE_ERROR = "error"
ATTEMPT_TERMINAL_STATES: tuple[str, ...] = (
    ATTEMPT_STATE_OK,
    ATTEMPT_STATE_DEGRADED,
    ATTEMPT_STATE_CANCELLED,
    ATTEMPT_STATE_ERROR,
)

REASON_CAPABILITY_NOT_READY = "capability_not_ready"
REASON_DEADLINE_EXCEEDED = "deadline_exceeded"
REASON_REMOTE_REJECTED = "remote_rejected"
REASON_RESPONSE_INVALID = "response_invalid"
REASON_CONTENT_MISMATCH = "content_mismatch"
REASON_USAGE_INVALID = "usage_invalid"
REASON_ATTESTATION_UNVERIFIED = "attestation_unverified"
REASON_RUNTIME_UNAVAILABLE = "runtime_unavailable"
REASON_CADENCE_CONTRACT_MISMATCH = "cadence_contract_mismatch"
PROOF_REASON_POOL: tuple[str, ...] = (
    REASON_CAPABILITY_NOT_READY,
    REASON_DEADLINE_EXCEEDED,
    REASON_REMOTE_REJECTED,
    REASON_RESPONSE_INVALID,
    REASON_CONTENT_MISMATCH,
    REASON_USAGE_INVALID,
    REASON_ATTESTATION_UNVERIFIED,
    REASON_RUNTIME_UNAVAILABLE,
    REASON_CADENCE_CONTRACT_MISMATCH,
)

REASON_DEPENDENCY_FAILED = "dependency_failed"
REASON_CANCELLED = "cancelled"
REASON_CLEANUP_UNVERIFIED = "cleanup_unverified"
REASON_INTERNAL_ERROR = "internal_error"
COMMON_REASONS: tuple[str, ...] = (
    REASON_DEPENDENCY_FAILED,
    REASON_CANCELLED,
    REASON_CLEANUP_UNVERIFIED,
    REASON_INTERNAL_ERROR,
)
FAILED_COMMON_REASONS: tuple[str, ...] = (
    REASON_CANCELLED,
    REASON_CLEANUP_UNVERIFIED,
    REASON_INTERNAL_ERROR,
)
NOT_RUN_REASONS: tuple[str, ...] = (
    REASON_DEPENDENCY_FAILED,
    REASON_CANCELLED,
)

ATTEMPT_TERMINAL_REASON_PROOF_FAILED = "proof_failed"
ATTEMPT_TERMINAL_REASONS: tuple[str, ...] = (
    REASON_CLEANUP_UNVERIFIED,
    REASON_CANCELLED,
    REASON_INTERNAL_ERROR,
    ATTEMPT_TERMINAL_REASON_PROOF_FAILED,
)

STABLE_ERROR_PROBE_ACTIVE = "probe_active"
STABLE_ERROR_ATTEMPT_LIMIT_REACHED = "attempt_limit_reached"
STABLE_ERROR_STALE_ATTEMPT = "stale_attempt"
STABLE_ERROR_RECORD_WRITE_FAILED = "record_write_failed"
STABLE_ERROR_INTERNAL_ERROR = "internal_error"
STABLE_ERRORS: tuple[str, ...] = (
    STABLE_ERROR_PROBE_ACTIVE,
    STABLE_ERROR_ATTEMPT_LIMIT_REACHED,
    STABLE_ERROR_STALE_ATTEMPT,
    STABLE_ERROR_RECORD_WRITE_FAILED,
    STABLE_ERROR_INTERNAL_ERROR,
)

PROOF_CHECKS: dict[str, tuple[str, ...]] = {
    CAPABILITY_SCOUT: (
        f"{CAPABILITY_SCOUT}.response_schema",
        f"{CAPABILITY_SCOUT}.nonce_match",
        f"{CAPABILITY_SCOUT}.finish",
        f"{CAPABILITY_SCOUT}.usage",
    ),
    CAPABILITY_SPL: (
        f"{CAPABILITY_SPL}.enrollment",
        f"{CAPABILITY_SPL}.relay_dial",
        f"{CAPABILITY_SPL}.inner_tls",
        f"{CAPABILITY_SPL}.observer_registered",
        f"{CAPABILITY_SPL}.segment_transferred",
        f"{CAPABILITY_SPL}.segment_landed",
        f"{CAPABILITY_SPL}.authorization_removed",
    ),
    CAPABILITY_SPB: (
        f"{CAPABILITY_SPB}.repository_initialized",
        f"{CAPABILITY_SPB}.snapshot_created",
        f"{CAPABILITY_SPB}.snapshot_confirmed",
        f"{CAPABILITY_SPB}.restore_match",
        f"{CAPABILITY_SPB}.local_cleanup",
    ),
    CAPABILITY_SPP: (
        f"{CAPABILITY_SPP}.attestation_session",
        f"{CAPABILITY_SPP}.text_nonce",
        f"{CAPABILITY_SPP}.text_usage",
        f"{CAPABILITY_SPP}.transcript_expected",
    ),
    CAPABILITY_RUNTIME: (
        f"{CAPABILITY_RUNTIME}.supervisor",
        f"{CAPABILITY_RUNTIME}.callosum",
        f"{CAPABILITY_RUNTIME}.listener",
        f"{CAPABILITY_RUNTIME}.sense",
        f"{CAPABILITY_RUNTIME}.task_queue",
        f"{CAPABILITY_RUNTIME}.cortex",
        f"{CAPABILITY_RUNTIME}.talent_output",
        f"{CAPABILITY_RUNTIME}.talent_usage",
        f"{CAPABILITY_RUNTIME}.cadence_contract",
        f"{CAPABILITY_RUNTIME}.cadence_dry_run",
    ),
}

_PROOF_REASON_EXCLUSIONS: dict[str, frozenset[str]] = {
    CAPABILITY_SCOUT: frozenset(
        {
            REASON_ATTESTATION_UNVERIFIED,
            REASON_RUNTIME_UNAVAILABLE,
            REASON_CADENCE_CONTRACT_MISMATCH,
        }
    ),
    CAPABILITY_SPL: frozenset(
        {
            REASON_USAGE_INVALID,
            REASON_ATTESTATION_UNVERIFIED,
            REASON_RUNTIME_UNAVAILABLE,
            REASON_CADENCE_CONTRACT_MISMATCH,
        }
    ),
    CAPABILITY_SPB: frozenset(
        {
            REASON_USAGE_INVALID,
            REASON_ATTESTATION_UNVERIFIED,
            REASON_RUNTIME_UNAVAILABLE,
            REASON_CADENCE_CONTRACT_MISMATCH,
        }
    ),
    CAPABILITY_SPP: frozenset(
        {
            REASON_RUNTIME_UNAVAILABLE,
            REASON_CADENCE_CONTRACT_MISMATCH,
        }
    ),
    CAPABILITY_RUNTIME: frozenset(
        {
            REASON_REMOTE_REJECTED,
            REASON_ATTESTATION_UNVERIFIED,
        }
    ),
}

PROOF_SPECIFIC_REASONS: dict[str, tuple[str, ...]] = {
    proof: tuple(
        sorted(
            reason
            for reason in PROOF_REASON_POOL
            if reason not in _PROOF_REASON_EXCLUSIONS[proof]
        )
    )
    for proof in CAPABILITY_ORDER
}

# Cleanup class is declared per proof; it is not derived from whether a proof
# has a terminal cleanup check.
DECLARED_CLEANUP_STATES: dict[str, str] = {
    # Request/response probe; no durable remote or local artifact is created.
    CAPABILITY_SCOUT: CLEANUP_STATE_VERIFIED,
    # Local authorization is removed, but a synthetic segment remains landed.
    CAPABILITY_SPL: CLEANUP_STATE_RETAINED_SYNTHETIC,
    # Local cleanup precedes terminalization; retained state is the remote snapshot.
    CAPABILITY_SPB: CLEANUP_STATE_RETAINED_SYNTHETIC,
    # Attested text round trip; no durable artifact is retained.
    CAPABILITY_SPP: CLEANUP_STATE_VERIFIED,
    # Runtime is the sandbox substrate and cannot self-clean mid-probe.
    CAPABILITY_RUNTIME: CLEANUP_STATE_RETAINED_SYNTHETIC,
}

FIELD_ATTEMPT_ID = "attempt_id"
FIELD_CHECKS = "checks"
FIELD_CLEANUP_STATE = "cleanup_state"
FIELD_CONTRACT_VERSION = "contract_version"
FIELD_DURATION_MS = "duration_ms"
FIELD_EXECUTION_ORDER = "execution_order"
FIELD_FINISHED_AT = "finished_at"
FIELD_PROOF = "proof"
FIELD_REASON = "reason"
FIELD_RUN_ID = "run_id"
FIELD_SELECTED = "selected"
FIELD_STARTED_AT = "started_at"
FIELD_STATE = "state"
FIELD_TERMINAL_REASON = "terminal_reason"
FIELD_TYPE = "type"

RECORD_FIELDS: dict[str, tuple[str, ...]] = {
    RECORD_TYPE_ATTEMPT_STARTED: (
        FIELD_ATTEMPT_ID,
        FIELD_CONTRACT_VERSION,
        FIELD_EXECUTION_ORDER,
        FIELD_RUN_ID,
        FIELD_SELECTED,
        FIELD_STARTED_AT,
        FIELD_TYPE,
    ),
    RECORD_TYPE_PROOF_TERMINAL: (
        FIELD_ATTEMPT_ID,
        FIELD_CHECKS,
        FIELD_CLEANUP_STATE,
        FIELD_CONTRACT_VERSION,
        FIELD_DURATION_MS,
        FIELD_FINISHED_AT,
        FIELD_PROOF,
        FIELD_REASON,
        FIELD_RUN_ID,
        FIELD_STATE,
        FIELD_TYPE,
    ),
    RECORD_TYPE_ATTEMPT_TERMINAL: (
        FIELD_ATTEMPT_ID,
        FIELD_CONTRACT_VERSION,
        FIELD_FINISHED_AT,
        FIELD_RUN_ID,
        FIELD_STATE,
        FIELD_TERMINAL_REASON,
        FIELD_TYPE,
    ),
}

PREDICATE_CHECKS_COMPLETE = "checks.complete"
PREDICATE_CHECKS_ORDERED_PREFIX = "checks.ordered_prefix"
PREDICATE_CHECKS_EMPTY = "checks.empty"
PREDICATE_REASON_NULL = "reason.null"
PREDICATE_REASON_FAILED_SPECIFIC_OR_COMMON = "reason.failed_specific_or_common"
PREDICATE_REASON_NOT_RUN = "reason.not_run"
PREDICATE_DURATION_NON_NEGATIVE_INT = "duration.non_negative_int"
PREDICATE_DURATION_NULL = "duration.null"
PREDICATE_CLEANUP_EXPECTED = "cleanup.expected_for_proof_state_reason"
PREDICATE_TERMINAL_ANY_CLEANUP_UNVERIFIED = "terminal.any_cleanup_unverified"
PREDICATE_TERMINAL_ANY_REASON_CANCELLED = "terminal.any_reason_cancelled"
PREDICATE_TERMINAL_ANY_REASON_INTERNAL_ERROR = "terminal.any_reason_internal_error"
PREDICATE_TERMINAL_ANY_FAILED_PROOF = "terminal.any_failed_proof"
PREDICATE_TERMINAL_ALL_PASSED = "terminal.all_passed"
PREDICATE_RETRY_ALL_CLEANUP_CLOSED = "retry.all_cleanup_closed"
PREDICATE_CANCELLATION_FIRST_FAILED_CANCELLED_AFTER_CONTACT = (
    "cancellation.first_failed_cancelled_after_contact"
)
PREDICATE_CANCELLATION_FIRST_NOT_RUN_CANCELLED_WITHOUT_CONTACT = (
    "cancellation.first_not_run_cancelled_without_contact"
)
PREDICATE_CANCELLATION_CONTIGUOUS_NOT_RUN_CANCELLED_SUFFIX = (
    "cancellation.contiguous_not_run_cancelled_suffix"
)
PREDICATE_KEYS: tuple[str, ...] = (
    PREDICATE_CHECKS_COMPLETE,
    PREDICATE_CHECKS_ORDERED_PREFIX,
    PREDICATE_CHECKS_EMPTY,
    PREDICATE_REASON_NULL,
    PREDICATE_REASON_FAILED_SPECIFIC_OR_COMMON,
    PREDICATE_REASON_NOT_RUN,
    PREDICATE_DURATION_NON_NEGATIVE_INT,
    PREDICATE_DURATION_NULL,
    PREDICATE_CLEANUP_EXPECTED,
    PREDICATE_TERMINAL_ANY_CLEANUP_UNVERIFIED,
    PREDICATE_TERMINAL_ANY_REASON_CANCELLED,
    PREDICATE_TERMINAL_ANY_REASON_INTERNAL_ERROR,
    PREDICATE_TERMINAL_ANY_FAILED_PROOF,
    PREDICATE_TERMINAL_ALL_PASSED,
    PREDICATE_RETRY_ALL_CLEANUP_CLOSED,
    PREDICATE_CANCELLATION_FIRST_FAILED_CANCELLED_AFTER_CONTACT,
    PREDICATE_CANCELLATION_FIRST_NOT_RUN_CANCELLED_WITHOUT_CONTACT,
    PREDICATE_CANCELLATION_CONTIGUOUS_NOT_RUN_CANCELLED_SUFFIX,
)

PROOF_TERMINAL_RULES: dict[str, dict[str, object]] = {
    PROOF_STATE_PASSED: {
        FIELD_CHECKS: PREDICATE_CHECKS_COMPLETE,
        FIELD_REASON: PREDICATE_REASON_NULL,
        FIELD_DURATION_MS: PREDICATE_DURATION_NON_NEGATIVE_INT,
        FIELD_CLEANUP_STATE: PREDICATE_CLEANUP_EXPECTED,
    },
    PROOF_STATE_FAILED: {
        FIELD_CHECKS: PREDICATE_CHECKS_ORDERED_PREFIX,
        FIELD_REASON: PREDICATE_REASON_FAILED_SPECIFIC_OR_COMMON,
        FIELD_DURATION_MS: PREDICATE_DURATION_NON_NEGATIVE_INT,
        FIELD_CLEANUP_STATE: PREDICATE_CLEANUP_EXPECTED,
        "failed_common_reasons": FAILED_COMMON_REASONS,
    },
    PROOF_STATE_NOT_RUN: {
        FIELD_CHECKS: PREDICATE_CHECKS_EMPTY,
        FIELD_REASON: PREDICATE_REASON_NOT_RUN,
        FIELD_DURATION_MS: PREDICATE_DURATION_NULL,
        FIELD_CLEANUP_STATE: PREDICATE_CLEANUP_EXPECTED,
    },
}

CLEANUP_RESOLUTION: dict[str, object] = {
    "declared_defaults": DECLARED_CLEANUP_STATES,
    "state_overrides": {
        PROOF_STATE_NOT_RUN: CLEANUP_STATE_VERIFIED,
    },
    "reason_overrides": {
        REASON_CLEANUP_UNVERIFIED: CLEANUP_STATE_UNVERIFIED,
    },
}

RECORD_CARDINALITY: dict[str, object] = {
    "attempt_sequence": (
        {
            "type": RECORD_TYPE_ATTEMPT_STARTED,
            "count": 1,
            "position": "first",
        },
        {
            "type": RECORD_TYPE_PROOF_TERMINAL,
            "count": "len(execution_order)",
            "order": "execution_order",
        },
        {
            "type": RECORD_TYPE_ATTEMPT_TERMINAL,
            "count": 1,
            "position": "last",
        },
    ),
    "attempt_count_type": RECORD_TYPE_ATTEMPT_STARTED,
}

CANCELLATION: dict[str, object] = {
    "first_started_predicate": PREDICATE_CANCELLATION_FIRST_FAILED_CANCELLED_AFTER_CONTACT,
    "first_unstarted_predicate": (
        PREDICATE_CANCELLATION_FIRST_NOT_RUN_CANCELLED_WITHOUT_CONTACT
    ),
    "suffix_predicate": PREDICATE_CANCELLATION_CONTIGUOUS_NOT_RUN_CANCELLED_SUFFIX,
    "later_proof": {
        FIELD_STATE: PROOF_STATE_NOT_RUN,
        FIELD_CHECKS: (),
        FIELD_REASON: REASON_CANCELLED,
        FIELD_DURATION_MS: None,
        FIELD_CLEANUP_STATE: CLEANUP_STATE_VERIFIED,
    },
}

TERMINAL_DERIVATION: tuple[dict[str, str | None], ...] = (
    {
        "predicate": PREDICATE_TERMINAL_ANY_CLEANUP_UNVERIFIED,
        "state": ATTEMPT_STATE_DEGRADED,
        FIELD_TERMINAL_REASON: REASON_CLEANUP_UNVERIFIED,
    },
    {
        "predicate": PREDICATE_TERMINAL_ANY_REASON_CANCELLED,
        "state": ATTEMPT_STATE_CANCELLED,
        FIELD_TERMINAL_REASON: REASON_CANCELLED,
    },
    {
        "predicate": PREDICATE_TERMINAL_ANY_REASON_INTERNAL_ERROR,
        "state": ATTEMPT_STATE_ERROR,
        FIELD_TERMINAL_REASON: REASON_INTERNAL_ERROR,
    },
    {
        "predicate": PREDICATE_TERMINAL_ANY_FAILED_PROOF,
        "state": ATTEMPT_STATE_DEGRADED,
        FIELD_TERMINAL_REASON: ATTEMPT_TERMINAL_REASON_PROOF_FAILED,
    },
    {
        "predicate": PREDICATE_TERMINAL_ALL_PASSED,
        "state": ATTEMPT_STATE_OK,
        FIELD_TERMINAL_REASON: None,
    },
)

RETRY_ELIGIBLE_TERMINALS: tuple[dict[str, str | None], ...] = (
    {
        "state": ATTEMPT_STATE_OK,
        FIELD_TERMINAL_REASON: None,
        "proofs": None,
    },
    {
        "state": ATTEMPT_STATE_DEGRADED,
        FIELD_TERMINAL_REASON: ATTEMPT_TERMINAL_REASON_PROOF_FAILED,
        "proofs": PREDICATE_RETRY_ALL_CLEANUP_CLOSED,
    },
)


def _json_dict_tuple(value: dict[str, tuple[str, ...]]) -> dict[str, list[str]]:
    return {key: list(items) for key, items in value.items()}


def _json_record_sequence(
    records: tuple[dict[str, object], ...],
) -> list[dict[str, object]]:
    return [dict(record) for record in records]


def _json_terminal_rules() -> dict[str, dict[str, object]]:
    return {
        state: {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in rule.items()
        }
        for state, rule in PROOF_TERMINAL_RULES.items()
    }


def _json_cleanup_resolution() -> dict[str, object]:
    return {
        "declared_defaults": dict(DECLARED_CLEANUP_STATES),
        "reason_overrides": dict(CLEANUP_RESOLUTION["reason_overrides"]),
        "state_overrides": dict(CLEANUP_RESOLUTION["state_overrides"]),
    }


def _json_cancellation() -> dict[str, object]:
    later = CANCELLATION["later_proof"]
    assert isinstance(later, dict)
    return {
        "first_started_predicate": CANCELLATION["first_started_predicate"],
        "first_unstarted_predicate": CANCELLATION["first_unstarted_predicate"],
        "later_proof": {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in later.items()
        },
        "suffix_predicate": CANCELLATION["suffix_predicate"],
    }


def sandbox_profile_health_path(journal_path: str | Path) -> Path:
    return Path(journal_path) / HEALTH_DIR_NAME / SANDBOX_PROFILE_DIR_NAME


def probe_ledger_path(journal_path: str | Path) -> Path:
    return sandbox_profile_health_path(journal_path) / LEDGER_FILE_NAME


def probe_lock_path(journal_path: str | Path) -> Path:
    return sandbox_profile_health_path(journal_path) / LOCK_FILE_NAME


def probe_attempts_parent_path(journal_path: str | Path) -> Path:
    return sandbox_profile_health_path(journal_path) / ATTEMPTS_DIR_NAME


def contract_payload() -> dict[str, object]:
    return {
        "attempt_terminal_reasons": list(ATTEMPT_TERMINAL_REASONS),
        "attempt_terminal_states": list(ATTEMPT_TERMINAL_STATES),
        "cancellation": _json_cancellation(),
        "capability_order": list(CAPABILITY_ORDER),
        "cleanup_resolution": _json_cleanup_resolution(),
        "cleanup_states": list(CLEANUP_STATES),
        "common_reasons": list(COMMON_REASONS),
        "contract_version": CONTRACT_VERSION,
        "limits": {
            "max_attempts": MAX_ATTEMPTS,
            "max_ledger_bytes": MAX_LEDGER_BYTES,
        },
        "not_run_reasons": list(NOT_RUN_REASONS),
        "predicate_keys": list(PREDICATE_KEYS),
        "proof_reason_pool": list(PROOF_REASON_POOL),
        "proof_terminal_rules": _json_terminal_rules(),
        "proof_terminal_states": list(PROOF_TERMINAL_STATES),
        "proofs": {
            proof: {
                "checks": list(PROOF_CHECKS[proof]),
                "cleanup_state": DECLARED_CLEANUP_STATES[proof],
                "proof_specific_reasons": list(PROOF_SPECIFIC_REASONS[proof]),
            }
            for proof in CAPABILITY_ORDER
        },
        "record_cardinality": {
            "attempt_count_type": RECORD_CARDINALITY["attempt_count_type"],
            "attempt_sequence": _json_record_sequence(_record_cardinality_sequence()),
        },
        "record_fields": _json_dict_tuple(RECORD_FIELDS),
        "record_types": list(RECORD_TYPES),
        "retry_eligible_terminals": [
            dict(terminal) for terminal in RETRY_ELIGIBLE_TERMINALS
        ],
        "stable_errors": list(STABLE_ERRORS),
        "terminal_derivation": [dict(rule) for rule in TERMINAL_DERIVATION],
    }


def _record_cardinality_sequence() -> tuple[dict[str, object], ...]:
    sequence = RECORD_CARDINALITY["attempt_sequence"]
    assert isinstance(sequence, tuple)
    return sequence
