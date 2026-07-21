# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only owner voice needs-you collectors."""

from __future__ import annotations

import logging
from typing import Any

from solstone.apps.speakers.copy import (
    NEEDS_YOU_RECURRING_MANY,
    NEEDS_YOU_RECURRING_ONE,
    OWNER_NEEDS_CONFIRM_VOICE_TEXT,
)
from solstone.apps.speakers.discovery import (
    MIN_SEGMENT_DIVERSITY,
    get_cluster_conversation_count,
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
from solstone.think.speaker_cluster_dismissals import (
    ClusterDismissalStoreError,
    cluster_dismissal_suppressed,
)

logger = logging.getLogger(__name__)


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
        f"/app/speakers/{today}",
        source_id="owner_voice:candidate",
    )


def _recurring_voice_need(_today: str) -> dict[str, Any] | None:
    if not _owner_centroid_file_exists():
        return None
    cache = load_discovery_cache()
    if cache is None:
        return None
    clusters = cache.get("clusters")
    if not isinstance(clusters, dict):
        return None
    eligible_clusters = _eligible_recurring_clusters(clusters)
    if not eligible_clusters:
        return None
    try:
        eligible_clusters = [
            (cluster_id, records)
            for cluster_id, records in eligible_clusters
            if not cluster_dismissal_suppressed(records)
        ]
    except ClusterDismissalStoreError:
        logger.warning(
            "owner voice recurring need suppressed: dismissal store unreadable",
            exc_info=True,
        )
        return None
    selected = _select_recurring_cluster(eligible_clusters)
    if selected is None:
        return None
    cluster_id, records = selected
    if not load_owner_centroid():
        return None
    conversation_count = get_cluster_conversation_count(records)
    if conversation_count < 1:
        logger.warning(
            "owner voice recurring need suppressed: selected cluster has no valid conversations"
        )
        return None
    return _route_need(
        _recurring_voice_text(conversation_count),
        f"/app/speakers?voice_cluster_id={cluster_id}",
        source_id=f"owner_voice:recurring:{cluster_id}",
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


def _eligible_recurring_clusters(
    clusters: dict[str, Any],
) -> list[tuple[int, list[dict[str, Any]]]]:
    eligible: list[tuple[int, list[dict[str, Any]]]] = []
    skipped_non_integer_ids: list[str] = []
    for raw_cluster_id, records in clusters.items():
        try:
            cluster_id = int(raw_cluster_id)
        except (TypeError, ValueError):
            skipped_non_integer_ids.append(str(raw_cluster_id))
            continue
        if not isinstance(records, list) or not _has_segment_diversity(records):
            continue
        eligible.append((cluster_id, records))
    if skipped_non_integer_ids:
        logger.warning(
            "owner voice recurring need skipped non-integer discovery cluster ids: %s",
            ", ".join(skipped_non_integer_ids),
        )
    return eligible


def _select_recurring_cluster(
    eligible_clusters: list[tuple[int, list[dict[str, Any]]]],
) -> tuple[int, list[dict[str, Any]]] | None:
    if not eligible_clusters:
        return None
    return max(
        eligible_clusters,
        key=lambda item: (
            len(item[1]),
            _segment_count(item[1]),
            -item[0],
        ),
    )


def _segment_count(records: list[dict[str, Any]]) -> int:
    segments: set[tuple[str, str, str]] = set()
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
        segments.add((day.strip(), stream.strip(), segment_key.strip()))
    return len(segments)


def _recurring_voice_text(conversation_count: int) -> str:
    if conversation_count == 1:
        return NEEDS_YOU_RECURRING_ONE
    return NEEDS_YOU_RECURRING_MANY.format(count=conversation_count)


def _route_need(text: str, href: str, *, source_id: str) -> dict[str, Any]:
    return {
        "text": text,
        "kind": "route",
        "payload": {"href": href},
        "source_id": source_id,
    }
