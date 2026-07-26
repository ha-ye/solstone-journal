# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OS-level probe slot ownership and attempt-directory identity."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from solstone.think.journal_io.lease import acquire_file_lease
from solstone.think.sandbox_profile import probe_contract as contract
from solstone.think.sandbox_profile import probe_durability, probe_records


class _LeaseHandle(Protocol):
    path: Path

    @property
    def owned(self) -> bool: ...

    def release(self) -> None: ...


@dataclass(slots=True)
class ProbeSlot:
    journal_path: Path
    run_id: str
    ledger_path: Path
    lock_path: Path
    attempts_parent_path: Path
    _lease: _LeaseHandle | None

    @property
    def owned(self) -> bool:
        return self._lease is not None and self._lease.owned

    def release(self) -> None:
        if self._lease is None:
            return
        self._lease.release()
        self._lease = None

    def __enter__(self) -> ProbeSlot:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


def acquire_probe_slot(journal_path: Path, *, run_id: str) -> ProbeSlot:
    journal = Path(journal_path)
    try:
        run_id = probe_records.validate_canonical_uuid(run_id)
    except probe_records.ProbeRecordValidationError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
    lock_path = contract.probe_lock_path(journal)
    lease = acquire_file_lease(lock_path, attempts=1, retry_max_seconds=0.0)
    if lease is None:
        probe_records.raise_probe_error(contract.STABLE_ERROR_PROBE_ACTIVE)

    slot = ProbeSlot(
        journal_path=journal,
        run_id=run_id,
        ledger_path=contract.probe_ledger_path(journal),
        lock_path=lock_path,
        attempts_parent_path=contract.probe_attempts_parent_path(journal),
        _lease=lease,
    )
    try:
        from solstone.think.sandbox_profile.probe_replay import replay_probe_ledger

        replay = replay_probe_ledger(journal)
        if replay.run_id is not None and replay.run_id != run_id:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
    except BaseException:
        slot.release()
        raise
    return slot


def assert_probe_slot_owned(slot: ProbeSlot) -> None:
    if not slot.owned:
        probe_records.raise_probe_error(contract.STABLE_ERROR_PROBE_ACTIVE)


def create_attempt_directory(slot: ProbeSlot, attempt_id: str) -> Path:
    attempt_id = _validate_attempt_id_for_error(attempt_id)
    path = slot.attempts_parent_path / attempt_id
    try:
        probe_durability._mkdir(
            slot.attempts_parent_path,
            mode=contract.ATTEMPT_DIR_MODE,
            parents=True,
        )
        probe_durability._mkdir(path, mode=contract.ATTEMPT_DIR_MODE)
        probe_durability._fsync_directory(slot.attempts_parent_path)
    except FileExistsError:
        probe_records.raise_probe_error(
            contract.STABLE_ERROR_STALE_ATTEMPT, attempt_id=attempt_id
        )
    except OSError:
        probe_records.raise_probe_error(
            contract.STABLE_ERROR_RECORD_WRITE_FAILED, attempt_id=attempt_id
        )
    return path


def validate_attempt_directory_set(journal_path: Path, attempt_ids: set[str]) -> None:
    parent = contract.probe_attempts_parent_path(journal_path)
    if not parent.exists():
        if attempt_ids:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        return
    try:
        parent_stat = parent.lstat()
    except OSError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)

    seen: set[str] = set()
    try:
        children = sorted(parent.iterdir(), key=lambda item: item.name)
    except OSError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
    for child in children:
        try:
            attempt_id = probe_records.validate_canonical_uuid(child.name)
        except probe_records.ProbeRecordValidationError:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        try:
            child_stat = child.lstat()
        except OSError:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISDIR(child_stat.st_mode):
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        if stat.S_IMODE(child_stat.st_mode) != contract.ATTEMPT_DIR_MODE:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        if attempt_id not in attempt_ids or attempt_id in seen:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        seen.add(attempt_id)
    if seen != attempt_ids:
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)


def _validate_attempt_id_for_error(value: str) -> str:
    try:
        return probe_records.validate_canonical_uuid(value)
    except probe_records.ProbeRecordValidationError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
