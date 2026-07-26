# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Record grammar and truth-table validation for sandbox probe ledgers."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
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
        record_type: str | None = None,
        proof: str | None = None,
    ) -> None:
        if code not in contract.STABLE_ERRORS:
            raise ValueError("invalid stable error code")
        if attempt_id is not None:
            validate_canonical_uuid(attempt_id)
        if record_type is not None and record_type not in contract.RECORD_TYPES:
            raise ValueError("invalid record type")
        if proof is not None and proof not in contract.CAPABILITY_ORDER:
            raise ValueError("invalid proof")
        super().__init__(code)
        self.code = code
        self.attempt_id = attempt_id
        self.record_type = record_type
        self.proof = proof


def raise_probe_error(
    code: str,
    *,
    attempt_id: str | None = None,
    record_type: str | None = None,
    proof: str | None = None,
) -> NoReturn:
    raise ProbeOperationError(
        code,
        attempt_id=attempt_id,
        record_type=record_type,
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
            "run_id": self.run_id,
            "selected": list(self.selected),
            "started_at": self.started_at,
            "type": contract.RECORD_TYPE_ATTEMPT_STARTED,
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
            "run_id": self.run_id,
            "state": self.state,
            "type": contract.RECORD_TYPE_PROOF_TERMINAL,
        }


@dataclass(frozen=True, slots=True)
class AttemptTerminalRecord:
    contract_version: int
    run_id: str
    attempt_id: str
    state: str
    terminal_reason: str | None
    finished_at: str

    def to_json_obj(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "contract_version": self.contract_version,
            "finished_at": self.finished_at,
            "run_id": self.run_id,
            "state": self.state,
            "terminal_reason": self.terminal_reason,
            "type": contract.RECORD_TYPE_ATTEMPT_TERMINAL,
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
    ledger_size_bytes: int
    ledger_identity: tuple[int, int] | None
    run_id: str | None
    attempts: tuple[ProbeAttemptReplay, ...]
    retry_permitted: bool

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)


@dataclass(frozen=True, slots=True)
class _ProofRuleContext:
    record: ProofTerminalRecord
    ordered_checks: tuple[str, ...]
    failed_reasons: frozenset[str]


@dataclass(frozen=True, slots=True)
class _TerminalDerivationContext:
    proofs: tuple[ProofTerminalRecord, ...]


@dataclass(frozen=True, slots=True)
class _RetryEligibilityContext:
    terminal: AttemptTerminalRecord
    proofs: tuple[ProofTerminalRecord, ...]


@dataclass(frozen=True, slots=True)
class _CancellationSuffixContext:
    proofs: tuple[ProofTerminalRecord, ...]
    start_index: int


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
    finished_at: str | None = None,
) -> AttemptTerminalRecord:
    state, terminal_reason = derive_attempt_terminal(proofs)
    return AttemptTerminalRecord(
        contract_version=contract.CONTRACT_VERSION,
        run_id=validate_canonical_uuid(run_id),
        attempt_id=validate_canonical_uuid(attempt_id),
        state=state,
        terminal_reason=terminal_reason,
        finished_at=validate_timestamp(finished_at or utc_timestamp_ms()),
    )


def validate_attempt_started_payload(
    payload: Mapping[str, Any],
) -> AttemptStartedRecord:
    _reject_unknown_fields(
        payload,
        contract.RECORD_TYPE_ATTEMPT_STARTED,
    )
    _validate_contract_version(payload.get("contract_version"))
    if payload.get("type") != contract.RECORD_TYPE_ATTEMPT_STARTED:
        raise ProbeRecordValidationError("wrong record type")
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
        contract.RECORD_TYPE_PROOF_TERMINAL,
    )
    _validate_contract_version(payload.get("contract_version"))
    if payload.get("type") != contract.RECORD_TYPE_PROOF_TERMINAL:
        raise ProbeRecordValidationError("wrong record type")
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
        contract.RECORD_TYPE_ATTEMPT_TERMINAL,
    )
    _validate_contract_version(payload.get("contract_version"))
    if payload.get("type") != contract.RECORD_TYPE_ATTEMPT_TERMINAL:
        raise ProbeRecordValidationError("wrong record type")
    terminal_reason = payload.get("terminal_reason")
    if (
        terminal_reason is not None
        and terminal_reason not in contract.ATTEMPT_TERMINAL_REASONS
    ):
        raise ProbeRecordValidationError("invalid attempt terminal reason")
    state = _validate_str(payload.get("state"), "state")
    if state not in contract.ATTEMPT_TERMINAL_STATES:
        raise ProbeRecordValidationError("invalid attempt terminal state")
    return AttemptTerminalRecord(
        contract_version=contract.CONTRACT_VERSION,
        run_id=validate_canonical_uuid(payload.get("run_id")),
        attempt_id=validate_canonical_uuid(payload.get("attempt_id")),
        state=state,
        terminal_reason=terminal_reason,
        finished_at=validate_timestamp(payload.get("finished_at")),
    )


