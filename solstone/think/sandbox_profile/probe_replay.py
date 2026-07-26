# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Fail-closed replay for the sandbox production-probe ledger.

This ledger coordinates probes that may touch disposable production-facing
services. Replay therefore treats ambiguity as unsafe instead of attempting
repair: a torn record, cardinality mismatch, missing attempt directory, orphan
attempt directory, or wrong attempt-directory mode becomes a stale attempt that
requires external whole-profile stop. The forward-barrier rule is intentionally
strict for the same reason. A new attempt may contact production only after its
own start record has reached disk, and only two prior terminal classes are
considered settled enough to permit that write: the clean-success class, or the
proof-failure degradation class whose proof rows all report closed cleanup.
Cancelled, internal-error, cleanup-unverified, and incomplete attempts remain
stale until the whole profile is removed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from solstone.think.sandbox_profile import json_codec, probe_records, probe_slot
from solstone.think.sandbox_profile import probe_contract as contract


def replay_probe_ledger(journal_path: Path) -> probe_records.ProbeReplay:
    journal = Path(journal_path)
    ledger_path = contract.probe_ledger_path(journal)
    raw_payloads = _read_framed_payloads(ledger_path)
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
        run_id=run_id,
        attempts=attempts,
        retry_permitted=retry_permitted,
    )


def _read_framed_payloads(ledger_path: Path) -> tuple[dict[str, Any], ...]:
    try:
        size = ledger_path.stat().st_size
    except FileNotFoundError:
        return ()
    except OSError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
    if size > contract.MAX_LEDGER_BYTES:
        probe_records.raise_probe_error(contract.STABLE_ERROR_ATTEMPT_LIMIT_REACHED)
    try:
        raw = ledger_path.read_bytes()
    except OSError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
    if not raw:
        return ()
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
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        if end != len(text) or not isinstance(payload, dict):
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        payloads.append(payload)
    return tuple(payloads)


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
    while index < len(payloads):
        if not prior_retry_permitted:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        try:
            start = probe_records.validate_attempt_started_payload(payloads[index])
        except probe_records.ProbeRecordValidationError:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        run_id = _merge_run_id(run_id, start.run_id)
        if start.attempt_id in seen_attempt_ids:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        seen_attempt_ids.add(start.attempt_id)
        index += 1

        proofs: list[probe_records.ProofTerminalRecord] = []
        for expected_proof in start.execution_order:
            if index >= len(payloads):
                probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
            try:
                proof = probe_records.validate_proof_terminal_payload(payloads[index])
            except probe_records.ProbeRecordValidationError:
                probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
            run_id = _merge_run_id(run_id, proof.run_id)
            if proof.attempt_id != start.attempt_id or proof.proof != expected_proof:
                probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
            proofs.append(proof)
            index += 1

        if index >= len(payloads):
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        try:
            terminal = probe_records.validate_attempt_terminal_payload(payloads[index])
        except probe_records.ProbeRecordValidationError:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        run_id = _merge_run_id(run_id, terminal.run_id)
        if terminal.attempt_id != start.attempt_id:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        try:
            probe_records.validate_attempt_terminal_matches(terminal, proofs)
        except probe_records.ProbeRecordValidationError:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        attempt = probe_records.ProbeAttemptReplay(
            start=start,
            proofs=tuple(proofs),
            terminal=terminal,
        )
        attempts.append(attempt)
        prior_retry_permitted = probe_records.attempt_terminal_retry_permitted(
            terminal, proofs
        )
        index += 1
    return tuple(attempts), run_id, seen_attempt_ids


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
