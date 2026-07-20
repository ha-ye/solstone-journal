# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Local-only speaker quality aggregation.

Article-8 no-egress tripwire: every number is computed from journal-local
files, and this payload must never enter
support/diagnostics.py::collect_all or any outbound payload.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from solstone.apps.speakers.owner import (
    load_owner_centroid,
    load_owner_manual_bootstrap_guidance,
)
from solstone.think.awareness import get_current
from solstone.think.entities.journal import get_journal_principal
from solstone.think.utils import day_dirs, get_journal, iter_segments

SPEAKER_QUALITY_WINDOW_DAYS = 30
_CONFIDENCE_CLASSES = {"high", "medium", None}


def get_speaker_quality_status() -> dict[str, Any]:
    """Return bounded local speaker-quality counters."""
    window_days = _quality_window_day_dirs()
    counters = _initial_counters()

    for day_name, _day_abs in window_days:
        for _stream, _segment_key, segment_dir in iter_segments(day_name):
            if not _has_audio_embeddings(segment_dir):
                continue
            _count_segment_quality(segment_dir, counters)

    unreadable_files = counters["unreadable_files"]
    return {
        "quality_window_days": SPEAKER_QUALITY_WINDOW_DAYS,
        "quality_window_count": len(window_days),
        "quality_window_error_count": unreadable_files["total_window_count"],
        "tier_histogram": counters["tier_histogram"],
        "demotions_by_class": counters["demotions_by_class"],
        "corrections_window_count": counters["corrections_window_count"],
        "unreadable_files": unreadable_files,
        "empty_labels_without_skipped_segments": counters[
            "empty_labels_without_skipped_segments"
        ],
        "owner_voice": _owner_voice_state(),
    }


def _quality_window_day_dirs() -> list[tuple[str, str]]:
    return sorted(day_dirs().items(), reverse=True)[:SPEAKER_QUALITY_WINDOW_DAYS]


def _initial_counters() -> dict[str, Any]:
    return {
        "tier_histogram": {
            "high_statements": 0,
            "medium_statements": 0,
            "margin_declined_statements": 0,
            "unlabeled_sentence_statements": 0,
            "skipped_stub_segments": 0,
            "no_labels_file_segments": 0,
        },
        "demotions_by_class": {
            "owner_margin_declined": _empty_demotion_counts(),
            "acoustic_margin_declined": _empty_demotion_counts(),
        },
        "corrections_window_count": 0,
        "unreadable_files": {
            "speaker_labels_window_count": 0,
            "speaker_corrections_window_count": 0,
            "total_window_count": 0,
        },
        "empty_labels_without_skipped_segments": 0,
    }


def _empty_demotion_counts() -> dict[str, int]:
    return {
        "high_statements": 0,
        "medium_statements": 0,
        "none_statements": 0,
        "total_statements": 0,
    }


def _has_audio_embeddings(segment_dir: Path) -> bool:
    # Quality counts only segments eligible for speaker attribution at all; screen-only
    # segments should not be reported as unprocessed voice work.
    return any(
        path.is_file() and (path.stem == "audio" or path.stem.endswith("_audio"))
        for path in segment_dir.glob("*.npz")
    )


def _count_segment_quality(segment_dir: Path, counters: dict[str, Any]) -> None:
    _count_label_file(segment_dir / "talents" / "speaker_labels.json", counters)
    _count_corrections_file(
        segment_dir / "talents" / "speaker_corrections.json",
        counters,
    )


def _count_label_file(labels_path: Path, counters: dict[str, Any]) -> None:
    histogram = counters["tier_histogram"]
    if not labels_path.exists():
        histogram["no_labels_file_segments"] += 1
        return

    payload = _read_json_object(labels_path)
    if payload is None:
        _count_unreadable(counters, "speaker_labels_window_count")
        return

    labels = payload.get("labels")
    if not _labels_are_classifiable(labels):
        _count_unreadable(counters, "speaker_labels_window_count")
        return

    skipped = payload.get("skipped") is True
    if skipped:
        histogram["skipped_stub_segments"] += 1
    elif len(labels) == 0:
        counters["empty_labels_without_skipped_segments"] += 1

    for label in labels:
        _count_label(label, counters)


