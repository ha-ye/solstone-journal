# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared journal configuration file helpers."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from solstone.think.journal_io.atomic import atomic_replace
from solstone.think.journal_io.locking import hold_lock
from solstone.think.utils import get_config, get_journal


def get_journal_config_path() -> Path:
    """Return the canonical journal config path."""

    return Path(get_journal()) / "config" / "journal.json"


def read_journal_config() -> dict[str, Any]:
    """Read journal config through the canonical config resolver."""

    return get_config()


def write_journal_config(config: dict[str, Any]) -> None:
    """Write journal config atomically with stable formatting and private permissions."""

    config_path = get_journal_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace(
        config_path,
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        mode=0o600,
    )


@contextmanager
def hold_config_lock() -> Iterator[None]:
    """Hold the journal config read-modify-write lock."""

    with hold_lock(get_journal_config_path()):
        yield


__all__ = [
    "get_journal_config_path",
    "hold_config_lock",
    "read_journal_config",
    "write_journal_config",
]
