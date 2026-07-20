# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only owner voice needs-you collectors."""

from __future__ import annotations

from typing import Any

from solstone.apps.speakers.copy import (
    OWNER_NEEDS_CONFIRM_VOICE_TEXT,
    OWNER_NEEDS_RECURRING_VOICE_TEXT,
)
from solstone.apps.speakers.discovery import (
    MIN_SEGMENT_DIVERSITY,
    load_discovery_cache,
)
from solstone.apps.speakers.owner import (
    load_owner_centroid,
    owner_candidate_ready_possible,
    owner_detection_ready,
)
from solstone.think.entities.journal import (
    get_journal_principal,
    journal_entity_memory_path,
)


def build_owner_voice_needs(today: str) -> list[dict[str, Any]]:
    """Return owner voice needs-you items without mutating journal state."""
    needs: list[dict[str, Any]] = []
    candidate_need = _owner_candidate_need(today)
    if candidate_need is not None:
        needs.append(candidate_need)
    recurring_need = _recurring_voice_need(today)
    if recurring_need is not None:
        needs.append(recurring_need)
    return needs


def _owner_candidate_need(today: str) -> dict[str, Any] | None:
    if not owner_candidate_ready_possible():
        return None
    readiness = owner_detection_ready()
    if readiness.get("ready") is not True:
        return None
    return _route_need(
        OWNER_NEEDS_CONFIRM_VOICE_TEXT,
        today,
        source_id="owner_voice:candidate",
    )


def _recurring_voice_need(today: str) -> dict[str, Any] | None:
    if not _owner_centroid_file_exists():
        return None
    cache = load_discovery_cache()
    if cache is None:
        return None
    clusters = cache.get("clusters")
    if not isinstance(clusters, dict):
        return None
    if not any(_has_segment_diversity(records) for records in clusters.values()):
        return None
    if not load_owner_centroid():
        return None
    return _route_need(
        OWNER_NEEDS_RECURRING_VOICE_TEXT,
        today,
        source_id="owner_voice:recurring",
    )


def _owner_centroid_file_exists() -> bool:
    principal = get_journal_principal()
    if principal is None:
        return False
    owner_path = journal_entity_memory_path(str(principal["id"])) / "owner_centroid.npz"
    return owner_path.exists()


def _has_segment_diversity(records: Any) -> bool:
    if not isinstance(records, list):
        return False
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        day = record.get("day")
        stream = record.get("stream")
        segment_key = record.get("segment_key")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (day, stream, segment_key)
        ):
            continue
        seen.add((day.strip(), stream.strip(), segment_key.strip()))
        if len(seen) >= MIN_SEGMENT_DIVERSITY:
            return True
    return False


def _route_need(text: str, today: str, *, source_id: str) -> dict[str, Any]:
    return {
        "text": text,
        "kind": "route",
        "payload": {"href": f"/app/speakers/{today}"},
        "source_id": source_id,
    }
