# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read talent run summaries from journal talent run indexes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from solstone.think.utils import get_journal


@dataclass(frozen=True)
class AgentFailure:
    use_id: str
    name: str
    ts: int
    reason_code: str | None
    provider: str | None
    model: str | None


@dataclass(frozen=True)
class AgentFailureScan:
    failures: list[AgentFailure]
    ok: bool


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _entry_ts(entry: dict[str, Any]) -> int | None:
    raw_ts = entry.get("ts", 0)
    if isinstance(raw_ts, bool):
        return None
    try:
        return int(raw_ts)
    except (TypeError, ValueError):
        return None


def _entry_execution_day(entry: dict[str, Any]) -> str | None:
    ts = _entry_ts(entry)
    if ts is None or ts <= 0:
        return None

    try:
        return datetime.fromtimestamp(ts / 1000).strftime("%Y%m%d")
    except (OSError, OverflowError, ValueError):
        return None


def _iter_index_entries(lines: list[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        entries.append(entry)
    return entries


def _read_index_entries(day_index: Path) -> tuple[list[dict[str, Any]], bool]:
    try:
        lines = day_index.read_text().splitlines()
    except (OSError, UnicodeError):
        return [], False
    return _iter_index_entries(lines), True


def _scan_failure_entries(
    entries: list[dict[str, Any]], *, ok: bool
) -> AgentFailureScan:
    """Return failures in ``entries`` not followed by a later same-name success."""

    max_success: dict[str, int] = {}
    errors: list[AgentFailure] = []

    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue

        ts = _entry_ts(entry)
        if ts is None:
            continue

        status = entry.get("status")
        if status == "completed":
            max_success[name] = max(max_success.get(name, 0), ts)
            continue
        if status != "error":
            continue

        errors.append(
            AgentFailure(
                use_id=str(entry.get("use_id") or ""),
                name=name,
                ts=ts,
                reason_code=_string_or_none(entry.get("reason_code")),
                provider=_string_or_none(entry.get("provider")),
                model=_string_or_none(entry.get("model")),
            )
        )

    failures = [
        failure for failure in errors if max_success.get(failure.name, 0) <= failure.ts
    ]
    failures.sort(key=lambda failure: failure.ts)
    return AgentFailureScan(failures, ok=ok)


def _read_journal_day_failures(day: str) -> AgentFailureScan:
    day_index = Path(get_journal()) / "talents" / f"{day}.jsonl"
    if not day_index.exists():
        return AgentFailureScan([], ok=True)

    entries, ok = _read_index_entries(day_index)
    if not ok:
        return AgentFailureScan([], ok=False)
    return _scan_failure_entries(entries, ok=True)


def _read_execution_day_failures(scan_day: str) -> AgentFailureScan:
    talents_dir = Path(get_journal()) / "talents"
    if not talents_dir.is_dir():
        return AgentFailureScan([], ok=True)

    day_indexes = sorted(talents_dir.glob("????????.jsonl"))
    if not day_indexes:
        return AgentFailureScan([], ok=True)

    ok = True
    filtered_entries: list[dict[str, Any]] = []
    for day_index in day_indexes:
        entries, index_ok = _read_index_entries(day_index)
        ok = ok and index_ok
        for entry in entries:
            if _entry_execution_day(entry) == scan_day:
                filtered_entries.append(entry)

    return _scan_failure_entries(filtered_entries, ok=ok)


def read_unresolved_agent_failures(day: str | None = None) -> AgentFailureScan:
    """Return unresolved talent failures.

    With ``day`` this reads only that journal-day index and preserves historical
    day-index timestamp handling. Without ``day`` this scans all day indexes for
    runs executed today. Entries whose ``ts`` cannot establish an execution day
    are excluded from the default scan because a run we cannot attribute to today
    must not be reported as "executed today."
    """

    if day is not None:
        return _read_journal_day_failures(day)
    return _read_execution_day_failures(datetime.now().strftime("%Y%m%d"))
