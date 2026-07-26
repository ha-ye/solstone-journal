# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Strict durability primitives for sandbox probe coordination.

Probe records are appended through a raw file descriptor so the short-write
check observes the actual ``os.write`` result. There is no separate flush step:
with no user-space buffer between ``os.write`` and ``os.fsync``, durability is
the fd fsync followed by the parent-directory fsync.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from solstone.think.sandbox_profile import probe_contract as contract
from solstone.think.sandbox_profile import probe_records


def append_jsonl_strict(path: Path, record: dict[str, object]) -> None:
    data = _encode_jsonl(record)
    fd: int | None = None
    try:
        _mkdir(path.parent, mode=contract.ATTEMPT_DIR_MODE, parents=True)
        fd = _open_append(path)
        written = _write_once(fd, data)
        if written != len(data):
            probe_records.raise_probe_error(contract.STABLE_ERROR_RECORD_WRITE_FAILED)
        _fsync_file(fd)
        os.close(fd)
        fd = None
        _fsync_directory(path.parent)
    except probe_records.ProbeOperationError:
        raise
    except OSError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_RECORD_WRITE_FAILED)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _encode_jsonl(record: dict[str, object]) -> bytes:
    return (
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _mkdir(path: Path, *, mode: int, parents: bool = False) -> None:
    path.mkdir(mode=mode, parents=parents, exist_ok=parents)


def _open_append(path: Path) -> int:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_once(fd: int, data: bytes) -> int:
    return os.write(fd, data)


def _fsync_file(fd: int) -> None:
    os.fsync(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
