# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Day-level JSONL accumulator for searchable talent outputs."""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from solstone.think.indexer.journal import index_file
from solstone.think.journal_io import MalformedPolicy, read_jsonl, write_jsonl
from solstone.think.utils import day_path, get_journal, now_ms

logger = logging.getLogger(__name__)


def _jsonl_path(day: str, name: str, *, create: bool) -> Path:
    return day_path(day, create=create) / "talents" / f"{name}.jsonl"


def _ts(record: dict) -> int | float:
    value = record.get("ts")
    return value if isinstance(value, (int, float)) else 0


def _records_for_day(day: str, name: str) -> list[dict]:
    return read_jsonl(
        _jsonl_path(day, name, create=False), on_error=MalformedPolicy.SKIP
    )


def append_record(day: str, name: str, record: dict) -> None:
    """Append one record to chronicle/<day>/talents/<name>.jsonl and index it.

    Stamps ts via setdefault (caller-supplied ts preserved). Reads all existing
    valid records (malformed/blank lines are dropped on rewrite - they are not
    records), appends, and rewrites the whole file atomically via write_jsonl.
    Then indexes the file so it is searchable under agent=<name>.
    """
    record.setdefault("ts", now_ms())
    path = _jsonl_path(day, name, create=True)
    records = read_jsonl(path, on_error=MalformedPolicy.SKIP)
    records.append(record)
    write_jsonl(path, records)

    try:
        index_file(get_journal(), str(path))
    except Exception:
        logger.warning(
            "day-accumulator-index-failed name=%s day=%s path=%s",
            name,
            day,
            path,
            exc_info=True,
        )


def read_latest(day: str, name: str, *, lookback_days: int = 7) -> dict | None:
    """Newest record for `day`, probing prior days when no records exist.

    Highest ts wins; last file-position wins on ties or when no record carries a
    ts. Returns None when the lookback window has no records. Never raises on
    malformed data.
    """
    base = datetime.strptime(day, "%Y%m%d")
    for offset in range(lookback_days + 1):
        probe = (base - timedelta(days=offset)).strftime("%Y%m%d")
        records = _records_for_day(probe, name)
        if records:
            return max(enumerate(records), key=lambda pair: (_ts(pair[1]), pair[0]))[1]
    return None


def read_records(day: str, name: str) -> list[dict]:
    """All valid records for `day` in ascending stable ts order."""
    return sorted(_records_for_day(day, name), key=_ts)


def format_day_accumulator(
    content: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Format accumulator JSONL records into indexable chunks.

    One formatter serves every accumulator name, so records are arbitrary dicts:
    render each as a json dump so all key/string content reaches FTS. The agent
    name is the file basename (e.g. "pulse"), supplied to the indexer here
    because extract_path_metadata only derives agent for .md files.
    """
    ctx = context or {}
    name = Path(ctx["file_path"]).stem
    chunks: list[dict[str, Any]] = []
    for record in content:
        markdown = json.dumps(record, ensure_ascii=False)
        if not markdown:
            continue
        chunks.append(
            {"timestamp": _ts(record), "markdown": markdown, "source": record}
        )
    return chunks, {"indexer": {"agent": name}}
