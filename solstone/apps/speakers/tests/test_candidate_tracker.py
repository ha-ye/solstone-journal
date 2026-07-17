# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the headless speaker candidate tracker."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from solstone.apps.speakers.candidate_tracker import (
    SOLO_CLUSTER_LABEL,
    CandidateTracker,
)
from solstone.apps.speakers.encoder_config import (
    CONFIRM_MIN_DURATION_S,
    CONFIRM_MIN_INTERVALS,
    CONFIRM_MIN_SEGMENTS,
    ENCODER_ID,
    MERGE_THRESHOLD,
    SOLO_CLUSTER_MIN_COSINE,
    SPEAKER_EVIDENCE_VERSION,
    SPLIT_THRESHOLD,
    STABILITY_THRESHOLD,
)
from solstone.apps.speakers.owner import OWNER_THRESHOLD
from solstone.think.entities import save_voiceprints_batch

STREAM = "test"


def _unit(vector: list[float]) -> np.ndarray:
    emb = np.array(vector + [0.0] * (256 - len(vector)), dtype=np.float32)
    return emb / np.linalg.norm(emb)


def _setup_owner(env, name: str = "Self Person") -> tuple[Path, np.ndarray]:
    principal_dir = env.create_entity(name, is_principal=True)
    centroid = _unit([1.0, 0.0])
    np.savez_compressed(
        principal_dir / "owner_centroid.npz",
        centroid=centroid,
        cluster_size=np.array(70, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        last_refreshed_at=np.array("2026-03-15T12:00:00Z"),
    )
    return principal_dir, centroid


def _write_labeled_segment(
    env,
    day: str,
    segment_key: str,
    clusters: dict[int, np.ndarray],
    *,
    stream: str = STREAM,
    source: str = "mic_audio",
    duration_s: float = 5.0,
    speaker_evidence: str | None = None,
    speaker_evidence_multi_fraction: float = 0.0,
    write_speaker_labels: bool = True,
    include_durations: bool = True,
) -> Path:
    flat_dir, chronicle_dir = env._segment_dirs(day, segment_key, stream=stream)
    embeddings: list[np.ndarray] = []
    statement_ids: list[int] = []
    durations: list[float] = []
    labels: list[int] = []
    sid = 1
    for cluster_label, cluster_embeddings in clusters.items():
        for embedding in cluster_embeddings:
            embeddings.append(embedding)
            statement_ids.append(sid)
            durations.append(duration_s)
            labels.append(cluster_label)
            sid += 1

    header = {"raw": f"{source}.flac", "model": "test"}
    if speaker_evidence is not None:
        header.update(
            {
                "speaker_evidence": speaker_evidence,
                "speaker_evidence_multi_fraction": speaker_evidence_multi_fraction,
                "speaker_evidence_version": SPEAKER_EVIDENCE_VERSION,
            }
        )
    lines = [json.dumps(header)]
    for sid, cluster_label in zip(statement_ids, labels):
        row = {
            "start": "09:00:00",
            "text": f"sentence {sid}",
        }
        if write_speaker_labels:
            row["speaker"] = int(cluster_label)
        lines.append(json.dumps(row))

    for seg_dir in (flat_dir, chronicle_dir):
        (seg_dir / f"{source}.jsonl").write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        npz_payload = {
            "embeddings": np.stack(embeddings).astype(np.float32),
            "statement_ids": np.array(statement_ids, dtype=np.int32),
            "encoder": np.array(ENCODER_ID),
        }
        if include_durations:
            npz_payload["durations_s"] = np.array(durations, dtype=np.float32)
        np.savez_compressed(seg_dir / f"{source}.npz", **npz_payload)
        (seg_dir / f"{source}.flac").write_bytes(b"")
    return chronicle_dir


def _voiceprint_count(entity_dir: Path) -> int:
    with np.load(entity_dir / "voiceprints.npz", allow_pickle=False) as data:
        return len(data["embeddings"])


def _only_candidate(tracker: CandidateTracker):
    assert len(tracker._candidates) == 1
    return next(iter(tracker._candidates.values()))


def test_tracker_constants_locked():
    assert MERGE_THRESHOLD == 0.72
    assert SPLIT_THRESHOLD == 0.55
    assert STABILITY_THRESHOLD == 0.25
    assert SOLO_CLUSTER_MIN_COSINE == 0.43
    assert CONFIRM_MIN_SEGMENTS == 2
    assert CONFIRM_MIN_INTERVALS == 5
    assert CONFIRM_MIN_DURATION_S == 25.0


def test_solo_segment_admits_with_trimmed_count_zero(speakers_env, tmp_path):
    env = speakers_env()
    base = _unit([0.0, 1.0])
    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: np.stack([base] * 3)},
        speaker_evidence="single",
        write_speaker_labels=False,
    )

    tracker = CandidateTracker(tmp_path / "speaker_candidates.json")
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    candidate = _only_candidate(tracker)
    assert candidate.n_intervals == 3
    assert candidate.total_duration_s == 15.0
    assert candidate.source_segments == [
        {
            "day": "20260101",
            "segment_key": "090000_300",
            "stream": STREAM,
            "source": "mic_audio",
            "cluster_label": SOLO_CLUSTER_LABEL,
            "trimmed_count": 0,
        }
    ]


