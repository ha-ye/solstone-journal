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

RECORD_KIND_ATTEMPT_STARTED = "attempt_started"
RECORD_KIND_PROOF_TERMINAL = "proof_terminal"
RECORD_KIND_ATTEMPT_TERMINAL = "attempt_terminal"
RECORD_KINDS: tuple[str, ...] = (
    RECORD_KIND_ATTEMPT_STARTED,
    RECORD_KIND_PROOF_TERMINAL,
    RECORD_KIND_ATTEMPT_TERMINAL,
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
        "capability_order": list(CAPABILITY_ORDER),
        "cleanup_states": list(CLEANUP_STATES),
        "common_reasons": list(COMMON_REASONS),
        "contract_version": CONTRACT_VERSION,
        "limits": {
            "max_attempts": MAX_ATTEMPTS,
            "max_ledger_bytes": MAX_LEDGER_BYTES,
        },
        "not_run_reasons": list(NOT_RUN_REASONS),
        "proof_reason_pool": list(PROOF_REASON_POOL),
        "proof_terminal_states": list(PROOF_TERMINAL_STATES),
        "proofs": {
            proof: {
                "checks": list(PROOF_CHECKS[proof]),
                "cleanup_state": DECLARED_CLEANUP_STATES[proof],
                "proof_specific_reasons": list(PROOF_SPECIFIC_REASONS[proof]),
            }
            for proof in CAPABILITY_ORDER
        },
        "record_kinds": list(RECORD_KINDS),
        "stable_errors": list(STABLE_ERRORS),
    }
