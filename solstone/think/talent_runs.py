# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read talent run summaries from the journal day index."""

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


def read_unresolved_agent_failures(day: str | None = None) -> AgentFailureScan:
    """Return same-day agent failure occurrences not followed by a later success."""

    scan_day = day or datetime.now().strftime("%Y%m%d")
    day_index = Path(get_journal()) / "talents" / f"{scan_day}.jsonl"
    if not day_index.exists():
        return AgentFailureScan([], ok=True)

    try:
        lines = day_index.read_text().splitlines()
    except (OSError, UnicodeError):
        return AgentFailureScan([], ok=False)

    max_success: dict[str, int] = {}
    errors: list[AgentFailure] = []

    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue

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
    return AgentFailureScan(failures, ok=True)