def validate_proof_terminal_semantics(record: ProofTerminalRecord) -> None:
    rule = contract.PROOF_TERMINAL_RULES.get(record.state)
    if rule is None:
        raise ProbeRecordValidationError("invalid proof terminal state")
    failed_common = rule.get("failed_common_reasons", ())
    if not isinstance(failed_common, tuple):
        raise ProbeRecordValidationError("invalid proof terminal rule")
    context = _ProofRuleContext(
        record=record,
        ordered_checks=contract.PROOF_CHECKS[record.proof],
        failed_reasons=frozenset(
            set(contract.PROOF_SPECIFIC_REASONS[record.proof]) | set(failed_common)
        ),
    )
    for field in (
        contract.FIELD_CHECKS,
        contract.FIELD_REASON,
        contract.FIELD_DURATION_MS,
        contract.FIELD_CLEANUP_STATE,
    ):
        predicate_key = rule.get(field)
        if not isinstance(predicate_key, str):
            raise ProbeRecordValidationError("invalid proof terminal rule")
        if not _predicate_matches(predicate_key, context):
            raise ProbeRecordValidationError(_proof_terminal_error(record.state, field))


def cleanup_state_for(*, proof: str, state: str, reason: str | None) -> str:
    proof = validate_proof_name(proof)
    state_overrides = contract.CLEANUP_RESOLUTION["state_overrides"]
    reason_overrides = contract.CLEANUP_RESOLUTION["reason_overrides"]
    declared_defaults = contract.CLEANUP_RESOLUTION["declared_defaults"]
    if not (
        isinstance(state_overrides, dict)
        and isinstance(reason_overrides, dict)
        and isinstance(declared_defaults, dict)
    ):
        raise ProbeRecordValidationError("invalid cleanup resolution")
    if state in state_overrides:
        return _validate_str(state_overrides[state], "cleanup_state")
    if reason in reason_overrides:
        return _validate_str(reason_overrides[reason], "cleanup_state")
    return _validate_str(declared_defaults[proof], "cleanup_state")


def derive_attempt_terminal(
    proofs: Sequence[ProofTerminalRecord],
) -> tuple[str, str | None]:
    if not proofs:
        raise ProbeRecordValidationError("attempt needs proof rows")
    validate_cancellation_suffix(proofs)
    context = _TerminalDerivationContext(proofs=tuple(proofs))
    for rule in contract.TERMINAL_DERIVATION:
        predicate_key = rule.get("predicate")
        state = rule.get(contract.FIELD_STATE)
        terminal_reason = rule.get(contract.FIELD_TERMINAL_REASON)
        if not isinstance(predicate_key, str) or not isinstance(state, str):
            raise ProbeRecordValidationError("invalid terminal derivation rule")
        if _predicate_matches(predicate_key, context):
            if terminal_reason is not None and not isinstance(terminal_reason, str):
                raise ProbeRecordValidationError("invalid terminal derivation rule")
            return state, terminal_reason
    raise ProbeRecordValidationError("invalid proof truth table")


