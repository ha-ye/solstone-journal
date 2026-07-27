# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Fail-closed replay for the sandbox production-probe ledger.

Replay validates any preexisting ledger as one regular non-symlink file and
reads it through a no-follow descriptor before partial JSONL validation. Torn
records, bounded decode failures including recursion, cardinality mismatches,
attempt-directory mismatches, and unsafe terminal classes become stale attempts
that require external whole-profile stop. The replay result retains ledger
byte-size and identity metadata for the writer-side descriptor checks.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solstone.think import json_codec
from solstone.think.sandbox_profile import probe_contract as contract
from solstone.think.sandbox_profile import probe_records, probe_slot

_ReplayRecord = (
    probe_records.AttemptStartedRecord
    | probe_records.ProofTerminalRecord
    | probe_records.AttemptTerminalRecord
)
_RECORD_VALIDATORS: dict[str, Callable[[Mapping[str, Any]], _ReplayRecord]] = {
    contract.RECORD_TYPE_ATTEMPT_STARTED: probe_records.validate_attempt_started_payload,
    contract.RECORD_TYPE_PROOF_TERMINAL: probe_records.validate_proof_terminal_payload,
    contract.RECORD_TYPE_ATTEMPT_TERMINAL: probe_records.validate_attempt_terminal_payload,
}


@dataclass(frozen=True, slots=True)
class _LedgerRead:
    payloads: tuple[dict[str, Any], ...]
    size: int
    identity: tuple[int, int] | None


def replay_probe_ledger(journal_path: Path) -> probe_records.ProbeReplay:
    journal = Path(journal_path)
    ledger_path = contract.probe_ledger_path(journal)
    ledger = _read_framed_payloads(ledger_path)
    raw_payloads = ledger.payloads
    attempt_count_type = contract.RECORD_CARDINALITY["attempt_count_type"]
    attempt_count = sum(
        1 for payload in raw_payloads if payload.get("type") == attempt_count_type
    )
    if attempt_count >= contract.MAX_ATTEMPTS:
        probe_records.raise_probe_error(contract.STABLE_ERROR_ATTEMPT_LIMIT_REACHED)
    attempts, run_id, attempt_ids = _fold_records(raw_payloads)
    probe_slot.validate_attempt_directory_set(journal, attempt_ids)
    retry_permitted = _retry_permitted(attempts)
    if not retry_permitted:
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
    return probe_records.ProbeReplay(
        journal_path=journal,
        ledger_path=ledger_path,
        ledger_size_bytes=ledger.size,
        ledger_identity=ledger.identity,
        run_id=run_id,
        attempts=attempts,
        retry_permitted=retry_permitted,
    )


def _read_framed_payloads(ledger_path: Path) -> _LedgerRead:
    try:
        ledger_stat = ledger_path.lstat()
    except FileNotFoundError:
        return _LedgerRead(payloads=(), size=0, identity=None)
    except OSError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
    if stat.S_ISLNK(ledger_stat.st_mode) or not stat.S_ISREG(ledger_stat.st_mode):
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
    size = ledger_stat.st_size
    if size > contract.MAX_LEDGER_BYTES:
        probe_records.raise_probe_error(contract.STABLE_ERROR_ATTEMPT_LIMIT_REACHED)
    identity = (ledger_stat.st_dev, ledger_stat.st_ino)
    fd: int | None = None
    try:
        fd = os.open(ledger_path, os.O_RDONLY | os.O_NOFOLLOW)
        fd_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(fd_stat.st_mode)
            or (fd_stat.st_dev, fd_stat.st_ino) != identity
            or fd_stat.st_size != size
        ):
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        raw = os.read(fd, size + 1)
        if len(raw) != size:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
    except probe_records.ProbeOperationError:
        raise
    except OSError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
    finally:
        if fd is not None:
            os.close(fd)
    if not raw:
        return _LedgerRead(payloads=(), size=size, identity=identity)
    if not raw.endswith(b"\n"):
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
    payloads: list[dict[str, Any]] = []
    for line in raw.splitlines(keepends=True):
        if line == b"\n" or not line.endswith(b"\n"):
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        body = line[:-1]
        try:
            text = body.decode("utf-8")
            decoder = json.JSONDecoder(
                object_pairs_hook=json_codec.reject_duplicate_keys
            )
            payload, end = decoder.raw_decode(text)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError):
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        if end != len(text) or not isinstance(payload, dict):
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        payloads.append(payload)
    return _LedgerRead(payloads=tuple(payloads), size=size, identity=identity)


