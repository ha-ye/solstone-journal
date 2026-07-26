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
    _started_barrier_complete: bool = True
    _last_proof_barrier_complete: bool = True
    _terminal_written: bool = False
    _poisoned_code: str | None = None

    def assert_contact_allowed(self, proof: str) -> None:
        self._raise_if_poisoned()
        expected = self._next_proof()
        try:
            proof = probe_records.validate_proof_name(proof)
        except probe_records.ProbeRecordValidationError:
            probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
        if not self._started_barrier_complete or not self._last_proof_barrier_complete:
            probe_records.raise_probe_error(
                contract.STABLE_ERROR_INTERNAL_ERROR, proof=proof
            )
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
        self._raise_if_poisoned()
        self._reject_after_terminal()
        expected = self._next_proof()
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
        self._append_record(
            record.to_json_obj(), record_kind=contract.RECORD_KIND_PROOF_TERMINAL
        )
        self._proofs.append(record)
        self._next_proof_index += 1
        self._proof_terminal_count += 1
        self._last_proof_barrier_complete = True

    def write_attempt_terminal(
        self,
        *,
        duration_ms: int,
        finished_at: str | None = None,
    ) -> None:
        self._raise_if_poisoned()
        self._reject_after_terminal()
        if self._proof_terminal_count != len(self.start.execution_order):
            probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
        try:
            record = probe_records.build_attempt_terminal_record(
                run_id=self.start.run_id,
                attempt_id=self.start.attempt_id,
                proofs=self._proofs,
                duration_ms=duration_ms,
                finished_at=finished_at,
            )
        except probe_records.ProbeRecordValidationError:
            probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
        self._append_record(
            record.to_json_obj(), record_kind=contract.RECORD_KIND_ATTEMPT_TERMINAL
        )
        self._terminal_written = True

    def _append_record(self, record: dict[str, object], *, record_kind: str) -> None:
        try:
            probe_durability.append_jsonl_strict(self.slot.ledger_path, record)
        except probe_records.ProbeOperationError as exc:
            if exc.code == contract.STABLE_ERROR_RECORD_WRITE_FAILED:
                self._poisoned_code = exc.code
            probe_records.raise_probe_error(
                exc.code,
                attempt_id=self.start.attempt_id,
                record_kind=record_kind,
            )

    def _raise_if_poisoned(self) -> None:
        if self._poisoned_code is not None:
            probe_records.raise_probe_error(
                self._poisoned_code, attempt_id=self.start.attempt_id
            )

    def _reject_after_terminal(self) -> None:
        if self._terminal_written:
            probe_records.raise_probe_error(
                contract.STABLE_ERROR_INTERNAL_ERROR, attempt_id=self.start.attempt_id
            )

    def _next_proof(self) -> str:
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
    slot.raise_if_poisoned()
    probe_slot.assert_probe_slot_owned(slot)
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
        probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
    try:
        attempt_dir = probe_slot.create_attempt_directory(slot, start.attempt_id)
    except probe_records.ProbeOperationError as exc:
        if exc.code in {
            contract.STABLE_ERROR_RECORD_WRITE_FAILED,
            contract.STABLE_ERROR_STALE_ATTEMPT,
        }:
            slot.poison(exc.code)
        probe_records.raise_probe_error(exc.code, attempt_id=start.attempt_id)
    try:
        probe_durability.append_jsonl_strict(slot.ledger_path, start.to_json_obj())
    except probe_records.ProbeOperationError as exc:
        if exc.code == contract.STABLE_ERROR_RECORD_WRITE_FAILED:
            slot.poison(exc.code)
        probe_records.raise_probe_error(
            exc.code,
            attempt_id=start.attempt_id,
            record_kind=contract.RECORD_KIND_ATTEMPT_STARTED,
        )
    return ProbeAttemptWriter(slot=slot, start=start, attempt_dir=attempt_dir)