def test_solo_trim_drops_skewed_contamination_and_counts_survivors(
    speakers_env, tmp_path
):
    env = speakers_env()
    voice_a = _unit([0.0, 1.0])
    voice_b = _unit([0.0, 0.0, 1.0])
    mixed = np.stack([voice_a] * 27 + [voice_b] * 3)
    centroid = mixed.mean(axis=0)
    centroid = centroid / np.linalg.norm(centroid)
    assert float(np.dot(voice_b, centroid)) < SOLO_CLUSTER_MIN_COSINE
    assert float(np.dot(voice_a, centroid)) > SOLO_CLUSTER_MIN_COSINE
    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: mixed},
        speaker_evidence="single",
        write_speaker_labels=False,
    )

    tracker = CandidateTracker(tmp_path / "speaker_candidates.json")
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    candidate = _only_candidate(tracker)
    assert candidate.n_intervals == 27
    assert candidate.total_duration_s == 135.0
    assert candidate.source_segments[0]["trimmed_count"] == 3


def test_solo_without_durations_fails_confirmation_closed(speakers_env, tmp_path):
    env = speakers_env()
    store = tmp_path / "speaker_candidates.json"
    base = _unit([0.0, 1.0])
    for day, stream in (("20260101", "mic"), ("20260102", "sys")):
        seg_dir = _write_labeled_segment(
            env,
            day,
            "090000_300",
            {1: np.stack([base] * 18)},
            stream=stream,
            speaker_evidence="single",
            write_speaker_labels=False,
            include_durations=False,
        )
        tracker = CandidateTracker(store)
        tracker.process_segment(day, "090000_300", stream, "mic_audio", seg_dir)

    candidate = _only_candidate(CandidateTracker(store))
    assert candidate.n_segments == 2
    assert candidate.n_intervals == 36
    assert candidate.total_duration_s == 0.0
    assert candidate.ready_for_confirmation() is False


def test_solo_process_segment_idempotent_for_same_source_segment(
    speakers_env, tmp_path
):
    env = speakers_env()
    base = _unit([0.0, 1.0])
    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: np.stack([base] * 3)},
        speaker_evidence="single",
        write_speaker_labels=False,
    )
    tracker = CandidateTracker(tmp_path / "speaker_candidates.json")
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)
    source_keys = tracker._existing_source_keys()
    assert source_keys == {
        ("20260101", "090000_300", STREAM, "mic_audio", SOLO_CLUSTER_LABEL)
    }

    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    candidate = _only_candidate(tracker)
    assert tracker._existing_source_keys() == source_keys
    assert candidate.n_segments == 1
    assert candidate.n_intervals == 3
    assert candidate.total_duration_s == 15.0
    assert len(candidate.source_segments) == 1


