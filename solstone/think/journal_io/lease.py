# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Small fd-held nonblocking file leases for journal mechanics."""

from __future__ import annotations

import errno
import fcntl
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LEASE_MODE = 0o600
LEASE_ATTEMPTS = 5
LEASE_RETRY_MAX_SECONDS = 0.25


@dataclass
class FileLease:
    """Owned file lease backed by a held flock handle."""

    path: Path
    _fd: int | None

    @property
    def owned(self) -> bool:
        if self._fd is None:
            return False
        try:
            os.fstat(self._fd)
        except OSError:
            return False
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> FileLease:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


def _retry_sleep_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0
    return min(remaining, random.uniform(0.01, LEASE_RETRY_MAX_SECONDS))


def acquire_file_lease(
    path: Path,
    *,
    attempts: int = LEASE_ATTEMPTS,
    retry_max_seconds: float = LEASE_RETRY_MAX_SECONDS,
    mode: int = LEASE_MODE,
) -> FileLease | None:
    """Acquire a file lease with bounded nonblocking retry."""

    path.parent.mkdir(parents=True, exist_ok=True)
    attempts = max(1, attempts)
    retry_max_seconds = max(0.0, retry_max_seconds)
    deadline = time.monotonic() + retry_max_seconds
    for index in range(attempts):
        fd = os.open(path, os.O_RDWR | os.O_CREAT, mode)
        try:
            os.fchmod(fd, mode)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                os.close(fd)
                if index == attempts - 1 or time.monotonic() >= deadline:
                    return None
                time.sleep(_retry_sleep_seconds(deadline))
                continue
            return FileLease(path=path, _fd=fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
    return None


def assert_file_lease_owned(
    lease: FileLease | None, path: Path | None = None
) -> FileLease:
    """Return lease only while the caller still owns its held flock handle."""

    if lease is None or not lease.owned:
        raise RuntimeError("file lease is not owned")
    if path is not None and lease.path != path:
        raise RuntimeError(f"file lease path mismatch: {lease.path} != {path}")
    assert lease._fd is not None
    try:
        os.fstat(lease._fd)
    except OSError as exc:
        raise RuntimeError("file lease handle is not valid") from exc
    return lease


def probe_file_lease_held(path: Path) -> bool:
    """Return whether an existing lease file is currently held by another process."""

    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return True
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def probe_file_lease_free(path: Path) -> bool:
    """Return whether the lease is absent or currently acquirable."""

    return not probe_file_lease_held(path)


__all__ = [
    "FileLease",
    "acquire_file_lease",
    "assert_file_lease_owned",
    "probe_file_lease_free",
    "probe_file_lease_held",
]
