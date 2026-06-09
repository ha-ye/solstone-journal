# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""NPZ journal I/O mechanics with lock, atomic replace, and reload verify."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from io import BytesIO
from pathlib import Path

import numpy as np

from solstone.think.journal_io.atomic import atomic_replace
from solstone.think.journal_io.errors import MalformedDataError
from solstone.think.journal_io.locking import hold_lock


def load_npz(path: Path) -> dict[str, np.ndarray] | None:
    """Load an NPZ file as a materialized key-to-array map."""
    if not path.exists():
        return None

    with np.load(path, allow_pickle=False) as data:
        return {key: data[key] for key in data.files}


def save_npz(
    path: Path,
    arrays: Mapping[str, np.ndarray],
    *,
    expected_keys: tuple[str, ...],
) -> None:
    """Atomically overwrite an NPZ file and verify the expected keys reload."""
    with hold_lock(path):
        _write_npz(path, arrays, expected_keys=expected_keys)


def write_npz(
    path: Path,
    arrays: Mapping[str, np.ndarray],
    *,
    expected_keys: tuple[str, ...],
) -> None:
    """Atomically overwrite an NPZ file and verify keys, WITHOUT locking.

    Unlike save_npz, this acquires no <name>.npz.lock sidecar — chosen for
    single-writer chronicle segment outputs where the cross-process lock
    protects nothing and a stray .lock file would be swept into peer-sync
    manifests and export tarballs. Same atomic-replace + reload-verify as
    save_npz; raises MalformedDataError if the written file fails to reload
    with all expected keys.
    """
    _write_npz(path, arrays, expected_keys=expected_keys)


def update_npz(
    path: Path,
    transform: Callable[[dict[str, np.ndarray]], dict[str, np.ndarray] | None],
    *,
    expected_keys: tuple[str, ...],
) -> None:
    """Apply a locked read-modify-write transform to an NPZ file."""
    with hold_lock(path):
        current = load_npz(path) or {}
        new = transform(current)
        if new is None:
            return
        if not new:
            path.unlink(missing_ok=True)
            return
        _write_npz(path, new, expected_keys=expected_keys)


def _write_npz(
    path: Path,
    arrays: Mapping[str, np.ndarray],
    *,
    expected_keys: tuple[str, ...],
) -> None:
    buf = BytesIO()
    np.savez_compressed(buf, **arrays)
    atomic_replace(path, buf.getvalue())
    _verify_npz(path, expected_keys)


def _verify_npz(path: Path, expected_keys: tuple[str, ...]) -> None:
    try:
        loaded = load_npz(path)
    except Exception as exc:
        raise MalformedDataError(path) from exc
    if loaded is None or any(key not in loaded for key in expected_keys):
        raise MalformedDataError(path)
