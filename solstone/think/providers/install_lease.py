# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Nonblocking provider install leases."""

from __future__ import annotations

import errno
import fcntl
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from solstone.think.providers.install_state import PROVIDERS, ProviderName
from solstone.think.utils import get_journal

LEASE_MODE = 0o600
LEASE_ATTEMPTS = 5
LEASE_RETRY_MAX_SECONDS = 0.25


@dataclass
class InstallLease:
    """Owned provider install lease backed by a held flock."""

    provider: ProviderName
    path: Path
    _fd: int | None

    @property
    def owned(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> InstallLease:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


def lease_path(
    provider: str,
    *,
    journal_path: str | Path | None = None,
) -> Path:
    validated = _validate_provider(provider)
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return root / "health" / "providers" / f"{validated}.lease"


def acquire_install_lease(
    provider: str,
    *,
    journal_path: str | Path | None = None,
    attempts: int = LEASE_ATTEMPTS,
) -> InstallLease | None:
    """Acquire provider lease with bounded nonblocking retry."""
    validated = _validate_provider(provider)
    path = lease_path(validated, journal_path=journal_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    attempts = max(1, attempts)
    deadline = time.monotonic() + LEASE_RETRY_MAX_SECONDS
    for index in range(attempts):
        fd = os.open(path, os.O_RDWR | os.O_CREAT, LEASE_MODE)
        try:
            os.fchmod(fd, LEASE_MODE)
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
            return InstallLease(provider=validated, path=path, _fd=fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
    return None


def assert_install_lease_owned(
    lease: InstallLease | None, provider: str
) -> InstallLease:
    """Return lease only when the caller still owns its held flock handle."""
    validated = _validate_provider(provider)
    if lease is None or lease.provider != validated or not lease.owned:
        raise RuntimeError(f"{validated} install lease is not owned")
    assert lease._fd is not None
    try:
        os.fstat(lease._fd)
    except OSError as exc:
        raise RuntimeError(f"{validated} install lease handle is not valid") from exc
    return lease


def probe_install_lease_state(
    provider: str,
    *,
    journal_path: str | Path | None = None,
) -> Literal["held", "free", "missing"]:
    """Probe an existing provider lease with a trial flock.

    The only sanctioned side effect is a transient flock on an existing lease
    file; this function never creates the lease path.
    """
    validated = _validate_provider(provider)
    path = lease_path(validated, journal_path=journal_path)
    try:
        fd = os.open(path, os.O_RDONLY)
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return "held"
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "free"
    finally:
        os.close(fd)


def probe_install_lease_free(
    provider: str,
    *,
    journal_path: str | Path | None = None,
) -> bool:
    """Return whether a reader can trial-flock the provider lease."""
    lease = acquire_install_lease(provider, journal_path=journal_path, attempts=1)
    if lease is None:
        return False
    lease.release()
    return True


def prune_unowned_lease_file(
    provider: str,
    *,
    journal_path: str | Path | None = None,
) -> bool:
    """Remove an unowned lease file; never infer ownership from file existence."""
    lease = acquire_install_lease(provider, journal_path=journal_path, attempts=1)
    if lease is None:
        return False
    path = lease.path
    try:
        lease.release()
        path.unlink(missing_ok=True)
    finally:
        lease.release()
    return True


def _retry_sleep_seconds(deadline: float) -> float:
    remaining = max(0.0, deadline - time.monotonic())
    return min(remaining, random.uniform(0.02, 0.06))


def _validate_provider(value: object) -> ProviderName:
    if value not in PROVIDERS:
        raise ValueError(f"provider install lease must be one of: {sorted(PROVIDERS)}")
    return value  # type: ignore[return-value]


__all__ = [
    "InstallLease",
    "LEASE_ATTEMPTS",
    "LEASE_MODE",
    "acquire_install_lease",
    "assert_install_lease_owned",
    "lease_path",
    "probe_install_lease_free",
    "probe_install_lease_state",
    "prune_unowned_lease_file",
]