def attempt_terminal_retry_permitted(
    terminal: AttemptTerminalRecord,
    proofs: Sequence[ProofTerminalRecord],
) -> bool:
    context = _RetryEligibilityContext(terminal=terminal, proofs=tuple(proofs))
    for eligible in contract.RETRY_ELIGIBLE_TERMINALS:
        if terminal.state == eligible.get(
            contract.FIELD_STATE
        ) and terminal.terminal_reason == eligible.get(contract.FIELD_TERMINAL_REASON):
            proofs_predicate = eligible.get("proofs")
            if proofs_predicate is None:
                return True
            if not isinstance(proofs_predicate, str):
                raise ProbeRecordValidationError("invalid retry eligibility rule")
            return _predicate_matches(proofs_predicate, context)
    return False


def validate_attempt_terminal_matches(
    terminal: AttemptTerminalRecord,
    proofs: Sequence[ProofTerminalRecord],
) -> None:
    expected_state, expected_reason = derive_attempt_terminal(proofs)
    if terminal.state != expected_state or terminal.terminal_reason != expected_reason:
        raise ProbeRecordValidationError("attempt terminal does not match proof rows")


def validate_cancellation_suffix(proofs: Sequence[ProofTerminalRecord]) -> None:
    proof_rows = tuple(proofs)
    first_cancelled = next(
        (
            index
            for index, proof in enumerate(proof_rows)
            if proof.reason == contract.REASON_CANCELLED
        ),
        None,
    )
    if first_cancelled is None:
        return

    first = proof_rows[first_cancelled]
    first_context = _ProofRuleContext(
        record=first,
        ordered_checks=contract.PROOF_CHECKS[first.proof],
        failed_reasons=frozenset(
            set(contract.PROOF_SPECIFIC_REASONS[first.proof])
            | set(contract.FAILED_COMMON_REASONS)
        ),
    )
    first_predicates = (
        contract.CANCELLATION["first_started_predicate"],
        contract.CANCELLATION["first_unstarted_predicate"],
    )
    if not all(isinstance(predicate, str) for predicate in first_predicates):
        raise ProbeRecordValidationError("invalid cancellation rule")
    if not any(
        _predicate_matches(predicate, first_context)
        for predicate in first_predicates
        if isinstance(predicate, str)
    ):
        raise ProbeRecordValidationError("invalid cancellation first row")

    suffix_predicate = contract.CANCELLATION["suffix_predicate"]
    if not isinstance(suffix_predicate, str):
        raise ProbeRecordValidationError("invalid cancellation rule")
    suffix_context = _CancellationSuffixContext(
        proofs=proof_rows,
        start_index=first_cancelled + 1,
    )
    if not _predicate_matches(suffix_predicate, suffix_context):
        raise ProbeRecordValidationError("invalid cancellation suffix")


def _predicate_checks_complete(context: object) -> bool:
    if not isinstance(context, _ProofRuleContext):
        return False
    return context.record.checks == context.ordered_checks


def _predicate_checks_ordered_prefix(context: object) -> bool:
    if not isinstance(context, _ProofRuleContext):
        return False
    return _is_ordered_prefix(context.record.checks, context.ordered_checks)


def _predicate_checks_empty(context: object) -> bool:
    if not isinstance(context, _ProofRuleContext):
        return False
    return not context.record.checks


def _predicate_reason_null(context: object) -> bool:
    if not isinstance(context, _ProofRuleContext):
        return False
    return context.record.reason is None


def _predicate_reason_failed_specific_or_common(context: object) -> bool:
    if not isinstance(context, _ProofRuleContext):
        return False
    return context.record.reason in context.failed_reasons


def _predicate_reason_not_run(context: object) -> bool:
    if not isinstance(context, _ProofRuleContext):
        return False
    return context.record.reason in contract.NOT_RUN_REASONS


def _predicate_duration_non_negative_int(context: object) -> bool:
    if not isinstance(context, _ProofRuleContext):
        return False
    try:
        validate_non_negative_int(context.record.duration_ms)
    except ProbeRecordValidationError:
        return False
    return True


def _predicate_duration_null(context: object) -> bool:
    if not isinstance(context, _ProofRuleContext):
        return False
    return context.record.duration_ms is None


