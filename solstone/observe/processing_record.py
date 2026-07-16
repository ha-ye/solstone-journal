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

from datetime import datetime, timezone

SCHEMA = "solstone.processing.v1"
FAILED_ATTEMPT_BOUND = 3
ATTEMPTS_KEY = "attempts"

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
    attempts = record.get(ATTEMPTS_KEY, 0)
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        attempts = 0
    return attempts >= FAILED_ATTEMPT_BOUND


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
) -> dict:
    """Build a `_solstone_processing` header record for a determined outcome.

    `attempted_at` defaults to the current UTC instant; pass an explicit value
    only in tests. The outcome must be the one the handler *determined* while
    running — never a pre-stamped guess. `source` is a provenance tag set only
    by the backfill.
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
    return record
