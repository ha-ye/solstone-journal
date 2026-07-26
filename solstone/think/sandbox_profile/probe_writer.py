# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Ordered writer for the sandbox production-probe ledger."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from solstone.think.sandbox_profile import probe_contract as contract
from solstone.think.sandbox_profile import probe_durability, probe_records, probe_slot


@dataclass(slots=True)
class ProbeAttemptWriter:
    slot: probe_slot.ProbeSlot
    start: probe_records.AttemptStartedRecord
    attempt_dir: Path
    _proofs: list[probe_records.ProofTerminalRecord] = field(default_factory=list)
    _next_proof_index: int = 0
    _proof_terminal_count: int = 0
    _terminal_written: bool = False

    def assert_contact_allowed(self, proof: str) -> None:
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
