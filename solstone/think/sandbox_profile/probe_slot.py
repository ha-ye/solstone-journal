# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OS-level probe slot ownership and attempt-directory identity."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from solstone.think.sandbox_profile import probe_contract as contract
from solstone.think.sandbox_profile import probe_durability, probe_records

SLOT_STATE_UNUSED = "unused"
SLOT_STATE_ACTIVE = "active"
SLOT_STATE_SPENT = "spent"
SLOT_STATE_POISONED = "poisoned"


@dataclass(slots=True)
class ProbeSlot:
    journal_path: Path
    run_id: str
    ledger_path: Path
    lock_path: Path
    attempts_parent_path: Path
    replay_run_id: str | None
    _lock_fd: int | None
    _lock_identity: tuple[int, int]
    _ledger_fd: int | None
    _ledger_identity: tuple[int, int]
    _ledger_tracked_size: int
    state: str = SLOT_STATE_UNUSED
    _poisoned_code: str | None = None
    _operation_lock: threading.RLock = field(default_factory=threading.RLock)
    _operation_active: bool = False

    @property
    def owned(self) -> bool:
        if self._lock_fd is None:
            return False
        try:
            os.fstat(self._lock_fd)
        except OSError:
            return False
        return True

    @property
    def ledger_size_bytes(self) -> int:
        return self._ledger_tracked_size

    @contextmanager
    def operation_guard(self) -> Iterator[None]:
        self._operation_lock.acquire()
        if self._operation_active:
            self._operation_lock.release()
            probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
        self._operation_active = True
        try:
            yield
        finally:
            self._operation_active = False
            self._operation_lock.release()

    def release(self) -> None:
        with self.operation_guard():
            self._release_unlocked()

    def __enter__(self) -> ProbeSlot:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def assert_owned_unlocked(self) -> None:
        if not self.owned:
            probe_records.raise_probe_error(contract.STABLE_ERROR_PROBE_ACTIVE)

    def assert_active_writer_unlocked(self, attempt_id: str) -> None:
        self._raise_if_poisoned_unlocked(attempt_id=attempt_id)
        if self.state != SLOT_STATE_ACTIVE:
            probe_records.raise_probe_error(
                contract.STABLE_ERROR_INTERNAL_ERROR, attempt_id=attempt_id
            )

    def revalidate_identities_unlocked(self) -> None:
        self.assert_owned_unlocked()
        self._revalidate_lock_identity_unlocked()
        self._revalidate_ledger_identity_unlocked()

    def check_ledger_capacity_unlocked(
        self,
        data: bytes,
        *,
        poison_on_failure: bool,
    ) -> None:
        if self._ledger_tracked_size + len(data) <= contract.MAX_LEDGER_BYTES:
            return
        if poison_on_failure:
            self._poison_unlocked(contract.STABLE_ERROR_ATTEMPT_LIMIT_REACHED)
        probe_records.raise_probe_error(contract.STABLE_ERROR_ATTEMPT_LIMIT_REACHED)

    def append_encoded_record_unlocked(
        self,
        data: bytes,
        *,
        record_type: str,
        attempt_id: str,
        capacity_checked: bool = False,
        poison_on_overflow: bool = True,
    ) -> None:
        self.revalidate_identities_unlocked()
        if not capacity_checked:
            self.check_ledger_capacity_unlocked(
                data, poison_on_failure=poison_on_overflow
            )
        fd = self._ledger_fd
        if fd is None:
            probe_records.raise_probe_error(
                contract.STABLE_ERROR_PROBE_ACTIVE,
                attempt_id=attempt_id,
                record_type=record_type,
            )
        try:
            probe_durability.append_jsonl_strict(
                fd,
                self.ledger_path.parent,
                data,
            )
        except probe_records.ProbeOperationError as exc:
            self._poison_unlocked(exc.code)
            probe_records.raise_probe_error(
                exc.code,
                attempt_id=attempt_id,
                record_type=record_type,
            )
        self._ledger_tracked_size += len(data)

    def mark_spent_unlocked(self) -> None:
        if self.state != SLOT_STATE_POISONED:
            self.state = SLOT_STATE_SPENT

    def _poison_unlocked(self, code: str) -> None:
        if code not in contract.STABLE_ERRORS:
            probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)
        if self._poisoned_code is None:
            self._poisoned_code = code
        self.state = SLOT_STATE_POISONED

    def _raise_if_poisoned_unlocked(self, *, attempt_id: str | None = None) -> None:
        if self._poisoned_code is not None:
            probe_records.raise_probe_error(self._poisoned_code, attempt_id=attempt_id)

    def _release_unlocked(self) -> None:
        self.mark_spent_unlocked()
        ledger_fd = self._ledger_fd
        lock_fd = self._lock_fd
        self._ledger_fd = None
        self._lock_fd = None
        for fd in (ledger_fd, lock_fd):
            if fd is None:
                continue
            try:
                os.close(fd)
            except OSError:
                pass

    def _revalidate_lock_identity_unlocked(self) -> None:
        try:
            current = self.lock_path.lstat()
        except OSError:
            self._poison_unlocked(contract.STABLE_ERROR_STALE_ATTEMPT)
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != self._lock_identity
        ):
            self._poison_unlocked(contract.STABLE_ERROR_STALE_ATTEMPT)
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)

    def _revalidate_ledger_identity_unlocked(self) -> None:
        try:
            current = self.ledger_path.lstat()
        except OSError:
            self._poison_unlocked(contract.STABLE_ERROR_STALE_ATTEMPT)
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != self._ledger_identity
            or current.st_size != self._ledger_tracked_size
        ):
            self._poison_unlocked(contract.STABLE_ERROR_STALE_ATTEMPT)
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)


