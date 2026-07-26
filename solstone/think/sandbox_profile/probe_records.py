# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Record grammar and truth-table validation for sandbox probe ledgers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID, uuid4

from solstone.think.sandbox_profile import probe_contract as contract

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class ProbeRecordValidationError(ValueError):
    """Internal validation failure converted to stable public errors by callers."""


class ProbeOperationError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        attempt_id: str | None = None,
        record_kind: str | None = None,
        proof: str | None = None,
    ) -> None:
        if code not in contract.STABLE_ERRORS:
            raise ValueError("invalid stable error code")
        if attempt_id is not None:
            validate_canonical_uuid(attempt_id)
        if record_kind is not None and record_kind not in contract.RECORD_KINDS:
            raise ValueError("invalid record kind")
        if proof is not None and proof not in contract.CAPABILITY_ORDER:
            raise ValueError("invalid proof")
        super().__init__(code)
        self.code = code
        self.attempt_id = attempt_id
        self.record_kind = record_kind
        self.proof = proof


def raise_probe_error(
    code: str,
    *,
    attempt_id: str | None = None,
    record_kind: str | None = None,
    proof: str | None = None,
) -> NoReturn:
    raise ProbeOperationError(
        code,
        attempt_id=attempt_id,
        record_kind=record_kind,
        proof=proof,
    ) from None


@dataclass(frozen=True, slots=True)
class AttemptStartedRecord:
    contract_version: int
    run_id: str
    attempt_id: str
    started_at: str
    selected: tuple[str, ...]
    execution_order: tuple[str, ...]

    def to_json_obj(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "contract_version": self.contract_version,
            "execution_order": list(self.execution_order),
            "record_kind": contract.RECORD_KIND_ATTEMPT_STARTED,
            "run_id": self.run_id,
            "selected": list(self.selected),
            "started_at": self.started_at,
        }