def test_solo_then_diarizer_sequence_keeps_ingesting_without_warning(
    speakers_env, tmp_path, caplog
):
    import logging

    env = speakers_env()
    store = tmp_path / "speaker_candidates.json"
    solo = _unit([0.0, 1.0])
    diarized = _unit([0.0, 0.0, 1.0])
    solo_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: np.stack([solo] * 3)},
        speaker_evidence="single",
        write_speaker_labels=False,
    )
    diarizer_dir = _write_labeled_segment(
        env,
        "20260101",
        "091000_300",
        {1: np.stack([diarized] * 3)},
    )
    tracker = CandidateTracker(store)

    with caplog.at_level(logging.WARNING):
        for segment_key, seg_dir in (
            ("090000_300", solo_dir),
            ("091000_300", diarizer_dir),
        ):
            try:
                tracker.process_segment(
                    "20260101", segment_key, STREAM, "mic_audio", seg_dir
                )
            except Exception:
                logging.warning("Speaker candidate tracking failed", exc_info=True)

    assert [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ] == []
    assert len(tracker._candidates) == 2
    assert any(
        source_segment["cluster_label"] == SOLO_CLUSTER_LABEL
        for candidate in tracker._candidates.values()
        for source_segment in candidate.source_segments
    )


def test_solo_evidence_multi_not_admitted(speakers_env, tmp_path):
    env = speakers_env()
    base = _unit([0.0, 1.0])
    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: np.stack([base] * 3)},
        speaker_evidence="multi",
        speaker_evidence_multi_fraction=0.2,
        write_speaker_labels=False,
    )

    tracker = CandidateTracker(tmp_path / "speaker_candidates.json")
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    assert tracker._candidates == {}


def test_solo_evidence_none_not_admitted(speakers_env, tmp_path):
    env = speakers_env()
    base = _unit([0.0, 1.0])
    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: np.stack([base] * 3)},
        speaker_evidence="none",
        write_speaker_labels=False,
    )

    tracker = CandidateTracker(tmp_path / "speaker_candidates.json")
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    assert tracker._candidates == {}


def test_solo_evidence_absent_not_admitted(speakers_env, tmp_path):
    env = speakers_env()
    base = _unit([0.0, 1.0])
    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: np.stack([base] * 3)},
        write_speaker_labels=False,
    )

    tracker = CandidateTracker(tmp_path / "speaker_candidates.json")
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    assert tracker._candidates == {}


def test_solo_evidence_unreadable_not_admitted(speakers_env, tmp_path):
    env = speakers_env()
    base = _unit([0.0, 1.0])
    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: np.stack([base] * 3)},
        speaker_evidence="single",
        write_speaker_labels=False,
    )
    jsonl_path = seg_dir / "mic_audio.jsonl"
    jsonl_path.unlink()
    jsonl_path.mkdir()

    tracker = CandidateTracker(tmp_path / "speaker_candidates.json")
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    assert tracker._candidates == {}


def test_pool_persist_reload_round_trip(speakers_env, tmp_path):
    env = speakers_env()
    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: np.stack([_unit([0.0, 1.0])] * 3)},
    )
    store = tmp_path / "speaker_candidates.json"

    tracker = CandidateTracker(store)
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    reloaded = CandidateTracker(store)
    candidate = _only_candidate(reloaded)
    assert candidate.cand_id == 1
    assert candidate.n_segments == 1
    assert candidate.n_intervals == 3
    assert candidate.total_duration_s == 15.0
    assert candidate.source_segments == [
        {
            "day": "20260101",
            "segment_key": "090000_300",
            "stream": STREAM,
            "source": "mic_audio",
            "cluster_label": 1,
        }
    ]


