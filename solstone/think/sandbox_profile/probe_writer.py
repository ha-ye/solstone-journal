# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Ordered writer for the sandbox production-probe ledger."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from solstone.think.sandbox_profile import probe_contract as contract
from solstone.think.sandbox_profile import probe_durability, probe_records, probe_slot

T = TypeVar("T")


@dataclass(slots=True)
class ProbeAttemptWriter:
    slot: probe_slot.ProbeSlot
    start: probe_records.AttemptStartedRecord
    attempt_dir: Path
    _proofs: list[probe_records.ProofTerminalRecord] = field(default_factory=list)
    _next_proof_index: int = 0
    _proof_terminal_count: int = 0
    _terminal_written: bool = False
    _contact_consumed: set[str] = field(default_factory=set)

    def dispatch_contact(self, proof: str, operation: Callable[[], T]) -> T:
        with self.slot.operation_guard():
            self._raise_if_not_active_unlocked()
            self._reject_after_terminal_unlocked()
            self.slot.revalidate_identities_unlocked()
            expected = self._next_proof_unlocked()
            try:
                proof = probe_records.validate_proof_name(proof)
            except probe_records.ProbeRecordValidationError:
                probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
            if proof != expected:
                probe_records.raise_probe_error(
                    contract.STABLE_ERROR_INTERNAL_ERROR, proof=proof
                )
            if proof in self._contact_consumed:
                probe_records.raise_probe_error(
                    contract.STABLE_ERROR_INTERNAL_ERROR, proof=proof
                )
            self._contact_consumed.add(proof)
            return operation()

    def write_proof_terminal(
        self,
        *,
        proof: str,
        state: str,
        checks: Sequence[str],
        reason: str | None,
        duration_ms: int | None,
        finished_at: str | None = None,
    ) -> None:
        with self.slot.operation_guard():
            self._raise_if_not_active_unlocked()
            self._reject_after_terminal_unlocked()
            self.slot.revalidate_identities_unlocked()
            expected = self._next_proof_unlocked()
            try:
                proof = probe_records.validate_proof_name(proof)
            except probe_records.ProbeRecordValidationError:
                probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
            if proof != expected:
                probe_records.raise_probe_error(
                    contract.STABLE_ERROR_INTERNAL_ERROR, proof=proof
                )
            self._validate_terminal_contact_unlocked(proof=proof, state=state)
            try:
                record = probe_records.build_proof_terminal_record(
                    run_id=self.start.run_id,
                    attempt_id=self.start.attempt_id,
                    proof=proof,
                    state=state,
                    checks=checks,
                    reason=reason,
                    duration_ms=duration_ms,
                    finished_at=finished_at,
                )
            except probe_records.ProbeRecordValidationError:
                probe_records.raise_probe_error(
                    contract.STABLE_ERROR_INTERNAL_ERROR, proof=proof
                )
            self._append_record_unlocked(
                record.to_json_obj(),
                record_type=contract.RECORD_TYPE_PROOF_TERMINAL,
            )
            self._proofs.append(record)
            self._next_proof_index += 1
            self._proof_terminal_count += 1

    def write_cancelled_attempt(
        self,
        *,
        proof: str,
        state: str,
        checks: Sequence[str],
        duration_ms: int | None,
        finished_at: str | None = None,
    ) -> None:
        with self.slot.operation_guard():
            self._raise_if_not_active_unlocked()
            self._reject_after_terminal_unlocked()
            self.slot.revalidate_identities_unlocked()
            try:
                first = self._build_first_cancelled_record_unlocked(
                    proof=proof,
                    state=state,
                    checks=checks,
                    duration_ms=duration_ms,
                    finished_at=finished_at,
                )
            except (
                probe_records.ProbeRecordValidationError,
                probe_records.ProbeOperationError,
            ):
                self.slot._poison_unlocked(contract.STABLE_ERROR_INTERNAL_ERROR)
                probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)

            self._append_record_unlocked(
                first.to_json_obj(),
                record_type=contract.RECORD_TYPE_PROOF_TERMINAL,
            )
            self._proofs.append(first)
            self._next_proof_index += 1
            self._proof_terminal_count += 1

            try:
                later_shape = _cancellation_later_proof_shape()
            except probe_records.ProbeRecordValidationError:
                self.slot._poison_unlocked(contract.STABLE_ERROR_INTERNAL_ERROR)
                probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
            for later_proof in self.start.execution_order[self._next_proof_index :]:
                try:
                    record = probe_records.build_proof_terminal_record(
                        run_id=self.start.run_id,
                        attempt_id=self.start.attempt_id,
                        proof=later_proof,
                        state=later_shape.state,
                        checks=later_shape.checks,
                        reason=later_shape.reason,
                        duration_ms=later_shape.duration_ms,
                        finished_at=finished_at,
                    )
                    if record.cleanup_state != later_shape.cleanup_state:
                        raise probe_records.ProbeRecordValidationError(
                            "invalid cancellation later proof shape"
                        )
                except probe_records.ProbeRecordValidationError:
                    self.slot._poison_unlocked(contract.STABLE_ERROR_INTERNAL_ERROR)
                    probe_records.raise_probe_error(
                        contract.STABLE_ERROR_INTERNAL_ERROR,
                        proof=later_proof,
                    )
                self._append_record_unlocked(
                    record.to_json_obj(),
                    record_type=contract.RECORD_TYPE_PROOF_TERMINAL,
                )
                self._proofs.append(record)
                self._next_proof_index += 1
                self._proof_terminal_count += 1

            try:
                terminal = probe_records.build_attempt_terminal_record(
                    run_id=self.start.run_id,
                    attempt_id=self.start.attempt_id,
                    proofs=self._proofs,
                    finished_at=finished_at,
                )
            except probe_records.ProbeRecordValidationError:
                self.slot._poison_unlocked(contract.STABLE_ERROR_INTERNAL_ERROR)
                probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
            self._append_record_unlocked(
                terminal.to_json_obj(),
                record_type=contract.RECORD_TYPE_ATTEMPT_TERMINAL,
            )
            self._terminal_written = True
            self.slot.mark_spent_unlocked()

    def write_attempt_terminal(
        self,
        *,
        finished_at: str | None = None,
    ) -> None:
        with self.slot.operation_guard():
            self._raise_if_not_active_unlocked()
            self._reject_after_terminal_unlocked()
            self.slot.revalidate_identities_unlocked()
            if self._proof_terminal_count != len(self.start.execution_order):
                probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
            try:
                record = probe_records.build_attempt_terminal_record(
                    run_id=self.start.run_id,
                    attempt_id=self.start.attempt_id,
                    proofs=self._proofs,
                    finished_at=finished_at,
                )
            except probe_records.ProbeRecordValidationError:
                probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
            self._append_record_unlocked(
                record.to_json_obj(),
                record_type=contract.RECORD_TYPE_ATTEMPT_TERMINAL,
            )
            self._terminal_written = True
            self.slot.mark_spent_unlocked()

    def _append_record_unlocked(
        self, record: dict[str, object], *, record_type: str
    ) -> None:
        data = probe_durability.encode_jsonl_record(record)
        self.slot.append_encoded_record_unlocked(
            data,
            record_type=record_type,
            attempt_id=self.start.attempt_id,
        )

    def _raise_if_not_active_unlocked(self) -> None:
        self.slot.assert_active_writer_unlocked(self.start.attempt_id)

    def _reject_after_terminal_unlocked(self) -> None:
        if self._terminal_written:
            probe_records.raise_probe_error(
                contract.STABLE_ERROR_INTERNAL_ERROR, attempt_id=self.start.attempt_id
            )

    def _next_proof_unlocked(self) -> str:
        if self._next_proof_index >= len(self.start.execution_order):
            probe_records.raise_probe_error(
                contract.STABLE_ERROR_INTERNAL_ERROR, attempt_id=self.start.attempt_id
            )
        return self.start.execution_order[self._next_proof_index]

    def _build_first_cancelled_record_unlocked(
        self,
        *,
        proof: str,
        state: str,
        checks: Sequence[str],
        duration_ms: int | None,
        finished_at: str | None,
    ) -> probe_records.ProofTerminalRecord:
        expected = self._next_proof_unlocked()
        proof = probe_records.validate_proof_name(proof)
        if proof != expected:
            raise probe_records.ProbeRecordValidationError("wrong cancellation proof")
        consumed = proof in self._contact_consumed
        if consumed and state != contract.PROOF_STATE_FAILED:
            raise probe_records.ProbeRecordValidationError("invalid cancellation state")
        if not consumed and state != contract.PROOF_STATE_NOT_RUN:
            raise probe_records.ProbeRecordValidationError("invalid cancellation state")
        return probe_records.build_proof_terminal_record(
            run_id=self.start.run_id,
            attempt_id=self.start.attempt_id,
            proof=proof,
            state=state,
            checks=checks,
            reason=contract.REASON_CANCELLED,
            duration_ms=duration_ms,
            finished_at=finished_at,
        )

    def _validate_terminal_contact_unlocked(self, *, proof: str, state: str) -> None:
        consumed = proof in self._contact_consumed
        if state in {contract.PROOF_STATE_PASSED, contract.PROOF_STATE_FAILED}:
            if not consumed:
                probe_records.raise_probe_error(
                    contract.STABLE_ERROR_INTERNAL_ERROR, proof=proof
                )
            return
        if state == contract.PROOF_STATE_NOT_RUN and consumed:
            probe_records.raise_probe_error(
                contract.STABLE_ERROR_INTERNAL_ERROR, proof=proof
            )


