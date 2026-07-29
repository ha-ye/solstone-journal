# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""App-owned scheduled maintenance routines for speaker suggestions."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from solstone.apps.speakers.discovery import (
    SpeakerDiscoveryKernelError,
    discover_unknown_speakers,
)
from solstone.think import speaker_candidate_pair_review_candidates as pair_store
from solstone.think.entities.journal import (
    get_journal_principal,
    journal_entity_memory_path,
)
from solstone.think.journal_io import LockTimeout
from solstone.think.maintenance import MaintenanceRoutine
from solstone.think.speaker_review_candidates import (
    record_name_variant_candidate,
    review_candidates_path,
)
from solstone.think.utils import get_journal

logger = logging.getLogger(__name__)


def run_consolidation(args: list[str]) -> int:
    """Consolidate dense speaker candidates."""
    from solstone.apps.speakers.candidate_tracker import CandidateTracker

    parser = argparse.ArgumentParser(
        prog="journal maintenance run speakers:consolidate-pool"
    )
    parser.parse_args(args)

    try:
        result = CandidateTracker().consolidate_dense_candidates()
    except LockTimeout as exc:
        logger.warning("speaker candidate consolidation skipped: %s", exc)
        return 1

    logger.info(
        "speaker candidate consolidation refreshed: merged=%d",
        result.get("merged", 0),
    )
    return 0


def run_discovery_scan(args: list[str]) -> int:
    """Refresh recurring unknown speaker discovery clusters."""
    parser = argparse.ArgumentParser(
        prog="journal maintenance run speakers:discover-voices"
    )
    parser.parse_args(args)

    principal = get_journal_principal()
    if principal is None:
        return 0
    owner_path = journal_entity_memory_path(str(principal["id"])) / "owner_centroid.npz"
    if not owner_path.exists():
        return 0

    try:
        result = discover_unknown_speakers()
    except LockTimeout as exc:
        logger.warning("speaker discovery scan skipped: %s", exc)
        return 1
    except SpeakerDiscoveryKernelError as exc:
        logger.warning(
            "speaker discovery scan failed: stage=%s reason=%s",
            exc.stage,
            exc.reason,
        )
        return 2

    logger.info(
        "speaker discovery refreshed: clusters=%d",
        len(result.get("clusters", [])),
    )
    return 0


def _candidate_samples(candidate) -> list[dict[str, Any]]:
    from solstone.apps.speakers.audio import resolve_audio_url
    from solstone.apps.speakers.candidate_tracker import source_segment_anchor

    samples: list[dict[str, Any]] = []
    for source_segment in sorted(
        candidate.source_segments, key=lambda item: source_segment_anchor(item)
    ):
        day = str(source_segment["day"])
        stream = str(source_segment["stream"])
        segment_key = str(source_segment["segment_key"])
        source = str(source_segment["source"])
        audio_url = resolve_audio_url(day, stream, segment_key, source)
        if audio_url is None:
            continue
        samples.append(
            {
                "day": day,
                "stream": stream,
                "segment_key": segment_key,
                "source": source,
                "cluster_label": int(source_segment["cluster_label"]),
                "audio_url": audio_url,
            }
        )
        if len(samples) >= 3:
            break
    return samples


def _exactly_one_confirmed(left, right) -> bool:
    left_confirmed = left.status == "confirmed" or left.confirmed_entity is not None
    right_confirmed = right.status == "confirmed" or right.confirmed_entity is not None
    return left_confirmed != right_confirmed


def _eligible_for_pair_suggestion(left, right, score: float) -> bool:
    from solstone.apps.speakers.encoder_config import (
        CONSOLIDATE_MERGE_THRESHOLD,
        CONSOLIDATE_MIN_INTERVALS,
        CONSOLIDATE_SUGGEST_MIN,
    )

    if (
        left.n_intervals < CONSOLIDATE_MIN_INTERVALS
        or right.n_intervals < CONSOLIDATE_MIN_INTERVALS
    ):
        return False
    if left.status == "rejected" or right.status == "rejected":
        return False
    if (left.status == "confirmed" or left.confirmed_entity is not None) and (
        right.status == "confirmed" or right.confirmed_entity is not None
    ):
        return False
    return (CONSOLIDATE_SUGGEST_MIN <= score < CONSOLIDATE_MERGE_THRESHOLD) or (
        score >= CONSOLIDATE_MERGE_THRESHOLD and _exactly_one_confirmed(left, right)
    )