def test_load_all_candidates_is_read_only(speakers_env, tmp_path):
    env = speakers_env()
    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: np.stack([_unit([0.0, 1.0])] * 3)},
    )
    store = tmp_path / "speaker_candidates.json"
    tracker = CandidateTracker(store)
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)
    before = store.read_text(encoding="utf-8")

    candidates = CandidateTracker(store).load_all_candidates()

    assert [candidate.cand_id for candidate in candidates] == [1]
    assert store.read_text(encoding="utf-8") == before


def test_merge_threshold_updates_existing_candidate(speakers_env, tmp_path):
    env = speakers_env()
    store = tmp_path / "speaker_candidates.json"
    base = _unit([0.0, 1.0])

    for day in ("20260101", "20260102"):
        seg_dir = _write_labeled_segment(
            env,
            day,
            "090000_300",
            {1: np.stack([base] * 3)},
        )
        tracker = CandidateTracker(store)
        tracker.process_segment(day, "090000_300", STREAM, "mic_audio", seg_dir)

    tracker = CandidateTracker(store)
    candidate = _only_candidate(tracker)
    assert candidate.n_segments == 2
    assert candidate.n_intervals == 6
    assert candidate.total_duration_s == 30.0


def test_split_threshold_creates_distinct_candidate(speakers_env, tmp_path):
    env = speakers_env()
    store = tmp_path / "speaker_candidates.json"
    bases = [_unit([0.0, 1.0]), _unit([0.0, 0.0, 1.0])]
    assert float(np.dot(bases[0], bases[1])) < SPLIT_THRESHOLD

    for i, base in enumerate(bases, start=1):
        day = f"2026010{i}"
        seg_dir = _write_labeled_segment(
            env,
            day,
            "090000_300",
            {1: np.stack([base] * 3)},
        )
        tracker = CandidateTracker(store)
        tracker.process_segment(day, "090000_300", STREAM, "mic_audio", seg_dir)

    tracker = CandidateTracker(store)
    assert len(tracker._candidates) == 2


def test_hold_band_does_not_merge_or_create(speakers_env, tmp_path):
    env = speakers_env()
    store = tmp_path / "speaker_candidates.json"
    base = _unit([0.0, 1.0])
    target = (MERGE_THRESHOLD + SPLIT_THRESHOLD) / 2
    ambiguous = _unit([0.0, target, np.sqrt(1.0 - target**2)])
    assert SPLIT_THRESHOLD < float(np.dot(base, ambiguous)) < MERGE_THRESHOLD

    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: np.stack([base] * 3)},
    )
    tracker = CandidateTracker(store)
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    seg_dir = _write_labeled_segment(
        env,
        "20260102",
        "090000_300",
        {1: np.stack([ambiguous] * 3)},
    )
    tracker = CandidateTracker(store)
    tracker.process_segment("20260102", "090000_300", STREAM, "mic_audio", seg_dir)

    tracker = CandidateTracker(store)
    candidate = _only_candidate(tracker)
    assert candidate.n_segments == 1
    assert candidate.n_intervals == 3


def test_stability_rejects_incoherent_cluster(speakers_env, tmp_path):
    env = speakers_env()
    a = _unit([0.0, 1.0])
    b = _unit([0.0, 0.0, 1.0])
    unstable = np.stack([a, a, b, b])
    centroid = unstable.mean(axis=0)
    centroid = centroid / np.linalg.norm(centroid)
    spread = float(np.mean(1.0 - unstable @ centroid))
    assert spread >= STABILITY_THRESHOLD

    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: unstable},
    )
    tracker = CandidateTracker(tmp_path / "speaker_candidates.json")
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    assert tracker._candidates == {}


