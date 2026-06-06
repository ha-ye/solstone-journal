# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Stable sidecar file locks for cross-process journal read-modify-write."""

import errno
import fcntl
import random
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from solstone.think.journal_io.errors import LockTimeout

DEFAULT_LOCK_TIMEOUT: float = 10.0
DEFAULT_LOCK_POLL_INTERVAL: float = 0.05
LOCK_BACKOFF_MIN: float = 0.01
LOCK_BACKOFF_MAX: float = 0.05


@contextmanager
def hold_lock(
    path: Path,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT,
    poll_interval: float = DEFAULT_LOCK_POLL_INTERVAL,
) -> Iterator[None]:
    """Hold an exclusive flock on a stable sidecar for path.

    The sidecar is path.parent / f"{path.name}.lock". The implementation must
    create the parent directory, open the sidecar, then use LOCK_EX | LOCK_NB
    with retry and random jitter until timeout. Blocking flock(LOCK_EX) is not
    acceptable because it cannot honor the typed timeout contract. On timeout,
    raise LockTimeout(path=path, timeout=timeout).

    Read-modify-write usage:
    with hold_lock(p): read_json(p); mutate in memory; write_json(p, data).
    The primitive is format-agnostic and coordinates real processes.
    """
    lock_path = path.parent / f"{path.name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    sleep_max = (
        min(poll_interval, LOCK_BACKOFF_MAX) if poll_interval > 0 else LOCK_BACKOFF_MAX
    )
    sleep_min = min(LOCK_BACKOFF_MIN, sleep_max)
    lock_file = open(lock_path, "w")
    try:
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(path=path, timeout=timeout) from exc
                time.sleep(random.uniform(sleep_min, sleep_max))
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()