def begin_probe_attempt(
    slot: probe_slot.ProbeSlot,
    *,
    selected: Sequence[str],
    execution_order: Sequence[str],
    attempt_id: str | None = None,
    started_at: str | None = None,
) -> ProbeAttemptWriter:
    with slot.operation_guard():
        if slot.state != probe_slot.SLOT_STATE_UNUSED:
            probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
        slot.state = probe_slot.SLOT_STATE_ACTIVE

        attempt_id = attempt_id or probe_records.new_attempt_id()
        try:
            start = probe_records.build_attempt_started_record(
                run_id=slot.run_id,
                attempt_id=attempt_id,
                selected=selected,
                execution_order=execution_order,
                started_at=started_at,
            )
        except probe_records.ProbeRecordValidationError:
            slot.mark_spent_unlocked()
            probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)

        data = probe_durability.encode_jsonl_record(start.to_json_obj())
        slot.revalidate_identities_unlocked()
        try:
            slot.check_ledger_capacity_unlocked(data, poison_on_failure=False)
        except probe_records.ProbeOperationError:
            slot.mark_spent_unlocked()
            raise

        attempt_dir = probe_slot.create_attempt_directory_unlocked(
            slot, start.attempt_id
        )
        slot.append_encoded_record_unlocked(
            data,
            record_type=contract.RECORD_TYPE_ATTEMPT_STARTED,
            attempt_id=start.attempt_id,
            capacity_checked=True,
        )
        return ProbeAttemptWriter(slot=slot, start=start, attempt_dir=attempt_dir)