def test_confirmation_queue_maturity_gates(speakers_env, tmp_path):
    env = speakers_env()
    store = tmp_path / "speaker_candidates.json"
    base = _unit([0.0, 1.0])

    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: np.stack([base] * 3)},
    )
    tracker = CandidateTracker(store)
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)
    assert tracker.confirmation_queue() == []

    seg_dir = _write_labeled_segment(
        env,
        "20260102",
        "090000_300",
        {1: np.stack([base] * 3)},
    )
    tracker = CandidateTracker(store)
    tracker.process_segment("20260102", "090000_300", STREAM, "mic_audio", seg_dir)
    queue = tracker.confirmation_queue()

    assert len(queue) == 1
    candidate = queue[0]
    assert candidate.n_segments >= CONFIRM_MIN_SEGMENTS
    assert candidate.n_intervals >= CONFIRM_MIN_INTERVALS
    assert candidate.total_duration_s >= CONFIRM_MIN_DURATION_S


def test_confirm_and_reject_status_transitions(speakers_env, tmp_path):
    env = speakers_env()
    store = tmp_path / "speaker_candidates.json"
    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {
            1: np.stack([_unit([0.0, 1.0])] * 3),
            2: np.stack([_unit([0.0, 0.0, 1.0])] * 3),
        },
    )
    tracker = CandidateTracker(store)
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    cand_ids = sorted(tracker._candidates)
    tracker.confirm(cand_ids[0], "alice_test")
    tracker.reject(cand_ids[1])

    reloaded = CandidateTracker(store)
    assert reloaded._candidates[cand_ids[0]].status == "confirmed"
    assert reloaded._candidates[cand_ids[0]].confirmed_entity == "alice_test"
    assert reloaded._candidates[cand_ids[1]].status == "rejected"


def test_retroactive_confirm_backfills_with_accumulate_guard(speakers_env, tmp_path):
    env = speakers_env()
    _setup_owner(env)
    alice_dir = env.create_entity("Alice Test")
    base = _unit([0.0, 1.0])
    save_voiceprints_batch(
        "alice_test",
        [
            (
                base,
                {
                    "day": "20251201",
                    "segment_key": f"09{i:02d}00_300",
                    "source": "mic_audio",
                    "stream": STREAM,
                    "sentence_id": 1,
                    "added_at": 1700000000000,
                },
            )
            for i in range(5)
        ],
    )

    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {7: np.stack([base] * 3)},
    )
    tracker = CandidateTracker(tmp_path / "speaker_candidates.json")
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    saved = tracker.retroactive_confirm(base, "alice_test")

    assert saved == 3
    assert _voiceprint_count(alice_dir) == 8
    candidate = _only_candidate(tracker)
    assert candidate.status == "confirmed"
    assert candidate.confirmed_entity == "alice_test"


def test_retroactive_confirm_noops_below_merge_threshold(speakers_env, tmp_path):
    env = speakers_env()
    _setup_owner(env)
    alice_dir = env.create_entity("Alice Test")
    base = _unit([0.0, 1.0])
    far = _unit([0.0, 0.0, 1.0])
    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: np.stack([base] * 3)},
    )
    tracker = CandidateTracker(tmp_path / "speaker_candidates.json")
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    saved = tracker.retroactive_confirm(far, "alice_test")

    assert saved == 0
    assert not (alice_dir / "voiceprints.npz").exists()
    candidate = _only_candidate(tracker)
    assert candidate.status == "pending"
    assert candidate.confirmed_entity is None


def test_retroactive_confirm_solo_backfills_from_statement_ids(speakers_env, tmp_path):
    env = speakers_env()
    _setup_owner(env)
    alice_dir = env.create_entity("Alice Test")
    base = _unit([0.0, 1.0])
    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: np.stack([base] * 3)},
        speaker_evidence="single",
        write_speaker_labels=False,
    )
    tracker = CandidateTracker(tmp_path / "speaker_candidates.json")
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    saved = tracker.retroactive_confirm(base, "alice_test")

    assert saved == 3
    assert _voiceprint_count(alice_dir) == 3
    candidate = _only_candidate(tracker)
    assert candidate.status == "confirmed"
    assert candidate.confirmed_entity == "alice_test"


