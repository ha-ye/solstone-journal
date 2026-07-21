# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Append-only speaker discovery-cluster dismissal store.

Sole write-owner of:
  journal/speakers/cluster-dismissals.jsonl
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

CLUSTER_DISMISSAL_SCHEMA_VERSION = 1
DISPOSITIONS = {"not_a_person", "quiet"}


class ClusterDismissalStoreError(RuntimeError):
    """Raised when cluster dismissal storage cannot be trusted."""


@dataclass(frozen=True)
class FoldedClusterDismissal:
    """Merged dismissal state folded from overlapping append-only events."""

    dismissal_id: str
    disposition: str
    members: tuple[dict[str, Any], ...]
    event_ids: tuple[str, ...]
    created_at: str
    updated_at: str

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def event_count(self) -> int:
        return len(self.event_ids)


def cluster_dismissals_dir(*, create: bool = False) -> Path:
    """Return the speaker dismissal directory."""
    path = Path(get_journal()) / "speakers"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def cluster_dismissals_path(*, create: bool = False) -> Path:
    """Return the speaker cluster-dismissal JSONL event-log path."""
    return cluster_dismissals_dir(create=create) / "cluster-dismissals.jsonl"


def record_cluster_dismissal(
    members: list[dict[str, Any]],
    disposition: str,
) -> dict[str, Any]:
    """Append one discovery-cluster dismissal event."""
    if disposition not in DISPOSITIONS:
        raise ValueError(f"unknown cluster dismissal disposition: {disposition}")
    canonical_members = _canonical_member_dicts(members)
    if not canonical_members:
        raise ValueError("cluster dismissal requires at least one member")
    ts = utc_now_iso()
    event = {
        "schema_version": CLUSTER_DISMISSAL_SCHEMA_VERSION,
        "event_kind": "dismiss",
        "dismiss_event_id": _dismiss_event_id(canonical_members, ts, disposition),
        "disposition": disposition,
        "members": canonical_members,
        "member_count": len(canonical_members),
        "ts": ts,
    }
    append_event(event)
    return event


def append_event(event: dict[str, Any]) -> None:
    """Strict-validate and append one cluster dismissal event."""
    _validate_row(event)
    path = cluster_dismissals_path(create=True)
    with hold_lock(path):
        append_jsonl(path, event)


def load_events() -> list[dict[str, Any]]:
    """Strict-load raw cluster dismissal events."""
    return _load_jsonl_rows(cluster_dismissals_path())


def fold_dismissals() -> list[FoldedClusterDismissal]:
    """Fold dismissal events into connected overlap components."""
    events = load_events()
    if not events:
        return []

    member_sets = [_member_set(event["members"]) for event in events]
    adjacency: list[set[int]] = [set() for _ in events]
    for left in range(len(events)):
        for right in range(left + 1, len(events)):
            if _overlap_ratio_min(member_sets[left], member_sets[right]) >= 0.50:
                adjacency[left].add(right)
                adjacency[right].add(left)

    folded: list[FoldedClusterDismissal] = []
    seen: set[int] = set()
    for start in range(len(events)):
        if start in seen:
            continue
        stack = [start]
        component: list[int] = []
        seen.add(start)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in sorted(adjacency[current]):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                stack.append(neighbor)
        folded.append(_fold_component(events, member_sets, component))
    return sorted(folded, key=lambda row: row.dismissal_id)


def cluster_dismissal_suppressed(candidate_members: list[dict[str, Any]]) -> bool:
    """Return whether a candidate cluster is suppressed by dismissed provenance."""
    candidate_set = _member_set(_canonical_member_dicts(candidate_members))
    if not candidate_set:
        return False
    for dismissal in fold_dismissals():
        dismissed_set = _member_set(list(dismissal.members))
        if len(candidate_set & dismissed_set) / len(candidate_set) >= 0.50:
            return True
    return False


