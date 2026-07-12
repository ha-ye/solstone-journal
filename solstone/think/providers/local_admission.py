# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Cross-process admission and content-free telemetry for local inference."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from solstone.think.journal_io import append_jsonl
from solstone.think.utils import get_journal

LOG = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.025


class LocalAdmissionTimeout(TimeoutError):
    """No bundled-local inference slot became available before the deadline."""

    reason_code = "local_queue_timeout"


@dataclass
class LocalPermit:
    """One flock-backed serving-capacity permit."""

    slot_index: int
    capacity: int
    queue_wait_ms: float
    _files: list[IO[str]]

    def release(self) -> None:
        if not self._files:
            return
        files = self._files
        self._files = []
        _release_files(files)

    def __enter__(self) -> LocalPermit:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    async def __aenter__(self) -> LocalPermit:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


def _admission_dir() -> Path:
    return Path(get_journal()) / "health" / "local-inference-admission"


@dataclass
class _WaitTicket:
    path: Path
    file: IO[str]


def _create_ticket(root: Path) -> _WaitTicket:
    root.mkdir(parents=True, exist_ok=True)
    identity = f"{time.monotonic_ns():020d}-{os.getpid()}-{uuid.uuid4().hex}"
    path = root / f"wait-{identity}.ticket"
    creating_path = root / f".creating-{identity}.ticket"
    ticket_file = open(creating_path, "x+", encoding="utf-8")
    fcntl.flock(ticket_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    creating_path.rename(path)
    return _WaitTicket(path=path, file=ticket_file)


def _drop_ticket(ticket: _WaitTicket) -> None:
    try:
        ticket.path.unlink(missing_ok=True)
    finally:
        try:
            fcntl.flock(ticket.file, fcntl.LOCK_UN)
        finally:
            ticket.file.close()


def _ticket_has_turn(root: Path, ticket: _WaitTicket) -> bool:
    """Return whether ticket is oldest, pruning tickets whose owners exited."""
    while True:
        waiting = sorted(root.glob("wait-*.ticket"))
        if not waiting:
            return False
        first = waiting[0]
        if first == ticket.path:
            return True
        try:
            stale_file = open(first, "a+", encoding="utf-8")
        except FileNotFoundError:
            continue
        try:
            try:
                fcntl.flock(stale_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    return False
                raise
            first.unlink(missing_ok=True)
        finally:
            stale_file.close()


def _release_files(files: list[IO[str]]) -> None:
    for lock_file in files:
        if lock_file.closed:
            continue
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        except Exception:
            LOG.warning("failed to unlock local inference slot lock", exc_info=True)
        _close_file(lock_file)


def _close_file(lock_file: IO[str]) -> None:
    try:
        lock_file.close()
    except Exception:
        LOG.warning("failed to close local inference slot lock", exc_info=True)


def _try_acquire(capacity: int, started: float, root: Path) -> LocalPermit | None:
    for slot_index in range(capacity):
        lock_file = open(root / f"slot-{slot_index}.lock", "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_file.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                continue
            raise
        return LocalPermit(
            slot_index=slot_index,
            capacity=capacity,
            queue_wait_ms=(time.monotonic() - started) * 1000.0,
            _files=[lock_file],
        )
    return None


def _try_acquire_exclusive(
    capacity: int, started: float, root: Path
) -> LocalPermit | None:
    lock_files: list[IO[str]] = []
    for slot_index in range(capacity):
        try:
            lock_file = open(root / f"slot-{slot_index}.lock", "a+", encoding="utf-8")
        except OSError:
            _release_files(lock_files)
            raise
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            try:
                _close_file(lock_file)
            finally:
                _release_files(lock_files)
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return None
            raise
        lock_files.append(lock_file)

    return LocalPermit(
        slot_index=0,
        capacity=capacity,
        queue_wait_ms=(time.monotonic() - started) * 1000.0,
        _files=lock_files,
    )


def _deadline(started: float, timeout_s: float | None) -> float | None:
    if timeout_s is None:
        return None
    return started + max(0.0, timeout_s)


def acquire_local_slot(
    capacity: int, timeout_s: float | None, *, exclusive: bool = False
) -> LocalPermit:
    """Wait synchronously for bundled-local serving capacity."""
    if capacity < 1:
        raise ValueError("local inference capacity must be at least one")
    started = time.monotonic()
    deadline = _deadline(started, timeout_s)
    root = _admission_dir()
    ticket = _create_ticket(root)
    try:
        while True:
            if _ticket_has_turn(root, ticket):
                permit = (
                    _try_acquire_exclusive(capacity, started, root)
                    if exclusive
                    else _try_acquire(capacity, started, root)
                )
                if permit is not None:
                    return permit
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                raise LocalAdmissionTimeout(
                    f"Local inference queue exceeded its {timeout_s:.3f}s deadline."
                )
            sleep_s = _POLL_INTERVAL_S
            if deadline is not None:
                sleep_s = min(sleep_s, max(0.0, deadline - now))
            time.sleep(sleep_s)
    finally:
        _drop_ticket(ticket)


async def acquire_local_slot_async(
    capacity: int, timeout_s: float | None, *, exclusive: bool = False
) -> LocalPermit:
    """Wait cancellation-safely for bundled-local serving capacity."""
    if capacity < 1:
        raise ValueError("local inference capacity must be at least one")
    started = time.monotonic()
    deadline = _deadline(started, timeout_s)
    root = _admission_dir()
    ticket = _create_ticket(root)
    try:
        while True:
            if _ticket_has_turn(root, ticket):
                permit = (
                    _try_acquire_exclusive(capacity, started, root)
                    if exclusive
                    else _try_acquire(capacity, started, root)
                )
                if permit is not None:
                    return permit
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                raise LocalAdmissionTimeout(
                    f"Local inference queue exceeded its {timeout_s:.3f}s deadline."
                )
            sleep_s = _POLL_INTERVAL_S
            if deadline is not None:
                sleep_s = min(sleep_s, max(0.0, deadline - now))
            await asyncio.sleep(sleep_s)
    finally:
        _drop_ticket(ticket)


def record_local_inference(record: dict[str, Any]) -> None:
    """Durably append one prompt/output-free local inference record."""
    try:
        path = (
            Path(get_journal())
            / "health"
            / "local-inference"
            / f"{time.strftime('%Y%m%d')}.jsonl"
        )
        append_jsonl(path, record)
    except Exception:
        LOG.warning("failed to record local inference telemetry", exc_info=True)


__all__ = [
    "LocalAdmissionTimeout",
    "LocalPermit",
    "acquire_local_slot",
    "acquire_local_slot_async",
    "record_local_inference",
]
