# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Strict durability primitives for sandbox probe coordination."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

from solstone.think.sandbox_profile import probe_contract as contract
from solstone.think.sandbox_profile import probe_records


def append_jsonl_strict(path: Path, record: dict[str, object]) -> None:
    data = _encode_jsonl(record)
    try:
        _mkdir(path.parent, mode=contract.ATTEMPT_DIR_MODE, parents=True)
        with _open_append(path) as handle:
            written = _write_once(handle, data)
            if written != len(data):
                probe_records.raise_probe_error(
                    contract.STABLE_ERROR_RECORD_WRITE_FAILED
                )
            _flush_file(handle)
            _fsync_file(handle)
        _fsync_directory(path.parent)
    except probe_records.ProbeOperationError:
        raise
    except OSError:
        probe_records.raise_probe_error(contract.STABLE_ERROR_RECORD_WRITE_FAILED)


def _encode_jsonl(record: dict[str, object]) -> bytes:
    return (
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _mkdir(path: Path, *, mode: int, parents: bool = False) -> None:
    path.mkdir(mode=mode, parents=parents, exist_ok=parents)


def _open_append(path: Path) -> io.BufferedWriter:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "ab")
    except BaseException:
        os.close(fd)
        raise


def _write_once(handle: io.BufferedWriter, data: bytes) -> int:
    return handle.write(data)


def _flush_file(handle: io.BufferedWriter) -> None:
    handle.flush()


def _fsync_file(handle: io.BufferedWriter) -> None:
    os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
