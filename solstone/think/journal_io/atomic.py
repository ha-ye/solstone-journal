# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Atomic replace writers for caller-owned absolute journal paths.

These helpers take absolute Paths that the caller already owns. They do not
offer a journal-relative write-anywhere convenience. atomic_replace() writes a
same-directory temporary file, flushes and fsyncs it, applies a requested mode
before close, replaces the target with os.replace(), and then always attempts a
best-effort parent-directory fsync. install_file() promotes a caller-prepared
same-directory temp onto a destination without reading its content.
"""

import json
import logging
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def atomic_replace(path: Path, data: str | bytes, *, mode: int | None = None) -> None:
    """Atomically replace path with data using a durable same-directory temp.

    Contract: create the parent directory, write str data as UTF-8 or bytes as
    provided, flush and fsync the temp file, fchmod before close when mode is
    supplied, close the temp, os.replace() it over the target, then call
    _fsync_dir(path.parent). On any exception before replace completion, unlink
    the temporary file and re-raise.
    """
    payload = data.encode("utf-8") if isinstance(data, str) else data
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
            if mode is not None:
                os.fchmod(f.fileno(), mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def install_file(temp_path: Path, dest: Path, *, mode: int | None = None) -> None:
    """Durably install an ALREADY-WRITTEN temp file onto dest.

    Contract: the caller streams content into temp_path and closes it; this
    primitive promotes it. It never reads the file content into memory.
    temp_path must live in dest.parent (same filesystem) so os.replace() is
    atomic, matching atomic_replace()'s same-directory temp contract. Create
    dest.parent, fsync the prepared temp by opening temp_path read-only, chmod
    temp_path before replacement when mode is supplied, os.replace() it over
    dest, then call _fsync_dir(dest.parent). On any exception during the
    fsync/chmod/replace phase, unlink temp_path and re-raise. A failure
    mid-install leaves any prior dest intact and cleans temp_path. The final
    parent-directory fsync stays outside that cleanup block, matching
    atomic_replace()'s degraded-durability policy.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(temp_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, dest)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    _fsync_dir(dest.parent)


def write_json(
    path: Path,
    obj: Any,
    *,
    mode: int | None = None,
    indent: int | None = 2,
    sort_keys: bool = False,
) -> None:
    """Serialize obj as JSON plus trailing newline and atomically replace path."""
    atomic_replace(
        path,
        json.dumps(obj, indent=indent, sort_keys=sort_keys) + "\n",
        mode=mode,
    )


def write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write text through atomic_replace() without path resolution side effects."""
    atomic_replace(path, text, mode=mode)


def write_jsonl(
    path: Path,
    records: Iterable[Any],
    *,
    mode: int | None = None,
) -> None:
    """Serialize records as JSONL plus trailing newline and atomically replace path."""
    body = "".join(json.dumps(record) + "\n" for record in records)
    atomic_replace(path, body, mode=mode)


def _fsync_dir(dirpath: Path) -> None:
    """Best-effort fsync for a parent directory after an entry update.

    Contract: open dirpath with os.O_RDONLY | os.O_DIRECTORY, fsync it, and
    close the descriptor. OSError is degraded durability, not write failure:
    log warning("parent-dir fsync degraded for %s: %s", dirpath, exc) with this
    module's logger and return.
    """
    try:
        fd = os.open(dirpath, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "parent-dir fsync degraded for %s: %s", dirpath, exc
        )