def acquire_probe_slot(journal_path: Path, *, run_id: str) -> ProbeSlot:
    journal = Path(journal_path)
    try:
        run_id = probe_records.validate_canonical_uuid(run_id)
    except probe_records.ProbeRecordValidationError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)

    lock_fd: int | None = None
    ledger_fd: int | None = None
    try:
        lock_path = contract.probe_lock_path(journal)
        lock_fd, lock_identity = _open_lock_fd(lock_path)

        from solstone.think.sandbox_profile.probe_replay import replay_probe_ledger

        replay = replay_probe_ledger(journal)
        if replay.run_id is not None and replay.run_id != run_id:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)

        ledger_path = contract.probe_ledger_path(journal)
        ledger_fd, ledger_identity, tracked_size = _open_ledger_fd(
            ledger_path,
            replay_identity=replay.ledger_identity,
            replay_size=replay.ledger_size_bytes,
        )
        return ProbeSlot(
            journal_path=journal,
            run_id=run_id,
            ledger_path=ledger_path,
            lock_path=lock_path,
            attempts_parent_path=contract.probe_attempts_parent_path(journal),
            replay_run_id=replay.run_id,
            _lock_fd=lock_fd,
            _lock_identity=lock_identity,
            _ledger_fd=ledger_fd,
            _ledger_identity=ledger_identity,
            _ledger_tracked_size=tracked_size,
        )
    except probe_records.ProbeOperationError:
        _close_fd_quietly(ledger_fd)
        _close_fd_quietly(lock_fd)
        raise
    except OSError:
        _close_fd_quietly(ledger_fd)
        _close_fd_quietly(lock_fd)
        probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)


def create_attempt_directory_unlocked(slot: ProbeSlot, attempt_id: str) -> Path:
    attempt_id = _validate_attempt_id_for_error(attempt_id)
    slot.revalidate_identities_unlocked()
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
        slot._poison_unlocked(contract.STABLE_ERROR_STALE_ATTEMPT)
        probe_records.raise_probe_error(
            contract.STABLE_ERROR_STALE_ATTEMPT, attempt_id=attempt_id
        )
    except OSError:
        slot._poison_unlocked(contract.STABLE_ERROR_RECORD_WRITE_FAILED)
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


def _open_lock_fd(path: Path) -> tuple[int, tuple[int, int]]:
    probe_durability._mkdir(path.parent, mode=contract.ATTEMPT_DIR_MODE, parents=True)
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        path_stat = None
    except OSError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
    if path_stat is not None and (
        stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode)
    ):
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)

    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        fd_stat = os.fstat(fd)
        if not stat.S_ISREG(fd_stat.st_mode):
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                probe_records.raise_probe_error(contract.STABLE_ERROR_PROBE_ACTIVE)
            raise
        return fd, (fd_stat.st_dev, fd_stat.st_ino)
    except probe_records.ProbeOperationError:
        _close_fd_quietly(fd)
        raise
    except OSError as exc:
        _close_fd_quietly(fd)
        if exc.errno == errno.ELOOP:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        raise


def _open_ledger_fd(
    path: Path,
    *,
    replay_identity: tuple[int, int] | None,
    replay_size: int,
) -> tuple[int, tuple[int, int], int]:
    probe_durability._mkdir(path.parent, mode=contract.ATTEMPT_DIR_MODE, parents=True)
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        path_stat = None
    except OSError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
    if path_stat is not None and (
        stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode)
    ):
        probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)

    fd: int | None = None
    try:
        fd = probe_durability._open_append(path)
        fd_stat = os.fstat(fd)
        if not stat.S_ISREG(fd_stat.st_mode):
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        identity = (fd_stat.st_dev, fd_stat.st_ino)
        if replay_identity is not None:
            if identity != replay_identity or fd_stat.st_size != replay_size:
                probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        elif fd_stat.st_size != 0:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        return fd, identity, fd_stat.st_size
    except probe_records.ProbeOperationError:
        _close_fd_quietly(fd)
        raise
    except OSError as exc:
        _close_fd_quietly(fd)
        if exc.errno == errno.ELOOP:
            probe_records.raise_probe_error(contract.STABLE_ERROR_STALE_ATTEMPT)
        raise


def _validate_attempt_id_for_error(value: str) -> str:
    try:
        return probe_records.validate_canonical_uuid(value)
    except probe_records.ProbeRecordValidationError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_INTERNAL_ERROR)


def _close_fd_quietly(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
