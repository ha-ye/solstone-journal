# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for speakers app-owned maintenance routine descriptors."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from solstone.apps.speakers.maintenance import (
    run_candidate_pair_suggestions,
    run_consolidation,
    run_discovery_scan,
    run_name_variants,
)
from solstone.apps.speakers.suggest import suggest_opportunities
from solstone.think.entities.journal import load_journal_entity, scan_journal_entities
from solstone.think.maintenance import (
    discover_routines,
    expected_schedule_entry,
    maintenance_schedule_name,
)
from solstone.think.speaker_candidate_pair_review_candidates import (
    load_candidates as load_pair_candidates,
)
from solstone.think.speaker_review_candidates import load_candidates


def _write_voiceprints(entity_dir: Path, embedding: np.ndarray, *, offset: int = 0):
    embeddings = np.tile(embedding.reshape(1, -1), (5, 1))
    metadata = np.array(
        [
            json.dumps(
                {
                    "day": "20240101",
                    "segment_key": "143022_300",
                    "source": "mic_audio",
                    "sentence_id": i + offset,
                    "added_at": 1700000000000,
                }
            )
            for i in range(5)
        ],
        dtype=str,
    )
    np.savez_compressed(
        entity_dir / "voiceprints.npz",
        embeddings=embeddings,
        metadata=metadata,
    )


def _create_meetings_md(env, day: str, content: str) -> Path:
    chronicle_day = env.journal / "chronicle" / day
    chronicle_day.mkdir(parents=True, exist_ok=True)
    flat_day = env.journal / day
    if not flat_day.exists():
        flat_day.symlink_to(chronicle_day, target_is_directory=True)
    meetings_path = chronicle_day / "talents" / "meetings.md"
    meetings_path.parent.mkdir(parents=True, exist_ok=True)
    meetings_path.write_text(content, encoding="utf-8")
    return meetings_path


def _unit(vector: list[float]) -> np.ndarray:
    emb = np.array(vector + [0.0] * (256 - len(vector)), dtype=np.float32)
    return emb / np.linalg.norm(emb)


def _source_segment(day: str, cluster_label: int = 1) -> dict[str, object]:
    return {
        "day": day,
        "segment_key": "090000_300",
        "stream": "test",
        "source": "mic_audio",
        "cluster_label": cluster_label,
    }


def _seed_candidate_pool(env, candidates) -> None:
    from solstone.apps.speakers.candidate_tracker import CandidateTracker

    tracker = CandidateTracker(env.journal / "awareness" / "speaker_candidates.json")
    tracker._candidates = {candidate.cand_id: candidate for candidate in candidates}
    tracker._next_id = (
        max((candidate.cand_id for candidate in candidates), default=0) + 1
    )
    tracker.save()


def _profile(
    cand_id: int,
    centroid: np.ndarray,
    *,
    n_intervals: int = 30,
    status: str = "pending",
    confirmed_entity: str | None = None,
):
    from solstone.apps.speakers.candidate_tracker import CandidateProfile

    source_segment = _source_segment(f"2026010{cand_id}", cand_id)
    return CandidateProfile(
        cand_id=cand_id,
        centroid=centroid,
        n_segments=1,
        n_intervals=n_intervals,
        total_duration_s=float(n_intervals),
        source_segments=[source_segment],
        confirmed_entity=confirmed_entity,
        status=status,
    )


def test_speakers_name_variant_routine_is_discovered():
    routines = discover_routines()

    assert "speakers:name-variants" in routines
    routine = routines["speakers:name-variants"]
    assert routine.every == "daily"
    assert routine.max_runtime == "10m"
    assert expected_schedule_entry("speakers:name-variants", routine) == {
        "cmd": ["journal", "maintenance", "run", "speakers:name-variants"],
        "every": "daily",
        "enabled": True,
        "max_runtime": "10m",
    }
    assert maintenance_schedule_name("speakers:name-variants") == (
        "maintenance:speakers:name-variants"
    )


def test_speakers_pool_routines_are_discovered():
    routines = discover_routines()

    for routine_id in (
        "speakers:consolidate-pool",
        "speakers:candidate-pair-suggestions",
        "speakers:discover-voices",
    ):
        assert routine_id in routines
        routine = routines[routine_id]
        assert routine.every == "daily"
        assert routine.max_runtime == "10m"
        assert expected_schedule_entry(routine_id, routine) == {
            "cmd": ["journal", "maintenance", "run", routine_id],
            "every": "daily",
            "enabled": True,
            "max_runtime": "10m",
        }
        assert maintenance_schedule_name(routine_id) == f"maintenance:{routine_id}"


def test_run_discovery_scan_noops_without_owner(speakers_env, monkeypatch):
    from solstone.apps.speakers import maintenance as speakers_maintenance

    speakers_env()
    calls = []
    monkeypatch.setattr(
        speakers_maintenance,
        "discover_unknown_speakers",
        lambda: calls.append(True) or {"clusters": []},
    )

    assert run_discovery_scan([]) == 0
    assert calls == []


