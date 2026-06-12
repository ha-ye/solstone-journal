# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Restic repository initialization for sol private backup."""

from __future__ import annotations

import os
from pathlib import Path

from solstone.think.backup.destination import (
    Destination,
    assemble_backend_env,
    validate_destination,
)
from solstone.think.backup.runner import run_restic


def init_repository(
    destination: Destination,
    *,
    daily_key: str,
    recovery_key: str,
    restic_path: Path,
    timeout: float | None = None,
) -> None:
    status = validate_destination(
        destination,
        daily_key,
        restic_path=restic_path,
        timeout=timeout,
    )
    if status.reason_code == "repo_missing":
        result = run_restic(
            ["init"],
            repository=destination.repository,
            password=daily_key,
            restic_path=restic_path,
            backend_env=assemble_backend_env(destination),
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"restic init failed with returncode {result.returncode}"
            )
        _add_recovery_key(
            destination,
            daily_key=daily_key,
            recovery_key=recovery_key,
            restic_path=restic_path,
            timeout=timeout,
        )
        _verify_recovery_key(
            destination,
            recovery_key=recovery_key,
            restic_path=restic_path,
            timeout=timeout,
        )
        return

    if status.reason_code == "repo_exists":
        recovery_status = validate_destination(
            destination,
            recovery_key,
            restic_path=restic_path,
            timeout=timeout,
        )
        if recovery_status.repo_exists and recovery_status.reason_code == "repo_exists":
            return
        if recovery_status.reason_code == "auth_failed":
            _add_recovery_key(
                destination,
                daily_key=daily_key,
                recovery_key=recovery_key,
                restic_path=restic_path,
                timeout=timeout,
            )
            _verify_recovery_key(
                destination,
                recovery_key=recovery_key,
                restic_path=restic_path,
                timeout=timeout,
            )
            return
        raise RuntimeError(recovery_status.message)

    if status.reason_code == "auth_failed":
        raise RuntimeError(
            "backup repository exists but the configured daily key does not unlock it"
        )

    raise RuntimeError(status.message)


def _add_recovery_key(
    destination: Destination,
    *,
    daily_key: str,
    recovery_key: str,
    restic_path: Path,
    timeout: float | None = None,
) -> None:
    read_fd = -1
    write_fd = -1
    try:
        read_fd, write_fd = os.pipe()
        payload = (recovery_key + "\n").encode()
        os.write(write_fd, payload)
        os.close(write_fd)
        write_fd = -1
        result = run_restic(
            ["key", "add", "--new-password-file", f"/dev/fd/{read_fd}"],
            repository=destination.repository,
            password=daily_key,
            restic_path=restic_path,
            backend_env=assemble_backend_env(destination),
            timeout=timeout,
            pass_fds=(read_fd,),
        )
    finally:
        for fd in (write_fd, read_fd):
            if fd == -1:
                continue
            try:
                os.close(fd)
            except OSError:
                pass

    if result.returncode != 0:
        raise RuntimeError(f"restic key add failed with returncode {result.returncode}")


def _verify_recovery_key(
    destination: Destination,
    *,
    recovery_key: str,
    restic_path: Path,
    timeout: float | None = None,
) -> None:
    status = validate_destination(
        destination,
        recovery_key,
        restic_path=restic_path,
        timeout=timeout,
    )
    if not (status.repo_exists and status.reason_code == "repo_exists"):
        raise RuntimeError("recovery key did not unlock the repository after key add")


__all__ = [
    "init_repository",
]
