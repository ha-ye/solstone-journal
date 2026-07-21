# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Append-only speaker keep-separate assertion store.

Sole write-owner of:
  journal/speakers/keep-separate.jsonl
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solstone.think.journal_io import append_jsonl, hold_lock
from solstone.think.utils import get_journal

logger = logging.getLogger(__name__)

KEEP_SEPARATE_SCHEMA_VERSION = 1
EVENT_KINDS = {"assert_source", "source_removed"}


class KeepSeparateStoreError(RuntimeError):
    """Raised when keep-separate assertion storage cannot be trusted."""


@dataclass(frozen=True)
class KeepSeparateAssertion:
    """Folded keep-separate assertion for one entity pair."""

    assertion_id: str
    pair_key: str
    entity_id_a: str
    entity_id_b: str
    dismissed_detection_count: int
    sources: tuple[dict[str, Any], ...]
    created_at: str
    updated_at: str
    last_recorded_at: str

    @property
    def source_count(self) -> int:
        return len(self.sources)


def keep_separate_dir(*, create: bool = False) -> Path:
    """Return the speaker keep-separate directory."""
    path = Path(get_journal()) / "speakers"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def keep_separate_path(*, create: bool = False) -> Path:
    """Return the speaker keep-separate JSONL event-log path."""
    return keep_separate_dir(create=create) / "keep-separate.jsonl"


def pair_key(entity_id_a: str, entity_id_b: str) -> str:
    """Return the order-independent entity pair key."""
    return "|".join(sorted([str(entity_id_a), str(entity_id_b)]))


def record_keep_separate_assertion(
    entity_id_a: str,
    entity_id_b: str,
    *,
    source_kind: str,
    operation_id: str | None,
    detection_count: int,
) -> dict[str, Any]:
    """Append a keep-separate source assertion event."""
    left, right = sorted([str(entity_id_a), str(entity_id_b)])
    event = {
        "schema_version": KEEP_SEPARATE_SCHEMA_VERSION,
        "event_kind": "assert_source",
        "pair_key": pair_key(left, right),
        "entity_id_a": left,
        "entity_id_b": right,
        "source_kind": source_kind,
        "operation_id": operation_id,
        "detection_count": int(detection_count),
        "ts": utc_now_iso(),
    }
    append_event(event)
    return event


def remove_operation_sources(
    operation_id: str,
    pair_keys: list[str] | tuple[str, ...] | set[str],
) -> list[dict[str, Any]]:
    """Append tombstones for all current sources from an operation."""
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError("operation_id must be a non-empty string")
    wanted = {str(key) for key in pair_keys}
    path = keep_separate_path(create=True)
    appended: list[dict[str, Any]] = []
    with hold_lock(path):
        events = _load_jsonl_rows(path)
        current_sources = _fold_sources(events)
        for key in sorted(wanted):
            for source_key in sorted(current_sources.get(key, {})):
                source_kind, source_operation_id = source_key
                if source_operation_id != operation_id:
                    continue
                event = {
                    "schema_version": KEEP_SEPARATE_SCHEMA_VERSION,
                    "event_kind": "source_removed",
                    "pair_key": key,
                    "source_kind": source_kind,
                    "operation_id": operation_id,
                    "ts": utc_now_iso(),
                }
                _validate_row(event)
                append_jsonl(path, event)
                appended.append(event)
                current_sources[key].pop(source_key, None)
    return appended


def append_event(event: dict[str, Any]) -> None:
    """Strict-validate and append one keep-separate event."""
    _validate_row(event)
    path = keep_separate_path(create=True)
    with hold_lock(path):
        append_jsonl(path, event)


def load_events() -> list[dict[str, Any]]:
    """Strict-load raw keep-separate events."""
    return _load_jsonl_rows(keep_separate_path())


def fold_assertions() -> list[KeepSeparateAssertion]:
    """Fold append-only keep-separate events into active assertions."""
    sources_by_pair = _fold_sources(load_events())
    assertions: list[KeepSeparateAssertion] = []
    for key, sources in sorted(sources_by_pair.items()):
        remaining = list(sources.values())
        if not remaining:
            continue
        detection_count = max(int(source["detection_count"]) for source in remaining)
        timestamps = [str(source["recorded_at"]) for source in remaining]
        left, right = key.split("|", maxsplit=1)
        assertions.append(
            KeepSeparateAssertion(
                assertion_id=_assertion_id(key),
                pair_key=key,
                entity_id_a=left,
                entity_id_b=right,
                dismissed_detection_count=detection_count,
                sources=tuple(
                    sorted(
                        remaining,
                        key=lambda source: (
                            str(source["source_kind"]),
                            str(source.get("operation_id") or ""),
                        ),
                    )
                ),
                created_at=min(timestamps),
                updated_at=max(timestamps),
                last_recorded_at=max(timestamps),
            )
        )
    return assertions