def test_process_segment_idempotent_for_same_source_segment(speakers_env, tmp_path):
    env = speakers_env()
    base = _unit([0.0, 1.0])
    seg_dir = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {1: np.stack([base] * 3)},
    )
    tracker = CandidateTracker(tmp_path / "speaker_candidates.json")
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)
    tracker.process_segment("20260101", "090000_300", STREAM, "mic_audio", seg_dir)

    candidate = _only_candidate(tracker)
    assert candidate.n_segments == 1
    assert candidate.n_intervals == 3
    assert candidate.total_duration_s == 15.0


def test_identify_cluster_triggers_retroactive_confirm(speakers_env):
    from solstone.apps.speakers.discovery import identify_cluster

    env = speakers_env()
    _setup_owner(env)
    base = _unit([0.0, 1.0])
    candidate_seg = _write_labeled_segment(
        env,
        "20260101",
        "090000_300",
        {3: np.stack([base] * 3)},
    )
    CandidateTracker().process_segment(
        "20260101",
        "090000_300",
        STREAM,
        "mic_audio",
        candidate_seg,
    )

    env.create_segment(
        "20260102",
        "091000_300",
        ["mic_audio"],
        stream=STREAM,
        embeddings=base.reshape(1, -1),
    )
    awareness = env.journal / "awareness"
    awareness.mkdir(parents=True, exist_ok=True)
    (awareness / "discovery_clusters.json").write_text(
        json.dumps(
            {
                "version": "test",
                "clusters": {
                    "0": [
                        {
                            "day": "20260102",
                            "stream": STREAM,
                            "segment_key": "091000_300",
                            "source": "mic_audio",
                            "sentence_id": 1,
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = identify_cluster(0, "Alice Test")

    assert result["status"] == "identified"
    alice_dir = env.journal / "entities" / "alice_test"
    assert _voiceprint_count(alice_dir) == 4
    tracker = CandidateTracker()
    candidate = _only_candidate(tracker)
    assert candidate.status == "confirmed"
    assert candidate.confirmed_entity == "alice_test"


def test_backfill_reattribute_preserves_user_correction(speakers_env):
    from solstone.apps.speakers.attribution import backfill_segments

    env = speakers_env()
    _setup_owner(env)
    env.create_entity("Alice Test")
    bob_dir = env.create_entity("Bob Smith")
    bob = _unit([0.0, 1.0])
    np.savez_compressed(
        bob_dir / "voiceprints.npz",
        embeddings=np.stack([bob] * 6).astype(np.float32),
        metadata=np.array(
            [
                json.dumps(
                    {
                        "day": "20251201",
                        "segment_key": f"09{i:02d}00_300",
                        "source": "mic_audio",
                        "stream": STREAM,
                        "sentence_id": 1,
                        "added_at": 1700000000000,
                    }
                )
                for i in range(6)
            ],
            dtype=str,
        ),
    )
    _write_labeled_segment(
        env,
        "20260101",
        "093000_300",
        {1: np.stack([bob])},
    )
    env.create_speaker_labels(
        "20260101",
        "093000_300",
        [
            {
                "sentence_id": 1,
                "speaker": "old_pipeline",
                "confidence": "high",
                "method": "acoustic",
            }
        ],
    )
    env.create_speaker_corrections(
        "20260101",
        "093000_300",
        [
            {
                "sentence_id": 1,
                "original_speaker": "old_pipeline",
                "corrected_speaker": "alice_test",
                "original_method": "acoustic",
                "timestamp": 1700000000000,
            }
        ],
    )

    skipped = backfill_segments(dry_run=False, reattribute=False)
    reattributed = backfill_segments(dry_run=False, reattribute=True)

    assert skipped["processed"] == 0
    assert skipped["already_labeled"] == 1
    assert reattributed["processed"] == 1
    labels_path = (
        env.journal
        / "chronicle"
        / "20260101"
        / STREAM
        / "093000_300"
        / "talents"
        / "speaker_labels.json"
    )
    labels = json.loads(labels_path.read_text(encoding="utf-8"))["labels"]
    assert labels[0]["speaker"] == "alice_test"
    assert labels[0]["method"] == "user_corrected"
