# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared `_solstone_processing` record vocabulary for media-analysis handlers.

The screen (`describe`) and audio (`transcribe`) handlers stamp one of these
records into the metadata header (row 1) of the JSONL they already produce, so
a downstream reader lode can derive per-segment processing state without
re-deriving it from raw media. This module is the one authoritative source of
the closed state / reason_code / handler / schema vocabulary; neither handler
may carry these literals inline. Failed describe records may also carry an
``attempts`` counter; absent attempts means 0, and
``FAILED_ATTEMPT_BOUND`` is the shared retry exhaustion bound.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "solstone.processing.v1"
FAILED_ATTEMPT_BOUND = 3
ATTEMPTS_KEY = "attempts"
MAX_FIRST_ROW_BYTES = 64 * 1024

# state values (closed set)
STATE_ANALYZED = "analyzed"
STATE_EMPTY = "empty"
STATE_FAILED = "failed"

# reason_code values (closed set). Audio emits no_decodable_audio for terminal
# preserved-empty outputs.
REASON_OK = "ok"
REASON_NO_DECODABLE_FRAMES = "no_decodable_frames"
REASON_NO_DECODABLE_AUDIO = "no_decodable_audio"
REASON_CORRUPT_INPUT = "corrupt_input"
REASON_ANALYSIS_FAILED = "analysis_failed"

# handler values (closed set)
HANDLER_DESCRIBE = "describe"
HANDLER_TRANSCRIBE = "transcribe"


def is_failure_exhausted(record: dict | None) -> bool:
    """Return whether a failed processing record has reached terminal exhaustion."""
    if not isinstance(record, dict) or record.get("state") != STATE_FAILED:
        return False
    if record.get("reason_code") == REASON_CORRUPT_INPUT:
        return True
    return record_attempts(record) >= FAILED_ATTEMPT_BOUND


def record_attempts(record: dict | None) -> int:
    """Return a record's failure-attempt count; absent or malformed means 0."""
    if not isinstance(record, dict):
        return 0
    attempts = record.get(ATTEMPTS_KEY, 0)
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        return 0
    return attempts


def read_processing_record_header(path: Path) -> dict | None:
    """Read a JSONL header's `_solstone_processing` record within one bounded window."""
    try:
        with path.open("rb") as handle:
            first_window = handle.read(MAX_FIRST_ROW_BYTES)
    except OSError:
        return None

    if b"\n" not in first_window:
        return None

    first_line = first_window.split(b"\n", 1)[0]
    try:
        row = json.loads(first_line.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    except json.JSONDecodeError:
        return None

    if not isinstance(row, dict):
        return None
    record = row.get("_solstone_processing")
    return record if isinstance(record, dict) else None


def should_reenter_failed_describe(record: dict | None) -> bool:
    """Return whether an existing failed describe output should be retried."""
    return (
        isinstance(record, dict)
        and record.get("state") == STATE_FAILED
        and record.get("handler") == HANDLER_DESCRIBE
        and not is_failure_exhausted(record)
    )


def now_iso_utc() -> str:
    """ISO-8601 UTC timestamp with a trailing ``Z`` (e.g. ``2026-06-30T12:00:00Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_processing_record(
    *,
    state: str,
    reason_code: str,
    handler: str,
    input_size: int,
    attempted_at: str | None = None,
    source: str | None = None,
    attempts: int | None = None,
) -> dict:
    """Build a `_solstone_processing` header record for a determined outcome.

    `attempted_at` defaults to the current UTC instant; pass an explicit value
    only in tests. The outcome must be the one the handler *determined* while
    running — never a pre-stamped guess. `source` is a provenance tag set only
    by the backfill. `attempts` is present only on failing records; absent means
    0, and callers never stamp `attempts: 0`.
    """
    record = {
        "schema": SCHEMA,
        "state": state,
        "reason_code": reason_code,
        "handler": handler,
        "attempted_at": attempted_at or now_iso_utc(),
        "input_size": input_size,
    }
    if source is not None:
        record["source"] = source
    if attempts is not None:
        record[ATTEMPTS_KEY] = attempts
    return record
