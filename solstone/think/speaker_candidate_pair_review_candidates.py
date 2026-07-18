# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Speaker candidate-pair review-candidate storage helpers.

Sole write-owner of:
  journal/speakers/candidate-pair-review-candidates.jsonl
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from solstone.think.journal_io import atomic_replace, hold_lock
from solstone.think.utils import get_journal

logger = logging.getLogger(__name__)


def review_candidates_dir() -> Path:
    """Return the speaker review-candidates directory, creating it if needed."""
    path = Path(get_journal()) / "speakers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def review_candidates_path() -> Path:
    """Return the speaker candidate-pair candidates JSONL path."""
    return review_candidates_dir() / "candidate-pair-review-candidates.jsonl"


def _load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    """Load JSONL rows from *path*, skipping blanks and malformed lines."""
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(
                    "speaker candidate-pair review candidates: malformed JSONL line %s in %s",
                    lineno,
                    path,
                )
                continue
            if not isinstance(data, dict):
                logger.warning(
                    "speaker candidate-pair review candidates: non-object JSONL line %s in %s (got %s)",
                    lineno,
                    path,
                    type(data).__name__,
                )
                continue
            rows.append(data)
    return rows


def load_candidates() -> list[dict[str, Any]]:
    """Load speaker candidate-pair review candidates from JSONL."""
    return _load_jsonl_rows(review_candidates_path())


def _save_jsonl_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write *rows* to *path* as JSONL using an atomic replace."""
    content = ""
    if rows:
        content = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
    atomic_replace(path, content)


def save_candidates(rows: list[dict[str, Any]]) -> None:
    """Persist speaker candidate-pair review candidates atomically."""
    _save_jsonl_rows(review_candidates_path(), rows)


def candidate_key(anchor_a: str, anchor_b: str) -> str:
    """Return the deterministic order-independent key for one anchor pair."""
    return json.dumps(
        sorted([anchor_a, anchor_b]), ensure_ascii=False, separators=(",", ":")
    )


def find_candidate(
    rows: list[dict[str, Any]], anchor_a: str, anchor_b: str
) -> dict[str, Any] | None:
    """Return one speaker candidate-pair review candidate by key."""
    target_key = candidate_key(anchor_a, anchor_b)
    for row in rows:
        if row.get("key") == target_key:
            return row
    return None


def locked_modify_candidates(
    fn: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Apply a locked read-modify-write cycle to candidate-pair review rows."""
    with hold_lock(review_candidates_path()):
        rows = load_candidates()
        new_rows = fn(rows)
        save_candidates(new_rows)
        return new_rows


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string ending in Z."""
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _sorted_anchors(anchor_a: str, anchor_b: str) -> tuple[str, str]:
    left, right = sorted([anchor_a, anchor_b])
    return left, right


def _evidence(
    *,
    similarity: float,
    source_intervals: int,
    target_intervals: int,
    source_samples: list[dict[str, Any]],
    target_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "basis": "speaker-candidate-pair",
        "similarity": similarity,
        "source_intervals": source_intervals,
        "target_intervals": target_intervals,
        "source_samples": source_samples,
        "target_samples": target_samples,
    }


def _dismissed_anchors(row: dict[str, Any]) -> tuple[str, str] | None:
    if row.get("status") != "dismissed":
        return None
    left = row.get("dismissed_anchor_a")
    right = row.get("dismissed_anchor_b")
    if not isinstance(left, str) or not isinstance(right, str):
        return None
    return left, right


def is_dismissed_pair_suppressed(
    rows: list[dict[str, Any]],
    source_anchors: set[str],
    target_anchors: set[str],
) -> bool:
    """Return whether a dismissed anchor pair is contained by two candidates."""
    for row in rows:
        anchors = _dismissed_anchors(row)
        if anchors is None:
            continue
        left, right = anchors
        if (left in source_anchors and right in target_anchors) or (
            left in target_anchors and right in source_anchors
        ):
            return True
    return False


def record_candidate_pair_candidate(
    *,
    source_anchor: str,
    target_anchor: str,
    source_anchors: set[str],
    target_anchors: set[str],
    similarity: float,
    source_intervals: int,
    target_intervals: int,
    source_samples: list[dict[str, Any]],
    target_samples: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, bool, bool]:
    """Create or update one speaker candidate-pair review candidate.

    Returns ``(row, created, suppressed)``. Dismissed rows are never reopened.
    """
    row: dict[str, Any] | None = None
    created = False
    suppressed = False

    def mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal row, created, suppressed
        if is_dismissed_pair_suppressed(rows, source_anchors, target_anchors):
            suppressed = True
            return rows

        key = candidate_key(source_anchor, target_anchor)
        anchor_a, anchor_b = _sorted_anchors(source_anchor, target_anchor)
        existing = find_candidate(rows, source_anchor, target_anchor)
        now = utc_now_iso()
        evidence = _evidence(
            similarity=similarity,
            source_intervals=source_intervals,
            target_intervals=target_intervals,
            source_samples=source_samples,
            target_samples=target_samples,
        )
        if existing is None:
            row = {
                "key": key,
                "anchor_a": anchor_a,
                "anchor_b": anchor_b,
                "status": "open",
                "similarity": similarity,
                "evidence": evidence,
                "first_surfaced": now,
                "last_surfaced": now,
                "created_at": now,
                "updated_at": now,
            }
            created = True
            return list(rows) + [row]

        existing["key"] = key
        existing["anchor_a"] = anchor_a
        existing["anchor_b"] = anchor_b
        existing["similarity"] = similarity
        existing["evidence"] = evidence
        existing["last_surfaced"] = now
        existing["updated_at"] = now
        row = existing
        created = False
        return rows

    locked_modify_candidates(mutate)
    return row, created, suppressed


def accept_candidate(anchor_a: str, anchor_b: str) -> dict[str, Any] | None:
    """Mark one speaker candidate-pair review candidate accepted."""
    row: dict[str, Any] | None = None

    def mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal row
        existing = find_candidate(rows, anchor_a, anchor_b)
        if existing is None:
            return rows
        existing["status"] = "accepted"
        existing["updated_at"] = utc_now_iso()
        row = existing
        return rows

    locked_modify_candidates(mutate)
    return row


def dismiss_candidate(anchor_a: str, anchor_b: str) -> dict[str, Any] | None:
    """Mark one speaker candidate-pair review candidate dismissed."""
    row: dict[str, Any] | None = None

    def mutate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal row
        existing = find_candidate(rows, anchor_a, anchor_b)
        if existing is None:
            return rows
        dismissed_anchor_a, dismissed_anchor_b = _sorted_anchors(anchor_a, anchor_b)
        existing["status"] = "dismissed"
        existing["dismissed_anchor_a"] = dismissed_anchor_a
        existing["dismissed_anchor_b"] = dismissed_anchor_b
        existing["dismissed_at"] = utc_now_iso()
        existing["updated_at"] = existing["dismissed_at"]
        row = existing
        return rows

    locked_modify_candidates(mutate)
    return row