@dataclass(frozen=True, slots=True)
class _CancellationLaterProofShape:
    state: str
    checks: tuple[str, ...]
    reason: str | None
    duration_ms: int | None
    cleanup_state: str


def _cancellation_later_proof_shape() -> _CancellationLaterProofShape:
    try:
        shape = contract.CANCELLATION["later_proof"]
    except KeyError as exc:
        raise probe_records.ProbeRecordValidationError(
            "invalid cancellation rule"
        ) from exc
    if not isinstance(shape, dict):
        raise probe_records.ProbeRecordValidationError("invalid cancellation rule")
    try:
        state = shape[contract.FIELD_STATE]
        checks = shape[contract.FIELD_CHECKS]
        reason = shape[contract.FIELD_REASON]
        duration_ms = shape[contract.FIELD_DURATION_MS]
        cleanup_state = shape[contract.FIELD_CLEANUP_STATE]
    except KeyError as exc:
        raise probe_records.ProbeRecordValidationError(
            "invalid cancellation rule"
        ) from exc
    if not isinstance(state, str):
        raise probe_records.ProbeRecordValidationError("invalid cancellation rule")
    if not isinstance(checks, tuple) or not all(
        isinstance(check, str) for check in checks
    ):
        raise probe_records.ProbeRecordValidationError("invalid cancellation rule")
    if reason is not None and not isinstance(reason, str):
        raise probe_records.ProbeRecordValidationError("invalid cancellation rule")
    if duration_ms is not None:
        probe_records.validate_non_negative_int(duration_ms)
    if not isinstance(cleanup_state, str):
        raise probe_records.ProbeRecordValidationError("invalid cancellation rule")
    return _CancellationLaterProofShape(
        state=state,
        checks=checks,
        reason=reason,
        duration_ms=duration_ms,
        cleanup_state=cleanup_state,
    )
