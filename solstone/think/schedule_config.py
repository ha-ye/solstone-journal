# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Write-owner for journal config/schedules.json."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from solstone.think.journal_io.atomic import atomic_replace
from solstone.think.journal_io.errors import MalformedDataError
from solstone.think.journal_io.locking import hold_lock
from solstone.think.journal_io.readers import MalformedPolicy, read_json
from solstone.think.utils import get_journal

RESERVED_METADATA_KEYS: frozenset[str] = frozenset(
    {"daily_time", "weekly_day", "weekly_time"}
)


def get_schedules_path() -> Path:
    """Return the canonical schedules config path."""
    return Path(get_journal()) / "config" / "schedules.json"


def read_schedules() -> dict[str, Any]:
    """Strictly read the raw schedules config object."""
    path = get_schedules_path()
    raw = read_json(path, on_error=MalformedPolicy.RAISE, default={})
    if not isinstance(raw, dict):
        raise MalformedDataError(path)
    return raw


def set_schedule_metadata(updates: dict[str, Any]) -> None:
    """Set reserved schedule metadata keys in one locked read-modify-write."""
    unknown = set(updates) - RESERVED_METADATA_KEYS
    if unknown:
        raise ValueError(f"unknown schedule metadata keys: {sorted(unknown)}")

    def mutator(raw: dict[str, Any]) -> bool:
        raw.update(updates)
        return True

    _mutate(mutator)


def set_schedule_entries(entries: dict[str, dict[str, Any]]) -> None:
    """Set one or more named schedule entries in one locked read-modify-write."""
    collisions = RESERVED_METADATA_KEYS.intersection(entries)
    if collisions:
        raise ValueError(
            f"schedule entries collide with metadata keys: {sorted(collisions)}"
        )

    def mutator(raw: dict[str, Any]) -> bool:
        for name, entry in entries.items():
            raw[name] = entry
        return True

    _mutate(mutator)


def remove_schedule_entry(name: str) -> None:
    """Remove a named schedule entry if present."""
    if name in RESERVED_METADATA_KEYS:
        raise ValueError(f"cannot remove reserved schedule metadata key: {name}")

    def mutator(raw: dict[str, Any]) -> bool:
        if name not in raw:
            return False
        del raw[name]
        return True

    _mutate(mutator)


def _mutate(mutator: Callable[[dict[str, Any]], bool]) -> None:
    """Apply a schedules-specific locked mutation and atomically persist changes."""
    path = get_schedules_path()
    with hold_lock(path):
        raw = read_json(path, on_error=MalformedPolicy.RAISE, default={})
        if not isinstance(raw, dict):
            raise MalformedDataError(path)
        if mutator(raw):
            atomic_replace(path, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