def _labels_are_classifiable(labels: Any) -> bool:
    if not isinstance(labels, list):
        return False
    return all(
        isinstance(label, dict) and label.get("confidence") in _CONFIDENCE_CLASSES
        for label in labels
    )


def _count_label(label: dict[str, Any], counters: dict[str, Any]) -> None:
    confidence = label.get("confidence")
    owner_margin_declined = label.get("owner_margin_declined") is True
    acoustic_margin_declined = label.get("acoustic_margin_declined") is True
    margin_declined = owner_margin_declined or acoustic_margin_declined

    if owner_margin_declined:
        _count_demotion(counters, "owner_margin_declined", confidence)
    if acoustic_margin_declined:
        _count_demotion(counters, "acoustic_margin_declined", confidence)

    histogram = counters["tier_histogram"]
    if confidence == "high":
        histogram["high_statements"] += 1
    elif confidence == "medium":
        histogram["medium_statements"] += 1
    elif margin_declined:
        histogram["margin_declined_statements"] += 1
    else:
        histogram["unlabeled_sentence_statements"] += 1


def _count_demotion(
    counters: dict[str, Any],
    demotion_class: str,
    confidence: Any,
) -> None:
    field = (
        f"{confidence}_statements"
        if confidence in {"high", "medium"}
        else "none_statements"
    )
    demotions = counters["demotions_by_class"][demotion_class]
    demotions[field] += 1
    demotions["total_statements"] += 1


def _count_corrections_file(corrections_path: Path, counters: dict[str, Any]) -> None:
    if not corrections_path.exists():
        return

    payload = _read_json_object(corrections_path)
    if payload is None:
        _count_unreadable(counters, "speaker_corrections_window_count")
        return

    corrections = payload.get("corrections", [])
    if not isinstance(corrections, list) or not all(
        isinstance(correction, dict) for correction in corrections
    ):
        _count_unreadable(counters, "speaker_corrections_window_count")
        return

    counters["corrections_window_count"] += len(corrections)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _count_unreadable(counters: dict[str, Any], field: str) -> None:
    unreadable_files = counters["unreadable_files"]
    unreadable_files[field] += 1
    unreadable_files["total_window_count"] += 1


def _load_awareness_voiceprint() -> dict[str, Any]:
    current_path = Path(get_journal()) / "awareness" / "current.json"
    # get_current() creates the awareness dir; this read-only surface must not write.
    if not current_path.exists():
        return {}

    voiceprint = get_current().get("voiceprint", {})
    if not isinstance(voiceprint, dict):
        return {}
    return voiceprint


def _owner_voice_state() -> dict[str, Any]:
    voiceprint = _load_awareness_voiceprint()
    status = str(voiceprint.get("status", "none"))
    centroid = load_owner_centroid()
    if centroid is not None:
        return {
            "bootstrap_state": "bootstrapped",
            "status": status,
            "centroid_saved": True,
            "evidence_tier": centroid.evidence_tier,
            "evidence_count": centroid.cluster_size,
            "built_at": centroid.created_at,
            "refreshed_at": centroid.last_refreshed_at,
        }

    principal = get_journal_principal()
    principal_id = str(principal["id"]) if principal else None
    guidance = load_owner_manual_bootstrap_guidance(principal_id)
    return {
        "bootstrap_state": "pre_bootstrap",
        "status": status,
        "centroid_saved": False,
        "evidence_tier": voiceprint.get("evidence_tier"),
        "evidence_count": int(guidance["manual_tags_count"]),
        "built_at": None,
        "refreshed_at": None,
    }
