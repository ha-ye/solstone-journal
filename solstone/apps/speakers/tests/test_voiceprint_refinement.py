# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for voiceprint decay and accumulation guards."""

from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np

from solstone.apps.speakers.encoder_config import (
    ACOUSTIC_HIGH,
    ACOUSTIC_MEDIUM,
    OWNER_THRESHOLD,
)
from solstone.think.entities import save_voiceprints_batch

STREAM = "test"


def _unit(vector: list[float]) -> np.ndarray:
    emb = np.array(vector + [0.0] * (256 - len(vector)), dtype=np.float32)
    return emb / np.linalg.norm(emb)


def _setup_owner(env) -> tuple[Path, np.ndarray]:
    principal_dir = env.create_entity("Self Person", is_principal=True)
    centroid = _unit([1.0, 0.0])
    np.savez_compressed(
        principal_dir / "owner_centroid.npz",
        centroid=centroid,
        cluster_size=np.array(70, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        last_refreshed_at=np.array("2026-03-15T12:00:00Z"),
    )
    return principal_dir, centroid


def _write_voiceprints(
    entity_id: str,
    embeddings: list[np.ndarray],
    added_ats: list[int | None],
    *,
    stream: str = STREAM,
    day: str = "20250101",
) -> None:
    items = []
    for i, (embedding, added_at) in enumerate(zip(embeddings, added_ats), start=1):
        metadata = {
            "day": day,
            "segment_key": f"09{i:02d}00_300",
            "source": "mic_audio",
            "stream": stream,
            "sentence_id": i,
        }
        if added_at is not None:
            metadata["added_at"] = added_at
        items.append((embedding, metadata))
    save_voiceprints_batch(entity_id, items)


def _voiceprint_count(entity_dir: Path) -> int:
    with np.load(entity_dir / "voiceprints.npz", allow_pickle=False) as data:
        return len(data["embeddings"])


def _segment(
    env,
    day: str,
    segment_key: str,
    embeddings: np.ndarray,
    *,
    source: str = "mic_audio",
) -> None:
    env.create_segment(
        day,
        segment_key,
        [source],
        stream=STREAM,
        embeddings=embeddings.astype(np.float32),
    )


def test_temporal_decay_weights_recent_voiceprints(speakers_env):
    from solstone.apps.speakers.attribution import VP_DECAY_LAMBDA, attribute_segment

    env = speakers_env()
    _setup_owner(env)
    env.create_entity("Alice Test")

    dir_old = _unit([0.0, 1.0])
    dir_new = _unit([0.0, 0.0, 1.0])
    now_ts = int(time.time() * 1000)
    old_ts = int((time.time() - 365 * 86_400) * 1000)

    _write_voiceprints(
        "alice_test",
        [dir_old] * 10 + [dir_new] * 2,
        [old_ts] * 10 + [now_ts] * 2,
    )

    unweighted = np.mean(np.stack([dir_old] * 10 + [dir_new] * 2), axis=0)
    unweighted /= np.linalg.norm(unweighted)
    assert float(np.dot(unweighted, dir_new)) < ACOUSTIC_MEDIUM

    weights = np.array(
        [math.exp(-VP_DECAY_LAMBDA * 365)] * 10 + [1.0] * 2,
        dtype=np.float32,
    )
    weighted = np.dot(weights, np.stack([dir_old] * 10 + [dir_new] * 2))
    weighted /= np.linalg.norm(weighted)
    assert float(np.dot(weighted, dir_new)) > ACOUSTIC_HIGH

    _segment(env, "20260101", "090000_300", dir_new.reshape(1, -1))

    result = attribute_segment("20260101", STREAM, "090000_300")
    label = result["labels"][0]

    assert label["speaker"] == "alice_test"
    assert label["confidence"] == "high"
    assert label["method"] == "acoustic"
    assert result["unmatched"] == []


def test_missing_added_at_defaults_to_fresh_weight(speakers_env):
    from solstone.apps.speakers.attribution import attribute_segment

    env = speakers_env()
    _setup_owner(env)
    env.create_entity("Alice Test")

    dir_missing_ts = _unit([0.0, 1.0])
    dir_recent = _unit([0.0, 0.0, 1.0])
    now_ts = int(time.time() * 1000)
    _write_voiceprints(
        "alice_test",
        [dir_missing_ts] * 10 + [dir_recent] * 2,
        [None] * 10 + [now_ts] * 2,
    )

    _segment(env, "20260101", "091000_300", dir_missing_ts.reshape(1, -1))

    result = attribute_segment("20260101", STREAM, "091000_300")
    label = result["labels"][0]

    assert label["speaker"] == "alice_test"
    assert label["confidence"] == "high"
    assert result["unmatched"] == []


def test_outlier_rejection_blocks_inconsistent_embeddings(speakers_env):
    from solstone.apps.speakers.attribution import (
        VP_OUTLIER_MIN_SAMPLES,
        VP_OUTLIER_MIN_SIMILARITY,
        accumulate_voiceprints,
    )

    env = speakers_env()
    _setup_owner(env)
    alice_dir = env.create_entity("Alice Test")

    dir_a = _unit([0.0, 1.0])
    dir_ortho = _unit([0.0, 0.0, 1.0])
    dir_opp = _unit([0.0, -1.0])
    now_ts = int(time.time() * 1000)
    _write_voiceprints(
        "alice_test",
        [dir_a] * VP_OUTLIER_MIN_SAMPLES,
        [now_ts] * VP_OUTLIER_MIN_SAMPLES,
    )

    _segment(
        env,
        "20260102",
        "090000_300",
        np.stack([dir_a, dir_ortho, dir_opp]),
    )
    labels = [
        {
            "sentence_id": sid,
            "speaker": "alice_test",
            "confidence": "high",
            "method": "structural_single_speaker",
        }
        for sid in (1, 2, 3)
    ]

    assert float(np.dot(dir_ortho, dir_a)) < VP_OUTLIER_MIN_SIMILARITY
    assert float(np.dot(dir_opp, dir_a)) < VP_OUTLIER_MIN_SIMILARITY

    saved = accumulate_voiceprints(
        "20260102", STREAM, "090000_300", labels, "mic_audio"
    )

    assert saved == {"alice_test": 1}
    assert _voiceprint_count(alice_dir) == VP_OUTLIER_MIN_SAMPLES + 1


def test_outlier_rejection_not_applied_below_min_samples(speakers_env):
    from solstone.apps.speakers.attribution import (
        VP_OUTLIER_MIN_SAMPLES,
        accumulate_voiceprints,
    )

    env = speakers_env()
    _setup_owner(env)
    alice_dir = env.create_entity("Alice Test")

    dir_a = _unit([0.0, 1.0])
    dir_ortho = _unit([0.0, 0.0, 1.0])
    now_ts = int(time.time() * 1000)
    _write_voiceprints("alice_test", [dir_a] * 3, [now_ts] * 3)

    assert VP_OUTLIER_MIN_SAMPLES > 3
    _segment(env, "20260102", "091000_300", dir_ortho.reshape(1, -1))
    labels = [
        {
            "sentence_id": 1,
            "speaker": "alice_test",
            "confidence": "high",
            "method": "structural_single_speaker",
        }
    ]

    saved = accumulate_voiceprints(
        "20260102", STREAM, "091000_300", labels, "mic_audio"
    )

    assert saved == {"alice_test": 1}
    assert _voiceprint_count(alice_dir) == 4


def test_outlier_rejection_not_applied_to_new_entity(speakers_env):
    from solstone.apps.speakers.attribution import accumulate_voiceprints

    env = speakers_env()
    _setup_owner(env)
    bob_dir = env.create_entity("Bob Smith")

    dir_far = _unit([0.0, 0.0, 1.0])
    _segment(env, "20260102", "092000_300", dir_far.reshape(1, -1))
    labels = [
        {
            "sentence_id": 1,
            "speaker": "bob_smith",
            "confidence": "high",
            "method": "structural_single_speaker",
        }
    ]

    saved = accumulate_voiceprints(
        "20260102", STREAM, "092000_300", labels, "mic_audio"
    )

    assert saved == {"bob_smith": 1}
    assert _voiceprint_count(bob_dir) == 1


def test_acoustic_cluster_high_confidence_is_flywheel_eligible(speakers_env):
    from solstone.apps.speakers.attribution import (
        VP_OUTLIER_MIN_SAMPLES,
        accumulate_voiceprints,
    )

    env = speakers_env()
    _setup_owner(env)
    alice_dir = env.create_entity("Alice Test")

    dir_a = _unit([0.0, 1.0])
    dir_ortho = _unit([0.0, 0.0, 1.0])
    now_ts = int(time.time() * 1000)
    _write_voiceprints(
        "alice_test",
        [dir_a] * VP_OUTLIER_MIN_SAMPLES,
        [now_ts] * VP_OUTLIER_MIN_SAMPLES,
    )

    _segment(env, "20260102", "093000_300", np.stack([dir_a, dir_ortho]))
    labels = [
        {
            "sentence_id": 1,
            "speaker": "alice_test",
            "confidence": "high",
            "method": "acoustic_cluster",
        },
        {
            "sentence_id": 2,
            "speaker": "alice_test",
            "confidence": "high",
            "method": "acoustic_cluster",
        },
    ]

    saved = accumulate_voiceprints(
        "20260102", STREAM, "093000_300", labels, "mic_audio"
    )

    assert saved == {"alice_test": 1}
    assert _voiceprint_count(alice_dir) == VP_OUTLIER_MIN_SAMPLES + 1