def list_dismissals() -> list[dict[str, Any]]:
    """Return redacted summaries for folded cluster dismissals."""
    return [
        {
            "dismissal_id": dismissal.dismissal_id,
            "disposition": dismissal.disposition,
            "member_count": dismissal.member_count,
            "event_count": dismissal.event_count,
            "created_at": dismissal.created_at,
            "updated_at": dismissal.updated_at,
        }
        for dismissal in fold_dismissals()
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
                    message = f"malformed cluster dismissal JSONL at {path}:{lineno}"
                    logger.error(message)
                    raise ClusterDismissalStoreError(message) from exc
                if not isinstance(row, dict):
                    message = f"non-object cluster dismissal JSONL at {path}:{lineno}"
                    logger.error(message)
                    raise ClusterDismissalStoreError(message)
                try:
                    _validate_row(row)
                except ClusterDismissalStoreError as exc:
                    message = f"invalid cluster dismissal row at {path}:{lineno}: {exc}"
                    logger.error(message)
                    raise ClusterDismissalStoreError(message) from exc
                rows.append(row)
    except OSError as exc:
        message = f"failed to read cluster dismissal store {path}: {exc}"
        logger.error(message)
        raise ClusterDismissalStoreError(message) from exc
    return rows


def _fold_component(
    events: list[dict[str, Any]],
    member_sets: list[set[tuple[str, str, str, str, int]]],
    component: list[int],
) -> FoldedClusterDismissal:
    event_ids = tuple(
        sorted(str(events[index]["dismiss_event_id"]) for index in component)
    )
    union_members: set[tuple[str, str, str, str, int]] = set()
    disposition = "quiet"
    timestamps: list[str] = []
    for index in component:
        union_members.update(member_sets[index])
        timestamps.append(str(events[index]["ts"]))
        if events[index]["disposition"] == "not_a_person":
            disposition = "not_a_person"
    return FoldedClusterDismissal(
        dismissal_id=_folded_dismissal_id(event_ids),
        disposition=disposition,
        members=tuple(_member_dict(member) for member in sorted(union_members)),
        event_ids=event_ids,
        created_at=min(timestamps),
        updated_at=max(timestamps),
    )


def _validate_row(row: dict[str, Any]) -> None:
    if row.get("schema_version") != CLUSTER_DISMISSAL_SCHEMA_VERSION:
        raise ClusterDismissalStoreError("invalid schema_version")
    if row.get("event_kind") != "dismiss":
        raise ClusterDismissalStoreError("event_kind must be dismiss")
    _required_str(row, "dismiss_event_id")
    disposition = _required_str(row, "disposition")
    if disposition not in DISPOSITIONS:
        raise ClusterDismissalStoreError(f"invalid disposition: {disposition}")
    _required_str(row, "ts")
    members = row.get("members")
    if not isinstance(members, list):
        raise ClusterDismissalStoreError("members must be a list")
    canonical_members = _canonical_member_dicts(members)
    if canonical_members != members:
        raise ClusterDismissalStoreError("members must be canonical sorted provenance")
    if row.get("member_count") != len(members):
        raise ClusterDismissalStoreError("member_count mismatch")
    if not members:
        raise ClusterDismissalStoreError("dismiss event requires members")


def _canonical_member_dicts(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        _member_dict(item) for item in sorted({_member_tuple(item) for item in members})
    ]


def _member_set(members: list[dict[str, Any]]) -> set[tuple[str, str, str, str, int]]:
    return {_member_tuple(member) for member in members}


def _member_tuple(member: dict[str, Any]) -> tuple[str, str, str, str, int]:
    try:
        return (
            str(member["day"]),
            str(member["stream"]),
            str(member["segment_key"]),
            str(member["source"]),
            int(member["sentence_id"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ClusterDismissalStoreError("invalid cluster member provenance") from exc


def _member_dict(
    member: tuple[str, str, str, str, int] | dict[str, Any],
) -> dict[str, Any]:
    if isinstance(member, dict):
        member = _member_tuple(member)
    day, stream, segment_key, source, sentence_id = member
    return {
        "day": day,
        "stream": stream,
        "segment_key": segment_key,
        "source": source,
        "sentence_id": sentence_id,
    }


def _overlap_ratio_min(
    left: set[tuple[str, str, str, str, int]],
    right: set[tuple[str, str, str, str, int]],
) -> float:
    denominator = min(len(left), len(right))
    if denominator == 0:
        return 0.0
    return len(left & right) / denominator


def _dismiss_event_id(
    members: list[dict[str, Any]],
    ts: str,
    disposition: str,
) -> str:
    payload = {
        "disposition": disposition,
        "members": members,
        "ts": ts,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"cdev_{digest[:24]}"


def _folded_dismissal_id(event_ids: tuple[str, ...]) -> str:
    encoded = json.dumps(list(event_ids), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"cdsm_{digest[:24]}"


def _required_str(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ClusterDismissalStoreError(f"missing or invalid {field}")
    return value