def test_run_consolidation_merges_dense_pool(speakers_env):
    from solstone.apps.speakers.candidate_tracker import CandidateTracker

    env = speakers_env()
    a = _unit([1.0, 0.0])
    b = _unit([0.70, np.sqrt(1.0 - 0.70**2)])
    _seed_candidate_pool(env, [_profile(1, a), _profile(2, b)])

    assert run_consolidation([]) == 0

    tracker = CandidateTracker(env.journal / "awareness" / "speaker_candidates.json")
    assert len(tracker._candidates) == 1
    assert tracker.consolidation_summary["merge_count_total"] == 1


def test_run_candidate_pair_suggestions_records_idempotently_with_audio(
    speakers_env,
):
    env = speakers_env()
    a = _unit([1.0, 0.0])
    b = _unit([0.60, np.sqrt(1.0 - 0.60**2)])
    for cand_id in (1, 2):
        env.create_segment(
            f"2026010{cand_id}",
            "090000_300",
            ["mic_audio"],
            stream="test",
            embeddings=np.tile(a.reshape(1, -1), (2, 1)),
        )
    _seed_candidate_pool(env, [_profile(1, a), _profile(2, b)])

    assert run_candidate_pair_suggestions([]) == 0
    assert run_candidate_pair_suggestions([]) == 0

    rows = load_pair_candidates()
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "open"
    assert np.isclose(row["similarity"], 0.60)
    assert row["evidence"]["source_intervals"] == 30
    assert row["evidence"]["target_intervals"] == 30
    sample = row["evidence"]["source_samples"][0]
    assert sample["segment_key"] == "090000_300"
    assert "segment" not in sample
    assert sample["audio_url"].endswith("/mic_audio.flac")


def test_run_candidate_pair_suggestions_filters_status_and_confirmed_pairs(
    speakers_env,
):
    from solstone.apps.speakers.maintenance import _eligible_for_pair_suggestion

    env = speakers_env()
    pending = _unit([1.0, 0.0])
    ambiguous = _unit([0.60, np.sqrt(1.0 - 0.60**2)])
    confirmed_y = (0.20 - 0.60 * 0.70) / np.sqrt(1.0 - 0.60**2)
    confirmed_high = _unit([0.70, confirmed_y, np.sqrt(1.0 - 0.70**2 - confirmed_y**2)])
    far = _unit([0.0, 1.0])
    confirmed = _profile(
        3, confirmed_high, status="confirmed", confirmed_entity="alice"
    )
    rejected = _profile(5, far, status="rejected")
    assert not _eligible_for_pair_suggestion(confirmed, confirmed, 0.90)
    assert not _eligible_for_pair_suggestion(rejected, _profile(6, pending), 0.60)
    _seed_candidate_pool(
        env,
        [
            _profile(1, pending),
            _profile(2, ambiguous),
            confirmed,
            rejected,
        ],
    )

    assert run_candidate_pair_suggestions([]) == 0

    rows = load_pair_candidates()
    assert len(rows) == 2
    similarities = sorted(round(float(row["similarity"]), 2) for row in rows)
    assert similarities == [0.6, 0.7]


def test_run_name_variants_records_idempotently_without_merging(speakers_env):
    env = speakers_env()
    embedding = env.create_embedding([1.0, 0.0, 0.0])
    alias_dir = env.create_entity("Alice")
    canonical_dir = env.create_entity("Alice Johnson")
    _write_voiceprints(alias_dir, embedding)
    _write_voiceprints(canonical_dir, embedding, offset=10)
    labels_path = env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "alice",
                "confidence": "high",
                "method": "voiceprint",
            }
        ],
    )
    labels_before = labels_path.read_text(encoding="utf-8")

    assert run_name_variants([]) == 0
    assert run_name_variants([]) == 0

    rows = load_candidates()
    assert len(rows) == 1
    assert rows[0]["status"] == "open"
    assert rows[0]["source_id"] == "alice"
    assert rows[0]["target_id"] == "alice_johnson"
    assert rows[0]["evidence"]["detection_count"] == 2
    assert load_journal_entity("alice") is not None
    assert load_journal_entity("alice_johnson") is not None
    assert "alice" in scan_journal_entities()
    assert labels_path.read_text(encoding="utf-8") == labels_before


def test_run_name_variants_bypasses_suggest_limit_starvation(speakers_env):
    env = speakers_env()
    env.create_entity("Romeo Montague")
    _create_meetings_md(
        env,
        "20240101",
        "# Meetings\n\n- 10:00 Strategy Call with Romeo and Juliet\n",
    )

    embedding = env.create_embedding([1.0, 0.0, 0.0])
    alias_dir = env.create_entity("Alice")
    canonical_dir = env.create_entity("Alice Johnson")
    _write_voiceprints(alias_dir, embedding)
    _write_voiceprints(canonical_dir, embedding, offset=10)

    limited = suggest_opportunities(limit=1)
    assert [item["type"] for item in limited] == ["import_linkable"]

    assert run_name_variants([]) == 0

    rows = load_candidates()
    assert len(rows) == 1
    assert rows[0]["source_id"] == "alice"
    assert rows[0]["target_id"] == "alice_johnson"