@dataclass(frozen=True, slots=True)
class ProofTerminalRecord:
    contract_version: int
    run_id: str
    attempt_id: str
    proof: str
    state: str
    checks: tuple[str, ...]
    reason: str | None
    cleanup_state: str
    duration_ms: int | None
    finished_at: str

    def to_json_obj(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "checks": list(self.checks),
            "cleanup_state": self.cleanup_state,
            "contract_version": self.contract_version,
            "duration_ms": self.duration_ms,
            "finished_at": self.finished_at,
            "proof": self.proof,
            "reason": self.reason,
            "record_kind": contract.RECORD_KIND_PROOF_TERMINAL,
            "run_id": self.run_id,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class AttemptTerminalRecord:
    contract_version: int
    run_id: str
    attempt_id: str
    state: str
    reason: str | None
    duration_ms: int
    finished_at: str

    def to_json_obj(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "contract_version": self.contract_version,
            "duration_ms": self.duration_ms,
            "finished_at": self.finished_at,
            "reason": self.reason,
            "record_kind": contract.RECORD_KIND_ATTEMPT_TERMINAL,
            "run_id": self.run_id,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class ProbeAttemptReplay:
    start: AttemptStartedRecord
    proofs: tuple[ProofTerminalRecord, ...]
    terminal: AttemptTerminalRecord


@dataclass(frozen=True, slots=True)
class ProbeReplay:
    journal_path: Path
    ledger_path: Path
    run_id: str | None
    attempts: tuple[ProbeAttemptReplay, ...]
    retry_permitted: bool

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)


def utc_timestamp_ms() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def new_attempt_id() -> str:
    return str(uuid4())


def validate_canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ProbeRecordValidationError("uuid must be a string")
    try:
        parsed_uuid = UUID(value)
    except ValueError as exc:
        raise ProbeRecordValidationError("uuid is invalid") from exc
    if str(parsed_uuid) != value:
        raise ProbeRecordValidationError("uuid must be canonical")
    return value


def validate_timestamp(value: object) -> str:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise ProbeRecordValidationError("timestamp must be RFC3339 UTC milliseconds")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ProbeRecordValidationError("timestamp is invalid") from exc
    return value


def validate_selected(value: object) -> tuple[str, ...]:
    items = _string_tuple(value, "selected")
    if not items:
        raise ProbeRecordValidationError("selected must not be empty")
    if len(set(items)) != len(items):
        raise ProbeRecordValidationError("selected must not contain duplicates")
    expected = tuple(
        capability
        for capability in contract.CAPABILITY_ORDER
        if capability in set(items)
    )
    if items != expected:
        raise ProbeRecordValidationError("selected must be a canonical subsequence")
    return items


def validate_execution_order(value: object, selected: Sequence[str]) -> tuple[str, ...]:
    items = _string_tuple(value, "execution_order")
    if len(set(items)) != len(items):
        raise ProbeRecordValidationError("execution_order must not contain duplicates")
    if set(items) != set(selected):
        raise ProbeRecordValidationError("execution_order must be selected permutation")
    return items


def build_attempt_started_record(
    *,
    run_id: str,
    attempt_id: str,
    selected: Sequence[str],
    execution_order: Sequence[str],
    started_at: str | None = None,
) -> AttemptStartedRecord:
    record = AttemptStartedRecord(
        contract_version=contract.CONTRACT_VERSION,
        run_id=validate_canonical_uuid(run_id),
        attempt_id=validate_canonical_uuid(attempt_id),
        started_at=validate_timestamp(started_at or utc_timestamp_ms()),
        selected=validate_selected(list(selected)),
        execution_order=(),
    )
    return AttemptStartedRecord(
        contract_version=record.contract_version,
        run_id=record.run_id,
        attempt_id=record.attempt_id,
        started_at=record.started_at,
        selected=record.selected,
        execution_order=validate_execution_order(
            list(execution_order), record.selected
        ),
    )


def build_proof_terminal_record(
    *,
    run_id: str,
    attempt_id: str,
    proof: str,
    state: str,
    checks: Sequence[str],
    reason: str | None,
    duration_ms: int | None,
    finished_at: str | None = None,
) -> ProofTerminalRecord:
    proof = validate_proof_name(proof)
    cleanup_state = cleanup_state_for(proof=proof, state=state, reason=reason)
    record = ProofTerminalRecord(
        contract_version=contract.CONTRACT_VERSION,
        run_id=validate_canonical_uuid(run_id),
        attempt_id=validate_canonical_uuid(attempt_id),
        proof=proof,
        state=state,
        checks=tuple(checks),
        reason=reason,
        cleanup_state=cleanup_state,
        duration_ms=duration_ms,
        finished_at=validate_timestamp(finished_at or utc_timestamp_ms()),
    )
    validate_proof_terminal_semantics(record)
    return record


def build_attempt_terminal_record(
    *,
    run_id: str,
    attempt_id: str,
    proofs: Sequence[ProofTerminalRecord],
    duration_ms: int,
    finished_at: str | None = None,
) -> AttemptTerminalRecord:
    state, reason = derive_attempt_terminal(proofs)
    return AttemptTerminalRecord(
        contract_version=contract.CONTRACT_VERSION,
        run_id=validate_canonical_uuid(run_id),
        attempt_id=validate_canonical_uuid(attempt_id),
        state=state,
        reason=reason,
        duration_ms=validate_non_negative_int(duration_ms),
        finished_at=validate_timestamp(finished_at or utc_timestamp_ms()),
    )


def validate_attempt_started_payload(
    payload: Mapping[str, Any],
) -> AttemptStartedRecord:
    _reject_unknown_fields(
        payload,
        {
            "attempt_id",
            "contract_version",
            "execution_order",
            "record_kind",
            "run_id",
            "selected",
            "started_at",
        },
    )
    _validate_contract_version(payload.get("contract_version"))
    if payload.get("record_kind") != contract.RECORD_KIND_ATTEMPT_STARTED:
        raise ProbeRecordValidationError("wrong record kind")
    selected = validate_selected(payload.get("selected"))
    return AttemptStartedRecord(
        contract_version=contract.CONTRACT_VERSION,
        run_id=validate_canonical_uuid(payload.get("run_id")),
        attempt_id=validate_canonical_uuid(payload.get("attempt_id")),
        started_at=validate_timestamp(payload.get("started_at")),
        selected=selected,
        execution_order=validate_execution_order(
            payload.get("execution_order"), selected
        ),
    )


def validate_proof_terminal_payload(
    payload: Mapping[str, Any],
) -> ProofTerminalRecord:
    _reject_unknown_fields(
        payload,
        {
            "attempt_id",
            "checks",
            "cleanup_state",
            "contract_version",
            "duration_ms",
            "finished_at",
            "proof",
            "reason",
            "record_kind",
            "run_id",
            "state",
        },
    )
    _validate_contract_version(payload.get("contract_version"))
    if payload.get("record_kind") != contract.RECORD_KIND_PROOF_TERMINAL:
        raise ProbeRecordValidationError("wrong record kind")
    record = ProofTerminalRecord(
        contract_version=contract.CONTRACT_VERSION,
        run_id=validate_canonical_uuid(payload.get("run_id")),
        attempt_id=validate_canonical_uuid(payload.get("attempt_id")),
        proof=validate_proof_name(payload.get("proof")),
        state=_validate_str(payload.get("state"), "state"),
        checks=_string_tuple(payload.get("checks"), "checks"),
        reason=_validate_reason_or_none(payload.get("reason")),
        cleanup_state=_validate_str(payload.get("cleanup_state"), "cleanup_state"),
        duration_ms=payload.get("duration_ms"),
        finished_at=validate_timestamp(payload.get("finished_at")),
    )
    validate_proof_terminal_semantics(record)
    return record


def validate_attempt_terminal_payload(
    payload: Mapping[str, Any],
) -> AttemptTerminalRecord:
    _reject_unknown_fields(
        payload,
        {
            "attempt_id",
            "contract_version",
            "duration_ms",
            "finished_at",
            "reason",
            "record_kind",
            "run_id",
            "state",
        },
    )
    _validate_contract_version(payload.get("contract_version"))
    if payload.get("record_kind") != contract.RECORD_KIND_ATTEMPT_TERMINAL:
        raise ProbeRecordValidationError("wrong record kind")
    reason = payload.get("reason")
    if reason is not None and reason not in contract.ATTEMPT_TERMINAL_REASONS:
        raise ProbeRecordValidationError("invalid attempt terminal reason")
    state = _validate_str(payload.get("state"), "state")
    if state not in contract.ATTEMPT_TERMINAL_STATES:
        raise ProbeRecordValidationError("invalid attempt terminal state")
    return AttemptTerminalRecord(
        contract_version=contract.CONTRACT_VERSION,
        run_id=validate_canonical_uuid(payload.get("run_id")),
        attempt_id=validate_canonical_uuid(payload.get("attempt_id")),
        state=state,
        reason=reason,
        duration_ms=validate_non_negative_int(payload.get("duration_ms")),
        finished_at=validate_timestamp(payload.get("finished_at")),
    )


def validate_proof_terminal_semantics(record: ProofTerminalRecord) -> None:
    if record.state not in contract.PROOF_TERMINAL_STATES:
        raise ProbeRecordValidationError("invalid proof terminal state")
    expected_cleanup = cleanup_state_for(
        proof=record.proof, state=record.state, reason=record.reason
    )
    if record.cleanup_state != expected_cleanup:
        raise ProbeRecordValidationError("invalid cleanup state")
    ordered_checks = contract.PROOF_CHECKS[record.proof]
    if record.state == contract.PROOF_STATE_PASSED:
        if record.checks != ordered_checks:
            raise ProbeRecordValidationError("passed checks must be complete")
        if record.reason is not None:
            raise ProbeRecordValidationError("passed reason must be null")
        validate_non_negative_int(record.duration_ms)
        return
    if record.state == contract.PROOF_STATE_FAILED:
        if not _is_ordered_prefix(record.checks, ordered_checks):
            raise ProbeRecordValidationError("failed checks must be an ordered prefix")
        allowed = set(contract.PROOF_SPECIFIC_REASONS[record.proof]) | set(
            contract.FAILED_COMMON_REASONS
        )
        if record.reason not in allowed:
            raise ProbeRecordValidationError("invalid failed reason")
        validate_non_negative_int(record.duration_ms)
        return
    if record.state == contract.PROOF_STATE_NOT_RUN:
        if record.checks:
            raise ProbeRecordValidationError("not_run checks must be empty")
        if record.reason not in contract.NOT_RUN_REASONS:
            raise ProbeRecordValidationError("invalid not_run reason")
        if record.duration_ms is not None:
            raise ProbeRecordValidationError("not_run duration must be null")
        return
    raise ProbeRecordValidationError("invalid proof terminal state")


def cleanup_state_for(*, proof: str, state: str, reason: str | None) -> str:
    proof = validate_proof_name(proof)
    if state == contract.PROOF_STATE_NOT_RUN:
        return contract.CLEANUP_STATE_VERIFIED
    if reason == contract.REASON_CLEANUP_UNVERIFIED:
        return contract.CLEANUP_STATE_UNVERIFIED
    return contract.DECLARED_CLEANUP_STATES[proof]


def derive_attempt_terminal(
    proofs: Sequence[ProofTerminalRecord],
) -> tuple[str, str | None]:
    if not proofs:
        raise ProbeRecordValidationError("attempt needs proof rows")
    if any(
        proof.cleanup_state == contract.CLEANUP_STATE_UNVERIFIED
        or proof.reason == contract.REASON_CLEANUP_UNVERIFIED
        for proof in proofs
    ):
        return contract.ATTEMPT_STATE_DEGRADED, contract.REASON_CLEANUP_UNVERIFIED
    if any(proof.reason == contract.REASON_CANCELLED for proof in proofs):
        return contract.ATTEMPT_STATE_CANCELLED, contract.REASON_CANCELLED
    if any(proof.reason == contract.REASON_INTERNAL_ERROR for proof in proofs):
        return contract.ATTEMPT_STATE_ERROR, contract.REASON_INTERNAL_ERROR
    if any(proof.state == contract.PROOF_STATE_FAILED for proof in proofs):
        return (
            contract.ATTEMPT_STATE_DEGRADED,
            contract.ATTEMPT_TERMINAL_REASON_PROOF_FAILED,
        )
    if all(proof.state == contract.PROOF_STATE_PASSED for proof in proofs):
        return contract.ATTEMPT_STATE_OK, None
    raise ProbeRecordValidationError("invalid proof truth table")


def attempt_terminal_retry_permitted(
    terminal: AttemptTerminalRecord,
    proofs: Sequence[ProofTerminalRecord],
) -> bool:
    if terminal.state == contract.ATTEMPT_STATE_OK and terminal.reason is None:
        return True
    if (
        terminal.state == contract.ATTEMPT_STATE_DEGRADED
        and terminal.reason == contract.ATTEMPT_TERMINAL_REASON_PROOF_FAILED
    ):
        return all(
            proof.cleanup_state
            in {
                contract.CLEANUP_STATE_VERIFIED,
                contract.CLEANUP_STATE_RETAINED_SYNTHETIC,
            }
            for proof in proofs
        )
    return False


def validate_attempt_terminal_matches(
    terminal: AttemptTerminalRecord,
    proofs: Sequence[ProofTerminalRecord],
) -> None:
    expected_state, expected_reason = derive_attempt_terminal(proofs)
    if terminal.state != expected_state or terminal.reason != expected_reason:
        raise ProbeRecordValidationError("attempt terminal does not match proof rows")


def validate_proof_name(value: object) -> str:
    proof = _validate_str(value, "proof")
    if proof not in contract.CAPABILITY_ORDER:
        raise ProbeRecordValidationError("invalid proof")
    return proof


def validate_non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProbeRecordValidationError("duration must be a nonnegative integer")
    return value


def _validate_contract_version(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value != contract.CONTRACT_VERSION
    ):
        raise ProbeRecordValidationError("invalid contract version")


def _reject_unknown_fields(payload: Mapping[str, Any], fields: set[str]) -> None:
    if set(payload) != fields:
        raise ProbeRecordValidationError("record fields do not match grammar")


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProbeRecordValidationError(f"{field} must be a list")
    if not all(isinstance(item, str) for item in value):
        raise ProbeRecordValidationError(f"{field} entries must be strings")
    return tuple(value)


def _validate_reason_or_none(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProbeRecordValidationError("reason must be a string or null")
    if value not in set(contract.PROOF_REASON_POOL) | set(contract.COMMON_REASONS):
        raise ProbeRecordValidationError("unknown reason")
    return value


def _validate_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ProbeRecordValidationError(f"{field} must be a string")
    return value


def _is_ordered_prefix(value: Sequence[str], expected: Sequence[str]) -> bool:
    return tuple(value) == tuple(expected[: len(value)])