def _fold_records(
    payloads: tuple[dict[str, Any], ...],
) -> tuple[tuple[probe_records.ProbeAttemptReplay, ...], str | None, set[str]]:
    if not payloads:
        return (), None, set()

    run_id: str | None = None
    seen_attempt_ids: set[str] = set()
    attempts: list[probe_records.ProbeAttemptReplay] = []
    index = 0
    prior_retry_permitted = True
    attempt_sequence = _attempt_sequence()
    while index < len(payloads):
        if not prior_retry_permitted:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        start: probe_records.AttemptStartedRecord | None = None
        proofs: list[probe_records.ProofTerminalRecord] = []
        terminal: probe_records.AttemptTerminalRecord | None = None
        for step in attempt_sequence:
            record_type = _step_record_type(step)
            if record_type == contract.RECORD_TYPE_ATTEMPT_STARTED:
                _require_step_count(step, 1)
                if index >= len(payloads):
                    probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
                record = _validate_step_record(record_type, payloads[index])
                if not isinstance(record, probe_records.AttemptStartedRecord):
                    probe_records.raise_probe_error(
                        contract.STABLE_ERROR_INTERNAL_ERROR
                    )
                start = record
                run_id = _merge_run_id(run_id, start.run_id)
                if start.attempt_id in seen_attempt_ids:
                    probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
                seen_attempt_ids.add(start.attempt_id)
                index += 1
            elif record_type == contract.RECORD_TYPE_PROOF_TERMINAL:
                if start is None:
                    probe_records.raise_probe_error(
                        contract.STABLE_ERROR_INTERNAL_ERROR
                    )
                if step.get("count") != "len(execution_order)":
                    probe_records.raise_probe_error(
                        contract.STABLE_ERROR_INTERNAL_ERROR
                    )
                if step.get("order") != "execution_order":
                    probe_records.raise_probe_error(
                        contract.STABLE_ERROR_INTERNAL_ERROR
                    )
                for expected_proof in start.execution_order:
                    if index >= len(payloads):
                        probe_records.raise_probe_error(
                            contract.STABLE_ERROR_STALE_ATTEMPT
                        )
                    record = _validate_step_record(record_type, payloads[index])
                    if not isinstance(record, probe_records.ProofTerminalRecord):
                        probe_records.raise_probe_error(
                            contract.STABLE_ERROR_INTERNAL_ERROR
                        )
                    run_id = _merge_run_id(run_id, record.run_id)
                    if (
                        record.attempt_id != start.attempt_id
                        or record.proof != expected_proof
                    ):
                        probe_records.raise_probe_error(
                            contract.STABLE_ERROR_STALE_ATTEMPT
                        )
                    proofs.append(record)
                    index += 1
            elif record_type == contract.RECORD_TYPE_ATTEMPT_TERMINAL:
                _require_step_count(step, 1)
                if start is None:
                    probe_records.raise_probe_error(
                        contract.STABLE_ERROR_INTERNAL_ERROR
                    )
                if index >= len(payloads):
                    probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
                record = _validate_step_record(record_type, payloads[index])
                if not isinstance(record, probe_records.AttemptTerminalRecord):
                    probe_records.raise_probe_error(
                        contract.STABLE_ERROR_INTERNAL_ERROR
                    )
                terminal = record
                run_id = _merge_run_id(run_id, terminal.run_id)
                if terminal.attempt_id != start.attempt_id:
                    probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
                try:
                    probe_records.validate_attempt_terminal_matches(terminal, proofs)
                except probe_records.ProbeRecordValidationError:
                    probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
                index += 1
            else:
                probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
        if start is None or terminal is None:
            probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
        attempt = probe_records.ProbeAttemptReplay(
            start=start,
            proofs=tuple(proofs),
            terminal=terminal,
        )
        attempts.append(attempt)
        prior_retry_permitted = probe_records.attempt_terminal_retry_permitted(
            terminal, proofs
        )
    return tuple(attempts), run_id, seen_attempt_ids


def _attempt_sequence() -> tuple[dict[str, object], ...]:
    sequence = contract.RECORD_CARDINALITY.get("attempt_sequence")
    if not isinstance(sequence, tuple) or not all(
        isinstance(step, dict) for step in sequence
    ):
        probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
    return sequence


def _step_record_type(step: dict[str, object]) -> str:
    record_type = step.get("type")
    if not isinstance(record_type, str) or record_type not in contract.RECORD_TYPES:
        probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
    return record_type


def _require_step_count(step: dict[str, object], expected: int) -> None:
    if step.get("count") != expected:
        probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)


def _validate_step_record(
    record_type: str,
    payload: Mapping[str, Any],
) -> _ReplayRecord:
    validator = _RECORD_VALIDATORS.get(record_type)
    if validator is None:
        probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
    try:
        return validator(payload)
    except probe_records.ProbeRecordValidationError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)


def _merge_run_id(existing: str | None, value: str) -> str:
    if existing is not None and existing != value:
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
    return value


def _retry_permitted(attempts: tuple[probe_records.ProbeAttemptReplay, ...]) -> bool:
    if not attempts:
        return True
    latest = attempts[-1]
    return probe_records.attempt_terminal_retry_permitted(
        latest.terminal, latest.proofs
    )