def _predicate_cleanup_expected(context: object) -> bool:
    if not isinstance(context, _ProofRuleContext):
        return False
    expected = cleanup_state_for(
        proof=context.record.proof,
        state=context.record.state,
        reason=context.record.reason,
    )
    return context.record.cleanup_state == expected


def _predicate_terminal_any_cleanup_unverified(context: object) -> bool:
    if not isinstance(context, _TerminalDerivationContext):
        return False
    return any(
        proof.cleanup_state == contract.CLEANUP_STATE_UNVERIFIED
        or proof.reason == contract.REASON_CLEANUP_UNVERIFIED
        for proof in context.proofs
    )


def _predicate_terminal_any_reason_cancelled(context: object) -> bool:
    if not isinstance(context, _TerminalDerivationContext):
        return False
    return any(proof.reason == contract.REASON_CANCELLED for proof in context.proofs)


def _predicate_terminal_any_reason_internal_error(context: object) -> bool:
    if not isinstance(context, _TerminalDerivationContext):
        return False
    return any(
        proof.reason == contract.REASON_INTERNAL_ERROR for proof in context.proofs
    )


def _predicate_terminal_any_failed_proof(context: object) -> bool:
    if not isinstance(context, _TerminalDerivationContext):
        return False
    return any(proof.state == contract.PROOF_STATE_FAILED for proof in context.proofs)


def _predicate_terminal_all_passed(context: object) -> bool:
    if not isinstance(context, _TerminalDerivationContext):
        return False
    return all(proof.state == contract.PROOF_STATE_PASSED for proof in context.proofs)


def _predicate_retry_all_cleanup_closed(context: object) -> bool:
    if not isinstance(context, _RetryEligibilityContext):
        return False
    return all(
        proof.cleanup_state
        in {
            contract.CLEANUP_STATE_VERIFIED,
            contract.CLEANUP_STATE_RETAINED_SYNTHETIC,
        }
        for proof in context.proofs
    )


def _predicate_cancellation_first_failed_cancelled_after_contact(
    context: object,
) -> bool:
    if not isinstance(context, _ProofRuleContext):
        return False
    return (
        context.record.state == contract.PROOF_STATE_FAILED
        and context.record.reason == contract.REASON_CANCELLED
        and _is_ordered_prefix(context.record.checks, context.ordered_checks)
        and _predicate_duration_non_negative_int(context)
    )


def _predicate_cancellation_first_not_run_cancelled_without_contact(
    context: object,
) -> bool:
    if not isinstance(context, _ProofRuleContext):
        return False
    return (
        context.record.state == contract.PROOF_STATE_NOT_RUN
        and context.record.reason == contract.REASON_CANCELLED
        and not context.record.checks
        and context.record.duration_ms is None
        and context.record.cleanup_state == contract.CLEANUP_STATE_VERIFIED
    )


def _predicate_cancellation_contiguous_not_run_cancelled_suffix(
    context: object,
) -> bool:
    if not isinstance(context, _CancellationSuffixContext):
        return False
    suffix_shape = contract.CANCELLATION["later_proof"]
    if not isinstance(suffix_shape, dict):
        return False
    for proof in context.proofs[context.start_index :]:
        if (
            proof.state != suffix_shape[contract.FIELD_STATE]
            or proof.checks != tuple(suffix_shape[contract.FIELD_CHECKS])
            or proof.reason != suffix_shape[contract.FIELD_REASON]
            or proof.duration_ms != suffix_shape[contract.FIELD_DURATION_MS]
            or proof.cleanup_state != suffix_shape[contract.FIELD_CLEANUP_STATE]
        ):
            return False
    return True