def run_candidate_pair_suggestions(args: list[str]) -> int:
    """Refresh dense speaker candidate-pair review candidates."""
    import numpy as np

    from solstone.apps.speakers.candidate_tracker import (
        CandidateTracker,
        candidate_source_anchors,
        canonical_candidate_anchor,
    )

    parser = argparse.ArgumentParser(
        prog="journal maintenance run speakers:candidate-pair-suggestions"
    )
    parser.parse_args(args)

    try:
        candidates = CandidateTracker().snapshot_candidates_locked()
    except LockTimeout as exc:
        logger.warning("speaker candidate-pair suggestions skipped: %s", exc)
        return 1

    found = 0
    created = 0
    updated = 0
    suppressed = 0
    for left_idx, left in enumerate(candidates):
        for right in candidates[left_idx + 1 :]:
            score = float(np.dot(left.centroid, right.centroid))
            if not _eligible_for_pair_suggestion(left, right, score):
                continue
            found += 1
            try:
                row, was_created, was_suppressed = pair_store.record_candidate_pair(
                    source_anchor=canonical_candidate_anchor(left),
                    target_anchor=canonical_candidate_anchor(right),
                    source_anchors=candidate_source_anchors(left),
                    target_anchors=candidate_source_anchors(right),
                    similarity=score,
                    source_intervals=left.n_intervals,
                    target_intervals=right.n_intervals,
                    source_samples=_candidate_samples(left),
                    target_samples=_candidate_samples(right),
                )
            except LockTimeout as exc:
                logger.warning("speaker candidate-pair suggestions skipped: %s", exc)
                return 1
            if was_suppressed:
                suppressed += 1
            elif row is not None and was_created:
                created += 1
            elif row is not None:
                updated += 1

    logger.info(
        "speaker candidate-pair suggestions refreshed: found=%d created=%d updated=%d "
        "suppressed=%d path=%s",
        found,
        created,
        updated,
        suppressed,
        pair_store.review_candidates_path(),
    )
    return 0


def run_name_variants(args: list[str]) -> int:
    """Refresh speaker name-variant review candidates."""
    from solstone.apps.speakers.bootstrap import detect_name_variant_candidates

    parser = argparse.ArgumentParser(
        prog="journal maintenance run speakers:name-variants"
    )
    parser.parse_args(args)

    journal = Path(get_journal())
    detection = detect_name_variant_candidates()
    created = 0
    updated = 0
    suppressed = 0
    for candidate in detection.get("candidates", []):
        _, was_created, was_suppressed = record_name_variant_candidate(
            source_id=candidate["source_id"],
            source_label=candidate["source_label"],
            target_id=candidate["target_id"],
            target_label=candidate["target_label"],
            similarity=candidate["similarity"],
            readiness=candidate["readiness"],
        )
        if was_suppressed:
            suppressed += 1
        if was_created:
            created += 1
        else:
            updated += 1

    logger.info(
        "speaker name variant candidates refreshed: journal=%s found=%d created=%d updated=%d suppressed=%d path=%s",
        journal,
        len(detection.get("candidates", [])),
        created,
        updated,
        suppressed,
        review_candidates_path(),
    )
    return 0


ROUTINES = [
    MaintenanceRoutine(
        name="consolidate-pool",
        description="Consolidate dense speaker candidates.",
        every="daily",
        run=run_consolidation,
        max_runtime="10m",
    ),
    MaintenanceRoutine(
        name="candidate-pair-suggestions",
        description="Find dense speaker candidate pairs for Suggestions.",
        every="daily",
        run=run_candidate_pair_suggestions,
        max_runtime="10m",
    ),
    MaintenanceRoutine(
        name="discover-voices",
        description="Refresh recurring voice discovery cache.",
        every="daily",
        run=run_discovery_scan,
        max_runtime="10m",
    ),
    MaintenanceRoutine(
        name="name-variants",
        description="Find speaker name variants for Suggestions.",
        every="daily",
        run=run_name_variants,
        max_runtime="10m",
    ),
]