def find_assertion(entity_id_a: str, entity_id_b: str) -> KeepSeparateAssertion | None:
    """Return the folded assertion for a pair, if present."""
    target = pair_key(entity_id_a, entity_id_b)
    for assertion in fold_assertions():
        if assertion.pair_key == target:
            return assertion
    return None


def name_variant_pair_suppressed(
    entity_id_a: str,
    entity_id_b: str,
    current_detection_count: int,
) -> bool:
    """Return whether a name-variant pair is suppressed by keep-separate memory."""
    assertion = find_assertion(entity_id_a, entity_id_b)
    if assertion is None:
        return False
    return int(current_detection_count) <= assertion.dismissed_detection_count


def list_assertions() -> list[dict[str, Any]]:
    """Return redacted summaries for active keep-separate assertions."""
    return [
        {
            "assertion_id": assertion.assertion_id,
            "pair_key": assertion.pair_key,
            "entity_id_a": assertion.entity_id_a,
            "entity_id_b": assertion.entity_id_b,
            "dismissed_detection_count": assertion.dismissed_detection_count,
            "source_count": assertion.source_count,
            "created_at": assertion.created_at,
            "updated_at": assertion.updated_at,
            "last_recorded_at": assertion.last_recorded_at,
        }
        for assertion in fold_assertions()
    ]


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string ending in Z."""
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    message = f"malformed keep-separate JSONL at {path}:{lineno}"
                    logger.error(message)
                    raise KeepSeparateStoreError(message) from exc
                if not isinstance(row, dict):
                    message = f"non-object keep-separate JSONL at {path}:{lineno}"
                    logger.error(message)
                    raise KeepSeparateStoreError(message)
                try:
                    _validate_row(row)
                except KeepSeparateStoreError as exc:
                    message = f"invalid keep-separate row at {path}:{lineno}: {exc}"
                    logger.error(message)
                    raise KeepSeparateStoreError(message) from exc
                rows.append(row)
    except OSError as exc:
        message = f"failed to read keep-separate store {path}: {exc}"
        logger.error(message)
        raise KeepSeparateStoreError(message) from exc
    return rows


def _fold_sources(
    events: list[dict[str, Any]],
) -> dict[str, dict[tuple[str, str | None], dict[str, Any]]]:
    sources_by_pair: dict[str, dict[tuple[str, str | None], dict[str, Any]]] = {}
    for event in events:
        key = str(event["pair_key"])
        sources = sources_by_pair.setdefault(key, {})
        source_key = (str(event["source_kind"]), event.get("operation_id"))
        if event["event_kind"] == "source_removed":
            sources.pop(source_key, None)
            continue
        existing = sources.get(source_key)
        detection_count = int(event["detection_count"])
        if existing is not None and int(existing["detection_count"]) >= detection_count:
            continue
        sources[source_key] = {
            "source_kind": str(event["source_kind"]),
            "operation_id": event.get("operation_id"),
            "detection_count": detection_count,
            "recorded_at": str(event["ts"]),
        }
    return sources_by_pair


def _validate_row(row: dict[str, Any]) -> None:
    if row.get("schema_version") != KEEP_SEPARATE_SCHEMA_VERSION:
        raise KeepSeparateStoreError("invalid schema_version")
    event_kind = _required_str(row, "event_kind")
    if event_kind not in EVENT_KINDS:
        raise KeepSeparateStoreError(f"unknown event_kind: {event_kind}")
    key = _required_str(row, "pair_key")
    _required_str(row, "source_kind")
    _required_str(row, "ts")

    if event_kind == "assert_source":
        entity_id_a = _required_str(row, "entity_id_a")
        entity_id_b = _required_str(row, "entity_id_b")
        if key != pair_key(entity_id_a, entity_id_b):
            raise KeepSeparateStoreError("pair_key does not match entity ids")
        operation_id = row.get("operation_id")
        if operation_id is not None and not isinstance(operation_id, str):
            raise KeepSeparateStoreError("operation_id must be string or null")
        detection_count = row.get("detection_count")
        if not isinstance(detection_count, int) or detection_count < 1:
            raise KeepSeparateStoreError("detection_count must be a positive int")
        return

    operation_id = row.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        raise KeepSeparateStoreError("source_removed operation_id is required")


def _assertion_id(key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"ksep_{digest[:24]}"


def _required_str(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise KeepSeparateStoreError(f"missing or invalid {field}")
    return value