PREDICATE_REGISTRY: dict[str, Callable[[object], bool]] = {
    contract.PREDICATE_CHECKS_COMPLETE: _predicate_checks_complete,
    contract.PREDICATE_CHECKS_ORDERED_PREFIX: _predicate_checks_ordered_prefix,
    contract.PREDICATE_CHECKS_EMPTY: _predicate_checks_empty,
    contract.PREDICATE_REASON_NULL: _predicate_reason_null,
    contract.PREDICATE_REASON_FAILED_SPECIFIC_OR_COMMON: (
        _predicate_reason_failed_specific_or_common
    ),
    contract.PREDICATE_REASON_NOT_RUN: _predicate_reason_not_run,
    contract.PREDICATE_DURATION_NON_NEGATIVE_INT: (
        _predicate_duration_non_negative_int
    ),
    contract.PREDICATE_DURATION_NULL: _predicate_duration_null,
    contract.PREDICATE_CLEANUP_EXPECTED: _predicate_cleanup_expected,
    contract.PREDICATE_TERMINAL_ANY_CLEANUP_UNVERIFIED: (
        _predicate_terminal_any_cleanup_unverified
    ),
    contract.PREDICATE_TERMINAL_ANY_REASON_CANCELLED: (
        _predicate_terminal_any_reason_cancelled
    ),
    contract.PREDICATE_TERMINAL_ANY_REASON_INTERNAL_ERROR: (
        _predicate_terminal_any_reason_internal_error
    ),
    contract.PREDICATE_TERMINAL_ANY_FAILED_PROOF: (
        _predicate_terminal_any_failed_proof
    ),
    contract.PREDICATE_TERMINAL_ALL_PASSED: _predicate_terminal_all_passed,
    contract.PREDICATE_RETRY_ALL_CLEANUP_CLOSED: (_predicate_retry_all_cleanup_closed),
    contract.PREDICATE_CANCELLATION_FIRST_FAILED_CANCELLED_AFTER_CONTACT: (
        _predicate_cancellation_first_failed_cancelled_after_contact
    ),
    contract.PREDICATE_CANCELLATION_FIRST_NOT_RUN_CANCELLED_WITHOUT_CONTACT: (
        _predicate_cancellation_first_not_run_cancelled_without_contact
    ),
    contract.PREDICATE_CANCELLATION_CONTIGUOUS_NOT_RUN_CANCELLED_SUFFIX: (
        _predicate_cancellation_contiguous_not_run_cancelled_suffix
    ),
}


def _predicate_matches(predicate_key: str, context: object) -> bool:
    try:
        predicate = PREDICATE_REGISTRY[predicate_key]
    except KeyError as exc:
        raise ProbeRecordValidationError("unknown contract predicate") from exc
    return predicate(context)


def _proof_terminal_error(state: str, field: str) -> str:
    messages = {
        (contract.PROOF_STATE_PASSED, contract.FIELD_CHECKS): (
            "passed checks must be complete"
        ),
        (contract.PROOF_STATE_PASSED, contract.FIELD_REASON): (
            "passed reason must be null"
        ),
        (contract.PROOF_STATE_PASSED, contract.FIELD_DURATION_MS): (
            "duration must be a nonnegative integer"
        ),
        (contract.PROOF_STATE_PASSED, contract.FIELD_CLEANUP_STATE): (
            "invalid cleanup state"
        ),
        (contract.PROOF_STATE_FAILED, contract.FIELD_CHECKS): (
            "failed checks must be an ordered prefix"
        ),
        (contract.PROOF_STATE_FAILED, contract.FIELD_REASON): ("invalid failed reason"),
        (contract.PROOF_STATE_FAILED, contract.FIELD_DURATION_MS): (
            "duration must be a nonnegative integer"
        ),
        (contract.PROOF_STATE_FAILED, contract.FIELD_CLEANUP_STATE): (
            "invalid cleanup state"
        ),
        (contract.PROOF_STATE_NOT_RUN, contract.FIELD_CHECKS): (
            "not_run checks must be empty"
        ),
        (contract.PROOF_STATE_NOT_RUN, contract.FIELD_REASON): (
            "invalid not_run reason"
        ),
        (contract.PROOF_STATE_NOT_RUN, contract.FIELD_DURATION_MS): (
            "not_run duration must be null"
        ),
        (contract.PROOF_STATE_NOT_RUN, contract.FIELD_CLEANUP_STATE): (
            "invalid cleanup state"
        ),
    }
    return messages[(state, field)]


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


def _reject_unknown_fields(payload: Mapping[str, Any], record_type: str) -> None:
    fields = contract.RECORD_FIELDS.get(record_type)
    if fields is None or set(payload) != set(fields):
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
