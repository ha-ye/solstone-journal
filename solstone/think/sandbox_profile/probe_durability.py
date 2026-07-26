# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Strict fd durability primitives for sandbox probe coordination.

Probe rows are encoded once by the caller, prospectively size-checked against
the retained ledger descriptor, then appended through that same descriptor. The
caller owns open-time no-follow checks, descriptor identity, and tracked byte
accounting; this module preserves the append durability order: write once,
fsync the file, then fsync the parent directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from solstone.think.sandbox_profile import probe_contract as contract
from solstone.think.sandbox_profile import probe_records


def encode_jsonl_record(record: dict[str, object]) -> bytes:
    return (
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def append_jsonl_strict(fd: int, parent_dir: Path, data: bytes) -> None:
    try:
        written = _write_once(fd, data)
        if written != len(data):
            probe_records.raise_probe_error(contract.STABLE_ERROR_RECORD_WRITE_FAILED)
        _fsync_file(fd)
        _fsync_directory(parent_dir)
    except probe_records.ProbeOperationError:
        raise
    except OSError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_RECORD_WRITE_FAILED)


def _mkdir(path: Path, *, mode: int, parents: bool = False) -> None:
    path.mkdir(mode=mode, parents=parents, exist_ok=parents)


def _open_append(path: Path) -> int:
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.fchmod(fd, 0o600)
    except OSError:
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
