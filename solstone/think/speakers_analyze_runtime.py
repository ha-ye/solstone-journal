# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Cross-process runtime state for native speaker analysis attempts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from solstone.think.journal_io import (
    MalformedPolicy,
    atomic_replace,
    hold_lock,
    read_json,
)
from solstone.think.utils import get_journal

SCHEMA_VERSION = 1
BREAKER_THRESHOLD = 3
STATE_MODE = 0o600


class SpeakersAnalyzeBreakerRecord(TypedDict):
    schema_version: int
    consecutive_failures: int
    opened_at: str | None
    last_failure_at: str | None
    last_failure_stage: str | None
    last_failure_reason: str | None
    last_native_exit_code: int | None
    last_success_at: str | None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def circuit_breaker_path(*, journal_path: str | Path | None = None) -> Path:
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return root / "health" / "speakers-analyze" / "circuit-breaker.json"


def empty_record() -> SpeakersAnalyzeBreakerRecord:
    return {
        "schema_version": SCHEMA_VERSION,
        "consecutive_failures": 0,
        "opened_at": None,
        "last_failure_at": None,
        "last_failure_stage": None,
        "last_failure_reason": None,
        "last_native_exit_code": None,
        "last_success_at": None,
    }


def _coerce_record(raw: object) -> SpeakersAnalyzeBreakerRecord:
    if not isinstance(raw, dict):
        return empty_record()
    return {
        "schema_version": SCHEMA_VERSION,
        "consecutive_failures": max(0, int(raw.get("consecutive_failures") or 0)),
        "opened_at": _optional_string(raw.get("opened_at")),
        "last_failure_at": _optional_string(raw.get("last_failure_at")),
        "last_failure_stage": _optional_string(raw.get("last_failure_stage")),
        "last_failure_reason": _optional_string(raw.get("last_failure_reason")),
        "last_native_exit_code": _optional_int(raw.get("last_native_exit_code")),
        "last_success_at": _optional_string(raw.get("last_success_at")),
    }


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_record(
    *, journal_path: str | Path | None = None
) -> SpeakersAnalyzeBreakerRecord:
    path = circuit_breaker_path(journal_path=journal_path)
    raw = read_json(path, on_error=MalformedPolicy.WARN_AND_SKIP, default={})
    return _coerce_record(raw)


def native_blocked(
    *, journal_path: str | Path | None = None
) -> tuple[bool, SpeakersAnalyzeBreakerRecord]:
    path = circuit_breaker_path(journal_path=journal_path)
    with hold_lock(path, mode=STATE_MODE):
        record = read_record(journal_path=journal_path)
        return record["consecutive_failures"] >= BREAKER_THRESHOLD, record


def record_native_success(
    *, journal_path: str | Path | None = None
) -> SpeakersAnalyzeBreakerRecord:
    path = circuit_breaker_path(journal_path=journal_path)
    with hold_lock(path, mode=STATE_MODE):
        record = empty_record()
        record["last_success_at"] = now_iso()
        _write_record(path, record)
        return record


def record_native_failure(
    *,
    stage: str,
    reason: str,
    native_exit_code: int | None = None,
    journal_path: str | Path | None = None,
) -> SpeakersAnalyzeBreakerRecord:
    path = circuit_breaker_path(journal_path=journal_path)
    with hold_lock(path, mode=STATE_MODE):
        current = read_record(journal_path=journal_path)
        consecutive = current["consecutive_failures"] + 1
        opened_at = current["opened_at"]
        if consecutive >= BREAKER_THRESHOLD and opened_at is None:
            opened_at = now_iso()
        record: SpeakersAnalyzeBreakerRecord = {
            "schema_version": SCHEMA_VERSION,
            "consecutive_failures": consecutive,
            "opened_at": opened_at,
            "last_failure_at": now_iso(),
            "last_failure_stage": stage,
            "last_failure_reason": reason,
            "last_native_exit_code": native_exit_code,
            "last_success_at": current["last_success_at"],
        }
        _write_record(path, record)
        return record


def _write_record(path: Path, record: SpeakersAnalyzeBreakerRecord) -> None:
    atomic_replace(
        path,
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        mode=STATE_MODE,
    )


__all__ = [
    "BREAKER_THRESHOLD",
    "SpeakersAnalyzeBreakerRecord",
    "circuit_breaker_path",
    "empty_record",
    "native_blocked",
    "read_record",
    "record_native_failure",
    "record_native_success",
]
