# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for owner voice identification."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from flask import Flask

from solstone.apps.speakers.encoder_config import ENCODER_ID, OVERLAP_DETECTOR_ID
from solstone.think.awareness import get_current, update_state


def _normalized(vector: np.ndarray) -> np.ndarray:
    return vector / np.linalg.norm(vector)


def _write_segment(
    journal: Path,
    day: str,
    stream: str,
    segment_key: str,
    source: str,
    embeddings: np.ndarray,
    *,
    durations_s: np.ndarray | None = None,
) -> Path:
    chronicle_day = journal / "chronicle" / day
    chronicle_day.mkdir(parents=True, exist_ok=True)
    flat_day = journal / day
    if not flat_day.exists():
        flat_day.symlink_to(chronicle_day, target_is_directory=True)
    segment_dir = chronicle_day / stream / segment_key
    segment_dir.mkdir(parents=True, exist_ok=True)

    statement_ids = np.arange(1, len(embeddings) + 1, dtype=np.int32)
    npz_kwargs = {
        "embeddings": np.asarray(embeddings, dtype=np.float32),
        "statement_ids": statement_ids,
    }
    if durations_s is not None:
        npz_kwargs["durations_s"] = np.asarray(durations_s, dtype=np.float32)
    np.savez_compressed(segment_dir / f"{source}.npz", **npz_kwargs)

    time_part = segment_key.split("_")[0]
    base_h = int(time_part[0:2])
    base_m = int(time_part[2:4])
    base_s = int(time_part[4:6])
    base_seconds = base_h * 3600 + base_m * 60 + base_s

    lines = [json.dumps({"raw": f"{source}.flac", "model": "medium.en"})]
    for idx in range(len(embeddings)):
        abs_seconds = base_seconds + idx * 5
        h = (abs_seconds // 3600) % 24
        m = (abs_seconds % 3600) // 60
        s = abs_seconds % 60
        lines.append(
            json.dumps(
                {
                    "start": f"{h:02d}:{m:02d}:{s:02d}",
                    "text": f"Sentence {idx + 1}",
                }
            )
        )

    (segment_dir / f"{source}.jsonl").write_text("\n".join(lines) + "\n")
    (segment_dir / f"{source}.flac").write_bytes(b"")
    return segment_dir


def _rewrite_segment_header(segment_dir: Path, source: str, **updates: object) -> None:
    jsonl_path = segment_dir / f"{source}.jsonl"
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0]) if lines else {}
    header.update(updates)
    lines[0] = json.dumps(header)
    jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _owner_embeddings(count: int, rng: np.random.Generator) -> np.ndarray:
    base = np.zeros(256, dtype=np.float32)
    base[0] = 1.0
    return np.repeat(base.reshape(1, -1), count, axis=0)


def _noise_embeddings(count: int, rng: np.random.Generator) -> np.ndarray:
    embeddings = rng.normal(0, 1, (count, 256)).astype(np.float32)
    return embeddings / np.linalg.norm(embeddings, axis=1, keepdims=True)


def _other_cluster_embeddings(count: int) -> np.ndarray:
    base = np.zeros(256, dtype=np.float32)
    base[1] = 1.0
    return np.repeat(base.reshape(1, -1), count, axis=0)


def _candidate_path(journal: Path) -> Path:
    return journal / "awareness" / "owner_candidate.npz"


def _write_confirmed_owner_centroid(env, *, cluster_size: int = 60) -> Path:
    from solstone.apps.speakers.encoder_config import OWNER_THRESHOLD

    principal_dir = env.create_entity("Self Person", is_principal=True)
    centroid = _normalized(np.array([1.0] + [0.0] * 255, dtype=np.float32))
    np.savez_compressed(
        principal_dir / "owner_centroid.npz",
        centroid=centroid,
        cluster_size=np.array(cluster_size, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        last_refreshed_at=np.array("2026-03-15T12:00:00Z"),
    )
    return principal_dir / "owner_centroid.npz"


def _normalize_rows(embeddings: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    return embeddings / np.where(norms == 0, 1.0, norms)


def _write_labeled_segment(
    env,
    day: str,
    segment_key: str,
    clusters: dict[int, np.ndarray],
    *,
    stream: str = "test",
    source: str = "mic_audio",
    duration_s: float = 5.0,
    overlap_fraction: float = 0.0,
) -> Path:
    flat_dir, chronicle_dir = env._segment_dirs(day, segment_key, stream=stream)
    embeddings: list[np.ndarray] = []
    statement_ids: list[int] = []
    durations: list[float] = []
    labels: list[int] = []
    sentence_id = 1
    for cluster_label, cluster_embeddings in clusters.items():
        for embedding in cluster_embeddings:
            embeddings.append(embedding)
            statement_ids.append(sentence_id)
            durations.append(duration_s)
            labels.append(cluster_label)
            sentence_id += 1

    lines = [
        json.dumps(
            {
                "raw": f"{source}.flac",
                "model": "test",
                "overlap_fraction": overlap_fraction,
                "overlap_detector": OVERLAP_DETECTOR_ID,
            }
        )
    ]
    for sid, cluster_label in zip(statement_ids, labels):
        lines.append(
            json.dumps(
                {
                    "start": "09:00:00",
                    "text": f"sentence {sid}",
                    "speaker": int(cluster_label),
                }
            )
        )

    for seg_dir in (flat_dir, chronicle_dir):
        (seg_dir / f"{source}.jsonl").write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        np.savez_compressed(
            seg_dir / f"{source}.npz",
            embeddings=np.stack(embeddings).astype(np.float32),
            statement_ids=np.array(statement_ids, dtype=np.int32),
            durations_s=np.array(durations, dtype=np.float32),
            encoder=np.array(ENCODER_ID),
        )
        (seg_dir / f"{source}.flac").write_bytes(b"")
    return chronicle_dir


def _source_segment(
    day: str,
    segment_key: str,
    *,
    stream: str,
    source: str = "mic_audio",
    cluster_label: int = 1,
) -> dict[str, object]:
    return {
        "day": day,
        "stream": stream,
        "segment_key": segment_key,
        "source": source,
        "cluster_label": cluster_label,
    }


def _candidate_record(
    cand_id: int,
    source_segments: list[dict[str, object]],
    *,
    n_intervals: int,
    n_segments: int | None = None,
    total_duration_s: float = 300.0,
    status: str = "pending",
    confirmed_entity: str | None = None,
) -> dict[str, object]:
    centroid = np.zeros(256, dtype=np.float32)
    centroid[0] = 1.0
    return {
        "cand_id": cand_id,
        "centroid": centroid.astype(float).tolist(),
        "n_segments": n_segments if n_segments is not None else len(source_segments),
        "n_intervals": n_intervals,
        "total_duration_s": total_duration_s,
        "source_segments": source_segments,
        "confirmed_entity": confirmed_entity,
        "status": status,
    }


def _write_candidate_pool(
    journal: Path,
    candidates: list[dict[str, object]],
) -> Path:
    path = journal / "awareness" / "speaker_candidates.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    next_id = (
        max((int(candidate["cand_id"]) for candidate in candidates), default=0) + 1
    )
    path.write_text(
        json.dumps({"next_id": next_id, "candidates": candidates}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _save_manual_owner_tags(
    env,
    principal_id: str,
    day: str,
    segment_key: str,
    embeddings: np.ndarray,
    *,
    source: str = "audio",
    method: str = "user_assigned",
    durations_s: np.ndarray | None = None,
    overlap_fraction: float = 0.0,
) -> Path:
    from solstone.apps.speakers.routes import _save_voiceprint

    normalized_embeddings = _normalize_rows(np.asarray(embeddings, dtype=np.float32))
    segment_dir = _write_segment(
        env.journal,
        day,
        "test",
        segment_key,
        source,
        normalized_embeddings,
        durations_s=durations_s,
    )
    env.create_speaker_labels(
        day,
        segment_key,
        [
            {
                "sentence_id": idx,
                "speaker": principal_id,
                "confidence": "high",
                "method": method,
            }
            for idx in range(1, len(normalized_embeddings) + 1)
        ],
    )
    _rewrite_segment_header(
        segment_dir,
        source,
        overlap_fraction=overlap_fraction,
        overlap_detector=OVERLAP_DETECTOR_ID,
    )
    for idx, embedding in enumerate(normalized_embeddings, start=1):
        _save_voiceprint(
            principal_id,
            embedding,
            day,
            segment_key,
            source,
            idx,
            stream="test",
        )
    return segment_dir


def test_load_owner_embedding_inventory_counts_without_materializing(
    speakers_env, monkeypatch
):
    import solstone.think.journal_io.npz as npz_io
    from solstone.apps.speakers import owner as owner_module
    from solstone.apps.speakers.owner import load_owner_embedding_inventory
    from solstone.apps.speakers.routes import (
        _load_embeddings_file,
        _scan_segment_embeddings,
    )
    from solstone.think.utils import segment_path

    env = speakers_env()
    _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _owner_embeddings(4, np.random.default_rng(1))},
        stream="mic",
    )
    _write_labeled_segment(
        env,
        "20240101",
        "091000_300",
        {1: _owner_embeddings(6, np.random.default_rng(2))},
        stream="sys",
    )

    reference_segments = 0
    reference_embeddings = 0
    for segment in _scan_segment_embeddings("20240101"):
        reference_segments += 1
        segment_dir = segment_path("20240101", segment["key"], segment["stream"])
        for source in segment["sources"]:
            emb_data = _load_embeddings_file(segment_dir / f"{source}.npz")
            assert emb_data is not None
            reference_embeddings += int(len(emb_data[0]))

    def fail_materialize(*args, **kwargs):
        raise AssertionError("inventory materialized embedding arrays")

    monkeypatch.setattr(owner_module, "_routes_helpers", fail_materialize)
    monkeypatch.setattr(owner_module, "load_npz", fail_materialize)
    monkeypatch.setattr(npz_io, "load_npz", fail_materialize)

    assert load_owner_embedding_inventory() == {
        "segments_available": reference_segments,
        "embeddings_available": reference_embeddings,
    }


def test_detect_owner_no_candidate_pool_marks_no_cluster(speakers_env):
    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _owner_embeddings(40, np.random.default_rng(1))},
        stream="mic",
    )

    result = detect_owner_candidate()

    assert result["status"] == "no_cluster"
    assert result["reason"] == "pool_missing"
    assert result["recommendation"] == "no_cluster"
    assert get_current()["voiceprint"]["status"] == "no_cluster"


def test_detect_owner_empty_candidate_pool_marks_no_cluster(speakers_env):
    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    _write_candidate_pool(env.journal, [])

    result = detect_owner_candidate()

    assert result["status"] == "no_cluster"
    assert result["reason"] == "pool_empty"
    assert result["recommendation"] == "no_cluster"
    assert get_current()["voiceprint"]["status"] == "no_cluster"


def test_detect_owner_candidate_pool_ready(speakers_env):
    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    rng = np.random.default_rng(42)
    _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _owner_embeddings(20, rng)},
        stream="mic",
    )
    _write_labeled_segment(
        env,
        "20240101",
        "091000_300",
        {1: _owner_embeddings(20, rng)},
        stream="sys",
    )
    _write_labeled_segment(
        env,
        "20240101",
        "092000_300",
        {1: _owner_embeddings(20, rng)},
        stream="mic",
    )
    _write_candidate_pool(
        env.journal,
        [
            _candidate_record(
                1,
                [
                    _source_segment("20240101", "090000_300", stream="mic"),
                    _source_segment("20240101", "091000_300", stream="sys"),
                    _source_segment("20240101", "092000_300", stream="mic"),
                ],
                n_intervals=60,
                total_duration_s=300.0,
            )
        ],
    )

    result = detect_owner_candidate()

    assert result["status"] == "candidate"
    assert result["cluster_size"] == 60
    assert result["streams_represented"] == 2
    assert result["recommendation"] == "ready"
    assert len(result["samples"]) == 3
    sample_segments = set()
    for sample in result["samples"]:
        assert {
            "day",
            "stream",
            "segment_key",
            "source",
            "sentence_id",
            "duration_s",
            "audio_url",
        } <= set(sample)
        sample_segments.add((sample["day"], sample["stream"], sample["segment_key"]))
        assert sample["audio_url"] == (
            f"/app/speakers/api/serve_audio/{sample['day']}/"
            f"{sample['stream']}/{sample['segment_key']}/{sample['source']}.flac"
        )
    assert len(sample_segments) == len(result["samples"])
    assert _candidate_path(env.journal).exists()
    assert get_current()["voiceprint"]["status"] == "candidate"


def test_owner_candidate_samples_use_registered_audio_extension(speakers_env):
    from solstone.apps.speakers.owner import _owner_candidate_samples

    env = speakers_env()
    embeddings = _owner_embeddings(3, np.random.default_rng(1))
    provenance = []
    for idx, segment_key in enumerate(
        ("090000_300", "091000_300", "092000_300"),
        start=1,
    ):
        env.create_segment(
            "20240101",
            segment_key,
            ["mic_audio"],
            num_sentences=1,
            embeddings=embeddings[idx - 1 : idx],
            audio_extension=".m4a",
        )
        provenance.append(
            {
                "day": "20240101",
                "stream": "test",
                "segment_key": segment_key,
                "source": "mic_audio",
                "sentence_id": 1,
                "duration_s": 5.0,
            }
        )

    samples = _owner_candidate_samples(embeddings, embeddings[0], provenance)

    assert len(samples) == 3
    for sample in samples:
        assert sample["audio_url"] == (
            f"/app/speakers/api/serve_audio/{sample['day']}/"
            f"{sample['stream']}/{sample['segment_key']}/{sample['source']}.m4a"
        )


def test_owner_candidate_samples_allow_missing_audio(speakers_env):
    from solstone.apps.speakers.owner import _owner_candidate_samples

    env = speakers_env()
    embeddings = _owner_embeddings(1, np.random.default_rng(1))
    env.create_segment(
        "20240101",
        "090000_300",
        ["mic_audio"],
        num_sentences=1,
        embeddings=embeddings,
    )
    (
        env.journal
        / "chronicle"
        / "20240101"
        / "test"
        / "090000_300"
        / "mic_audio.flac"
    ).unlink()
    provenance = [
        {
            "day": "20240101",
            "stream": "test",
            "segment_key": "090000_300",
            "source": "mic_audio",
            "sentence_id": 1,
            "duration_s": 5.0,
        }
    ]

    samples = _owner_candidate_samples(embeddings, embeddings[0], provenance)

    assert samples[0]["audio_url"] is None


def test_detect_owner_candidate_selection_skips_rejected_and_non_principal(
    speakers_env,
):
    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    env.create_entity("Self Person", is_principal=True)
    _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _owner_embeddings(40, np.random.default_rng(3))},
        stream="mic",
    )
    _write_candidate_pool(
        env.journal,
        [
            _candidate_record(
                1,
                [_source_segment("20240101", "090000_300", stream="missing")],
                n_intervals=100,
                status="rejected",
            ),
            _candidate_record(
                2,
                [_source_segment("20240101", "090000_300", stream="missing")],
                n_intervals=90,
                confirmed_entity="someone_else",
            ),
            _candidate_record(
                3,
                [_source_segment("20240101", "090000_300", stream="mic")],
                n_intervals=40,
            ),
        ],
    )

    result = detect_owner_candidate()

    assert result["status"] == "candidate"
    assert result["cluster_size"] == 40


def test_detect_owner_candidate_prefilter_avoids_npz_load(speakers_env, monkeypatch):
    from solstone.apps.speakers import owner as owner_module
    from solstone.apps.speakers.encoder_config import OWNER_BOOTSTRAP_MIN_STMTS
    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    _write_candidate_pool(
        env.journal,
        [
            _candidate_record(
                1,
                [_source_segment("20240101", "090000_300", stream="mic")],
                n_intervals=1,
            )
        ],
    )

    def fail_materialize(*args, **kwargs):
        raise AssertionError("prefilter opened segment embeddings")

    monkeypatch.setattr(
        owner_module,
        "_expand_owner_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("expanded")),
    )
    monkeypatch.setattr(owner_module, "_routes_helpers", fail_materialize)
    monkeypatch.setattr(owner_module, "load_npz", fail_materialize)

    result = detect_owner_candidate()

    assert result["status"] == "low_quality"
    assert result["source"] == "candidate_pool"
    assert result["low_quality_reason"] == "too_few_stmts"
    assert result["observed_value"] == 1.0
    assert result["threshold_value"] == float(OWNER_BOOTSTRAP_MIN_STMTS)
    assert result["segments_available"] == 1
    assert result["embeddings_available"] == 1


def test_detect_owner_candidate_round_robin_prevents_stream_starvation(
    speakers_env, monkeypatch
):
    from solstone.apps.speakers import owner as owner_module
    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    rng = np.random.default_rng(7)
    source_segments: list[dict[str, object]] = []
    for idx in range(4):
        segment_key = f"09{idx:02d}00_300"
        _write_labeled_segment(
            env,
            "20240101",
            segment_key,
            {1: _owner_embeddings(20, rng)},
            stream="a_stream",
        )
        source_segments.append(
            _source_segment("20240101", segment_key, stream="a_stream")
        )
    _write_labeled_segment(
        env,
        "20240101",
        "100000_300",
        {1: _owner_embeddings(20, rng)},
        stream="b_stream",
    )
    source_segments.append(_source_segment("20240101", "100000_300", stream="b_stream"))
    _write_candidate_pool(
        env.journal,
        [
            _candidate_record(
                1, source_segments, n_intervals=100, total_duration_s=500.0
            )
        ],
    )
    monkeypatch.setattr(
        owner_module,
        "OWNER_CANDIDATE_EXPANSION_MAX_EMBEDDINGS",
        40,
    )

    result = detect_owner_candidate()

    assert result["status"] == "candidate"
    assert result["cluster_size"] == 40
    assert result["streams_represented"] == 2
    assert result["recommendation"] == "ready"


def test_low_quality_too_few_stmts_from_candidate_pool(speakers_env):
    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _owner_embeddings(10, np.random.default_rng(0))},
        stream="mic",
    )
    _write_candidate_pool(
        env.journal,
        [
            _candidate_record(
                1,
                [_source_segment("20240101", "090000_300", stream="mic")],
                n_intervals=40,
            )
        ],
    )

    result = detect_owner_candidate()

    assert result["status"] == "low_quality"
    assert result["source"] == "candidate_pool"
    assert result["recommendation"] == "low_quality"
    assert result["low_quality_reason"] == "too_few_stmts"
    assert get_current()["voiceprint"]["source"] == "candidate_pool"
    assert not _candidate_path(env.journal).exists()


def test_low_quality_median_duration_too_short_from_candidate_pool(speakers_env):
    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _owner_embeddings(40, np.random.default_rng(0))},
        stream="mic",
        duration_s=0.1,
    )
    _write_candidate_pool(
        env.journal,
        [
            _candidate_record(
                1,
                [_source_segment("20240101", "090000_300", stream="mic")],
                n_intervals=40,
            )
        ],
    )

    result = detect_owner_candidate()

    assert result["status"] == "low_quality"
    assert result["source"] == "candidate_pool"
    assert result["low_quality_reason"] == "median_duration_too_short"
    assert get_current()["voiceprint"]["source"] == "candidate_pool"


def test_low_quality_cluster_too_diffuse_from_candidate_pool(speakers_env):
    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _noise_embeddings(40, np.random.default_rng(0))},
        stream="mic",
        duration_s=5.0,
    )
    _write_candidate_pool(
        env.journal,
        [
            _candidate_record(
                1,
                [_source_segment("20240101", "090000_300", stream="mic")],
                n_intervals=40,
            )
        ],
    )

    result = detect_owner_candidate()

    assert result["status"] == "low_quality"
    assert result["source"] == "candidate_pool"
    assert result["low_quality_reason"] == "cluster_too_diffuse"
    assert get_current()["voiceprint"]["source"] == "candidate_pool"


def test_detect_owner_candidate_skips_noisy_source_segments(speakers_env):
    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    rng = np.random.default_rng(2)
    _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _owner_embeddings(40, rng)},
        stream="mic",
        overlap_fraction=0.20,
    )
    _write_labeled_segment(
        env,
        "20240101",
        "091000_300",
        {1: _owner_embeddings(40, rng)},
        stream="mic",
        overlap_fraction=0.0,
    )
    _write_candidate_pool(
        env.journal,
        [
            _candidate_record(
                1,
                [
                    _source_segment("20240101", "090000_300", stream="mic"),
                    _source_segment("20240101", "091000_300", stream="mic"),
                ],
                n_intervals=80,
                total_duration_s=400.0,
            )
        ],
    )

    result = detect_owner_candidate()

    assert result["status"] == "candidate"
    assert result["cluster_size"] == 40
    assert result["recommendation"] == "single_stream"


def test_detect_owner_candidate_missing_npz_after_wipe_marks_no_cluster(speakers_env):
    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    seg_dir = _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _owner_embeddings(40, np.random.default_rng(1))},
        stream="mic",
    )
    (seg_dir / "mic_audio.npz").unlink()
    _write_candidate_pool(
        env.journal,
        [
            _candidate_record(
                1,
                [_source_segment("20240101", "090000_300", stream="mic")],
                n_intervals=40,
            )
        ],
    )

    result = detect_owner_candidate()

    assert result["status"] == "no_cluster"
    assert result["reason"] == "candidate_no_usable_embeddings"
    assert get_current()["voiceprint"]["status"] == "no_cluster"


def test_detect_owner_candidate_missing_segment_dir_marks_no_cluster(speakers_env):
    import shutil

    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    seg_dir = _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _owner_embeddings(40, np.random.default_rng(1))},
        stream="mic",
    )
    shutil.rmtree(seg_dir)
    _write_candidate_pool(
        env.journal,
        [
            _candidate_record(
                1,
                [_source_segment("20240101", "090000_300", stream="mic")],
                n_intervals=40,
            )
        ],
    )

    result = detect_owner_candidate()

    assert result["status"] == "no_cluster"
    assert result["reason"] == "candidate_no_usable_embeddings"
    assert get_current()["voiceprint"]["status"] == "no_cluster"


def test_detect_owner_candidate_reuses_persisted_candidate(speakers_env, monkeypatch):
    from solstone.apps.speakers import owner as owner_module
    from solstone.apps.speakers.encoder_config import OWNER_THRESHOLD
    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    candidate_path = _candidate_path(env.journal)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    version = np.array("2026-03-19T12:00:00Z")
    np.savez_compressed(
        candidate_path,
        centroid=_normalized(np.array([1.0] + [0.0] * 255, dtype=np.float32)),
        cluster_size=np.array(40, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        version=version,
    )
    update_state(
        "voiceprint",
        {
            "status": "candidate",
            "cluster_size": 40,
            "streams_represented": 2,
            "recommendation": "ready",
            "samples": [{"day": "20240101"}],
        },
    )
    monkeypatch.setattr(
        owner_module,
        "_expand_owner_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("recomputed")),
    )

    result = detect_owner_candidate()

    assert result == {
        "status": "candidate",
        "cluster_size": 40,
        "streams_represented": 2,
        "recommendation": "ready",
        "samples": [{"day": "20240101"}],
    }
    with np.load(candidate_path, allow_pickle=False) as data:
        assert str(np.asarray(data["version"]).item()) == str(version.item())


def test_detect_owner_candidate_confirmed_short_circuit(speakers_env):
    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    _write_confirmed_owner_centroid(env, cluster_size=60)

    result = detect_owner_candidate()

    assert result["status"] == "confirmed"
    assert result["recommendation"] == "confirmed"
    assert result["cluster_size"] == 60
    assert result["samples"] == []
    assert get_current()["voiceprint"]["status"] == "confirmed"
    assert get_current()["voiceprint"]["cluster_size"] == 60


def test_detect_owner_candidate_confirmed_short_circuit_idempotent(
    speakers_env, monkeypatch
):
    from solstone.apps.speakers import owner as owner_module
    from solstone.apps.speakers.owner import detect_owner_candidate

    env = speakers_env()
    _write_confirmed_owner_centroid(env, cluster_size=60)
    update_state(
        "voiceprint",
        {
            "status": "confirmed",
            "cluster_size": 60,
            "confirmed_at": "2026-03-15T12:00:00Z",
        },
    )

    def fail_update_state(*args, **kwargs):
        raise AssertionError("confirmed short-circuit rewrote awareness")

    monkeypatch.setattr(owner_module, "update_state", fail_update_state)

    result = detect_owner_candidate()

    assert result["status"] == "confirmed"
    assert result["cluster_size"] == 60


def test_bootstrap_owner_from_manual_tags_confirms(speakers_env):
    from solstone.apps.speakers.encoder_config import OWNER_THRESHOLD
    from solstone.apps.speakers.owner import bootstrap_owner_from_manual_tags

    env = speakers_env()
    principal_dir = env.create_entity("Self Person", is_principal=True)
    principal_id = "self_person"
    rng = np.random.default_rng(4)
    base = np.zeros((10, 256), dtype=np.float32)
    base[:, 0] = 1.0
    durations = np.full(10, 2.4, dtype=np.float32)
    for idx in range(3):
        embeddings = base + rng.normal(scale=0.01, size=(10, 256)).astype(np.float32)
        _save_manual_owner_tags(
            env,
            principal_id,
            "20240101",
            f"{9 + idx:02d}0000_300",
            embeddings,
            durations_s=durations,
        )

    result = bootstrap_owner_from_manual_tags()

    owner_path = principal_dir / "owner_centroid.npz"
    assert result["status"] == "confirmed"
    assert result["principal_id"] == principal_id
    assert result["cluster_size"] == 30
    assert owner_path.exists()
    with np.load(owner_path, allow_pickle=False) as data:
        assert set(data.files) == {
            "centroid",
            "cluster_size",
            "threshold",
            "last_refreshed_at",
        }
        centroid = data["centroid"]
        cluster_size = int(np.asarray(data["cluster_size"]).item())
        threshold = float(np.asarray(data["threshold"]).item())
        last_refreshed_at = str(np.asarray(data["last_refreshed_at"]).item())
    assert cluster_size == 30
    assert np.isclose(np.linalg.norm(centroid), 1.0)
    assert np.isclose(threshold, OWNER_THRESHOLD)
    assert last_refreshed_at.endswith("Z")
    assert get_current()["voiceprint"]["status"] == "confirmed"


def test_bootstrap_owner_from_manual_tags_too_few_stmts(speakers_env):
    from solstone.apps.speakers.owner import (
        LOW_QUALITY_REASON_TOO_FEW_STMTS,
        bootstrap_owner_from_manual_tags,
    )

    env = speakers_env()
    env.create_entity("Self Person", is_principal=True)
    embeddings = np.zeros((10, 256), dtype=np.float32)
    embeddings[:, 0] = 1.0
    _save_manual_owner_tags(
        env,
        "self_person",
        "20240101",
        "090000_300",
        embeddings,
        durations_s=np.full(10, 2.0, dtype=np.float32),
    )

    result = bootstrap_owner_from_manual_tags()

    assert result["status"] == "low_quality"
    assert result["source"] == "manual_tags"
    assert result["low_quality_reason"] == LOW_QUALITY_REASON_TOO_FEW_STMTS
    assert get_current()["voiceprint"]["source"] == "manual_tags"


def test_bootstrap_owner_from_manual_tags_short_durations(speakers_env):
    from solstone.apps.speakers.owner import (
        LOW_QUALITY_REASON_MEDIAN_DURATION_TOO_SHORT,
        bootstrap_owner_from_manual_tags,
    )

    env = speakers_env()
    env.create_entity("Self Person", is_principal=True)
    base = np.zeros((10, 256), dtype=np.float32)
    base[:, 0] = 1.0
    for idx in range(3):
        _save_manual_owner_tags(
            env,
            "self_person",
            "20240101",
            f"{9 + idx:02d}0000_300",
            base,
            durations_s=np.full(10, 0.3, dtype=np.float32),
        )

    result = bootstrap_owner_from_manual_tags()

    assert result["status"] == "low_quality"
    assert result["source"] == "manual_tags"
    assert result["low_quality_reason"] == LOW_QUALITY_REASON_MEDIAN_DURATION_TOO_SHORT


def test_bootstrap_owner_from_manual_tags_diffuse_cluster(speakers_env):
    from solstone.apps.speakers.owner import (
        LOW_QUALITY_REASON_CLUSTER_TOO_DIFFUSE,
        bootstrap_owner_from_manual_tags,
    )

    env = speakers_env()
    env.create_entity("Self Person", is_principal=True)
    rng = np.random.default_rng(9)
    for idx in range(3):
        _save_manual_owner_tags(
            env,
            "self_person",
            "20240101",
            f"{9 + idx:02d}0000_300",
            _noise_embeddings(10, rng),
            durations_s=np.full(10, 2.0, dtype=np.float32),
        )

    result = bootstrap_owner_from_manual_tags()

    assert result["status"] == "low_quality"
    assert result["source"] == "manual_tags"
    assert result["low_quality_reason"] == LOW_QUALITY_REASON_CLUSTER_TOO_DIFFUSE


def test_manual_tag_overlap_guard_excludes_rows(speakers_env):
    from solstone.apps.speakers.owner import (
        LOW_QUALITY_REASON_TOO_FEW_STMTS,
        bootstrap_owner_from_manual_tags,
        count_manual_tag_embeddings,
    )

    env = speakers_env()
    env.create_entity("Self Person", is_principal=True)
    embeddings = np.zeros((5, 256), dtype=np.float32)
    embeddings[:, 0] = 1.0
    _save_manual_owner_tags(
        env,
        "self_person",
        "20240101",
        "090000_300",
        embeddings,
        durations_s=np.full(5, 2.0, dtype=np.float32),
        overlap_fraction=0.0,
    )
    _save_manual_owner_tags(
        env,
        "self_person",
        "20240101",
        "100000_300",
        embeddings,
        durations_s=np.full(5, 2.0, dtype=np.float32),
        overlap_fraction=0.20,
    )

    assert count_manual_tag_embeddings("self_person") == 5
    result = bootstrap_owner_from_manual_tags()
    assert result["low_quality_reason"] == LOW_QUALITY_REASON_TOO_FEW_STMTS


def test_owner_centroid_schema_parity_between_confirm_and_manual_build(speakers_env):
    from solstone.apps.speakers.encoder_config import OWNER_THRESHOLD
    from solstone.apps.speakers.owner import (
        bootstrap_owner_from_manual_tags,
        clear_owner_provisional_cache,
        confirm_owner_candidate,
    )

    env = speakers_env()
    principal_dir = env.create_entity("Self Person", is_principal=True)
    candidate_path = _candidate_path(env.journal)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    centroid = _normalized(np.array([1.0] + [0.0] * 255, dtype=np.float32))
    np.savez_compressed(
        candidate_path,
        centroid=centroid,
        cluster_size=np.array(40, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        version=np.array("2026-03-19T12:00:00"),
    )

    confirm_owner_candidate()
    owner_path = principal_dir / "owner_centroid.npz"
    with np.load(owner_path, allow_pickle=False) as data:
        confirmed_keys = set(data.files)

    owner_path.unlink()
    clear_owner_provisional_cache("self_person")
    update_state("voiceprint", {"status": "none"})

    base = np.zeros((10, 256), dtype=np.float32)
    base[:, 0] = 1.0
    for idx in range(3):
        _save_manual_owner_tags(
            env,
            "self_person",
            "20240101",
            f"{9 + idx:02d}0000_300",
            base,
            durations_s=np.full(10, 2.0, dtype=np.float32),
        )

    bootstrap_owner_from_manual_tags()
    with np.load(owner_path, allow_pickle=False) as data:
        manual_keys = set(data.files)

    assert (
        confirmed_keys
        == manual_keys
        == {
            "centroid",
            "cluster_size",
            "threshold",
            "last_refreshed_at",
        }
    )


def test_bootstrap_owner_from_manual_tags_is_idempotent(speakers_env):
    from solstone.apps.speakers.owner import bootstrap_owner_from_manual_tags

    env = speakers_env()
    env.create_entity("Self Person", is_principal=True)
    base = np.zeros((10, 256), dtype=np.float32)
    base[:, 0] = 1.0
    for idx in range(3):
        _save_manual_owner_tags(
            env,
            "self_person",
            "20240101",
            f"{9 + idx:02d}0000_300",
            base,
            durations_s=np.full(10, 2.1, dtype=np.float32),
        )

    first = bootstrap_owner_from_manual_tags()
    state_before = dict(get_current()["voiceprint"])
    second = bootstrap_owner_from_manual_tags()

    assert first["status"] == "confirmed"
    assert second["status"] == "confirmed"
    assert second["cluster_size"] == first["cluster_size"]
    assert dict(get_current()["voiceprint"]) == state_before


def test_load_owner_centroid_no_principal(speakers_env):
    from solstone.apps.speakers.owner import load_owner_centroid

    speakers_env()
    assert load_owner_centroid() is None


def test_load_owner_centroid_no_file(speakers_env):
    from solstone.apps.speakers.owner import load_owner_centroid

    env = speakers_env()
    env.create_entity("Self Person", is_principal=True)

    assert load_owner_centroid() is None


def test_load_owner_centroid_success(speakers_env):
    from solstone.apps.speakers.owner import OWNER_THRESHOLD, load_owner_centroid

    env = speakers_env()
    principal_dir = env.create_entity("Self Person", is_principal=True)
    centroid = _normalized(np.array([1.0] + [0.0] * 255, dtype=np.float32))
    np.savez_compressed(
        principal_dir / "owner_centroid.npz",
        centroid=centroid,
        cluster_size=np.array(60, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        last_refreshed_at=np.array("2026-03-15T12:00:00Z"),
    )

    loaded = load_owner_centroid()

    assert loaded is not None
    assert np.allclose(loaded.centroid, centroid)
    assert np.isclose(loaded.threshold, OWNER_THRESHOLD)
    assert loaded.cluster_size == 60
    assert loaded.last_refreshed_at == "2026-03-15T12:00:00Z"
    assert loaded.intra_cosine_p25 is None
    assert loaded.streams == []


def test_owner_detection_ready_not_ready_when_centroid_exists(speakers_env):
    from solstone.apps.speakers.encoder_config import OWNER_THRESHOLD
    from solstone.apps.speakers.owner import owner_detection_ready

    env = speakers_env()
    principal_dir = env.create_entity("Self Person", is_principal=True)
    centroid = _normalized(np.array([1.0] + [0.0] * 255, dtype=np.float32))
    np.savez_compressed(
        principal_dir / "owner_centroid.npz",
        centroid=centroid,
        cluster_size=np.array(60, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        last_refreshed_at=np.array("2026-03-15T12:00:00Z"),
    )

    result = owner_detection_ready()

    assert result["ready"] is False
    assert result["reason"] == "centroid_exists"


def test_owner_detection_ready_not_ready_during_cooldown(speakers_env, monkeypatch):
    from datetime import datetime

    from solstone.apps.speakers import owner as owner_module
    from solstone.apps.speakers.owner import owner_detection_ready

    speakers_env()
    update_state("voiceprint", {"rejected_at": datetime.now().isoformat()})
    monkeypatch.setattr(
        owner_module,
        "detect_owner_candidate",
        lambda: (_ for _ in ()).throw(AssertionError("called detection")),
    )

    result = owner_detection_ready()

    assert result["ready"] is False
    assert result["reason"] == "cooldown"
    assert result["days_remaining"] == 14


def test_owner_detection_ready_reads_persisted_candidate(speakers_env, monkeypatch):
    from solstone.apps.speakers import owner as owner_module
    from solstone.apps.speakers.encoder_config import OWNER_THRESHOLD
    from solstone.apps.speakers.owner import owner_detection_ready

    env = speakers_env()
    candidate_path = _candidate_path(env.journal)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        candidate_path,
        centroid=_normalized(np.array([1.0] + [0.0] * 255, dtype=np.float32)),
        cluster_size=np.array(40, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        version=np.array("2026-03-19T12:00:00Z"),
    )
    update_state(
        "voiceprint",
        {
            "status": "candidate",
            "cluster_size": 40,
            "streams_represented": 2,
            "recommendation": "ready",
            "samples": [{"day": "20240101"}],
        },
    )
    monkeypatch.setattr(
        owner_module,
        "detect_owner_candidate",
        lambda: (_ for _ in ()).throw(AssertionError("called detection")),
    )

    result = owner_detection_ready()

    assert result["ready"] is True
    assert result["reason"] == "candidate_found"
    assert result["cluster_size"] == 40
    assert result["streams_represented"] == 2
    assert result["samples"] == [{"day": "20240101"}]


def test_owner_detection_ready_preserves_single_stream_not_ready(
    speakers_env, monkeypatch
):
    from solstone.apps.speakers import owner as owner_module
    from solstone.apps.speakers.encoder_config import OWNER_THRESHOLD
    from solstone.apps.speakers.owner import owner_detection_ready

    env = speakers_env()
    candidate_path = _candidate_path(env.journal)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        candidate_path,
        centroid=_normalized(np.array([1.0] + [0.0] * 255, dtype=np.float32)),
        cluster_size=np.array(40, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        version=np.array("2026-03-19T12:00:00Z"),
    )
    update_state(
        "voiceprint",
        {
            "status": "candidate",
            "cluster_size": 40,
            "streams_represented": 1,
            "recommendation": "single_stream",
            "samples": [],
        },
    )
    monkeypatch.setattr(
        owner_module,
        "detect_owner_candidate",
        lambda: (_ for _ in ()).throw(AssertionError("called detection")),
    )

    result = owner_detection_ready()

    assert result["ready"] is False
    assert result["reason"] == "single_stream"


def test_owner_detection_ready_no_candidate_data(speakers_env, monkeypatch):
    from solstone.apps.speakers import owner as owner_module
    from solstone.apps.speakers.owner import owner_detection_ready

    speakers_env()
    monkeypatch.setattr(
        owner_module,
        "detect_owner_candidate",
        lambda: (_ for _ in ()).throw(AssertionError("called detection")),
    )

    result = owner_detection_ready()

    assert result == {"ready": False, "reason": "no_candidate"}


def test_owner_detection_ready_cooldown_expired_allows_candidate(
    speakers_env, monkeypatch
):
    from datetime import datetime, timedelta

    from solstone.apps.speakers import owner as owner_module
    from solstone.apps.speakers.encoder_config import OWNER_THRESHOLD
    from solstone.apps.speakers.owner import owner_detection_ready

    env = speakers_env()
    candidate_path = _candidate_path(env.journal)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        candidate_path,
        centroid=_normalized(np.array([1.0] + [0.0] * 255, dtype=np.float32)),
        cluster_size=np.array(40, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        version=np.array("2026-03-19T12:00:00Z"),
    )
    update_state(
        "voiceprint",
        {
            "status": "candidate",
            "rejected_at": (datetime.now() - timedelta(days=15)).isoformat(),
            "cluster_size": 40,
            "streams_represented": 2,
            "recommendation": "ready",
            "samples": [],
        },
    )
    monkeypatch.setattr(
        owner_module,
        "detect_owner_candidate",
        lambda: (_ for _ in ()).throw(AssertionError("called detection")),
    )

    result = owner_detection_ready()

    assert result["ready"] is True


def test_classify_sentences_no_centroid(speakers_env):
    from solstone.apps.speakers.owner import classify_sentences

    env = speakers_env()
    env.create_segment("20240101", "090000_300", ["audio"], num_sentences=2)

    assert classify_sentences("20240101", "test", "090000_300", "audio") == []


def test_classify_sentences_with_centroid(speakers_env):
    from solstone.apps.speakers.owner import OWNER_THRESHOLD, classify_sentences

    env = speakers_env()
    principal_dir = env.create_entity("Self Person", is_principal=True)
    centroid = _normalized(np.array([1.0] + [0.0] * 255, dtype=np.float32))
    np.savez_compressed(
        principal_dir / "owner_centroid.npz",
        centroid=centroid,
        cluster_size=np.array(70, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        last_refreshed_at=np.array("2026-03-15T12:00:00Z"),
    )

    close = _normalized(np.array([0.95, 0.05] + [0.0] * 254, dtype=np.float32))
    far = _normalized(np.array([0.1, 0.99] + [0.0] * 254, dtype=np.float32))
    _write_segment(
        env.journal,
        "20240101",
        "mic",
        "090000_300",
        "audio",
        np.vstack([close, far]),
    )

    results = classify_sentences("20240101", "mic", "090000_300", "audio")

    assert len(results) == 2
    assert results[0]["sentence_id"] == 1
    assert results[0]["is_owner"] is True
    assert results[1]["sentence_id"] == 2
    assert results[1]["is_owner"] is False


def test_api_owner_status_none(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    speakers_env()
    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.get("/app/speakers/api/owner/status")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "none",
        "manual_tags_count": 0,
        "segments_available": 0,
        "segments_with_embeddings": 0,
        "embeddings_available": 0,
        "streams_represented": 0,
        "can_build_from_tags": False,
    }


def test_api_owner_status_needs_detection(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    for idx in range(50):
        env.create_segment(
            "20240101", f"{idx // 12 + 9:02d}{(idx % 12) * 5:02d}00_300", ["audio"]
        )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.get("/app/speakers/api/owner/status")

    data = response.get_json()
    assert response.status_code == 200
    assert data["status"] == "needs_detection"
    assert data["segments_with_embeddings"] == 50
    assert data["segments_available"] == 50
    assert data["embeddings_available"] == 250
    assert data["manual_tags_count"] == 0
    assert data["streams_represented"] == 0
    assert data["can_build_from_tags"] is False


def test_api_owner_status_manual_tags_count(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_entity("Self Person", is_principal=True)
    embeddings = np.zeros((7, 256), dtype=np.float32)
    embeddings[:, 0] = 1.0
    _save_manual_owner_tags(
        env,
        "self_person",
        "20240101",
        "090000_300",
        embeddings,
        durations_s=np.full(7, 2.0, dtype=np.float32),
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.get("/app/speakers/api/owner/status")

    data = response.get_json()
    assert response.status_code == 200
    assert data["status"] == "needs_detection"
    assert data["manual_tags_count"] == 7
    assert data["segments_available"] == 1
    assert data["segments_with_embeddings"] == 1
    assert data["embeddings_available"] == 7
    assert data["streams_represented"] == 1
    assert data["can_build_from_tags"] is False


def test_api_owner_status_candidate(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    speakers_env()
    update_state(
        "voiceprint",
        {
            "status": "candidate",
            "cluster_size": 55,
            "samples": [{"day": "20240101"}],
        },
    )
    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.get("/app/speakers/api/owner/status")

    assert response.status_code == 200
    assert response.get_json()["status"] == "candidate"


def test_api_owner_status_low_quality(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    speakers_env()
    update_state(
        "voiceprint",
        {
            "status": "low_quality",
            "low_quality_reason": "too_few_stmts",
            "observed_value": 5,
            "threshold_value": 30,
            "segments_checked": 1,
            "attempted_at": "2026-03-15T12:00:00",
        },
    )
    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.get("/app/speakers/api/owner/status")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "low_quality",
        "source": "candidate_pool",
        "low_quality_reason": "too_few_stmts",
        "observed_value": 5,
        "threshold_value": 30,
        "manual_tags_count": 0,
        "segments_available": 0,
        "segments_with_embeddings": 0,
        "embeddings_available": 0,
        "streams_represented": 0,
        "can_build_from_tags": False,
    }


def test_api_owner_status_no_cluster(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    speakers_env()
    update_state("voiceprint", {"status": "no_cluster"})
    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.get("/app/speakers/api/owner/status")

    assert response.status_code == 200
    assert response.get_json()["status"] == "no_cluster"


def test_api_owner_status_confirmed(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    speakers_env()
    update_state("voiceprint", {"status": "confirmed"})
    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.get("/app/speakers/api/owner/status")

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "confirmed",
        "centroid_metadata": {
            "cluster_size": 0,
            "streams": [],
            "last_refreshed_at": "",
            "intra_cosine_p25": None,
        },
    }


def test_api_owner_classify_no_centroid(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "090000_300", ["audio"], num_sentences=2)
    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.post(
            "/app/speakers/api/owner/classify",
            json={
                "day": "20240101",
                "stream": "test",
                "segment_key": "090000_300",
                "source": "audio",
            },
        )

    assert response.status_code == 200
    assert response.get_json() == {"sentences": []}


def test_api_owner_confirm(speakers_env):
    from solstone.apps.speakers.owner import OWNER_THRESHOLD
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    principal_dir = env.create_entity("Self Person", is_principal=True)
    candidate_path = _candidate_path(env.journal)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    centroid = _normalized(np.array([1.0] + [0.0] * 255, dtype=np.float32))
    np.savez_compressed(
        candidate_path,
        centroid=centroid,
        cluster_size=np.array(88, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        version=np.array("2026-03-15T12:00:00"),
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.post("/app/speakers/api/owner/confirm")

    assert response.status_code == 200
    assert response.get_json()["status"] == "confirmed"
    assert not candidate_path.exists()
    assert (principal_dir / "owner_centroid.npz").exists()
    assert get_current()["voiceprint"]["status"] == "confirmed"


def test_api_owner_reject(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    candidate_path = _candidate_path(env.journal)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_bytes(b"test")

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.post("/app/speakers/api/owner/reject")

    assert response.status_code == 200
    assert response.get_json() == {"status": "needs_detection"}
    assert not candidate_path.exists()
    assert get_current()["voiceprint"]["status"] == "rejected"


def test_api_owner_detect(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    rng = np.random.default_rng(42)
    _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _owner_embeddings(20, rng)},
        stream="mic",
    )
    _write_labeled_segment(
        env,
        "20240101",
        "091000_300",
        {1: _owner_embeddings(20, rng)},
        stream="sys",
    )
    _write_candidate_pool(
        env.journal,
        [
            _candidate_record(
                1,
                [
                    _source_segment("20240101", "090000_300", stream="mic"),
                    _source_segment("20240101", "091000_300", stream="sys"),
                ],
                n_intervals=40,
            )
        ],
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.post("/app/speakers/api/owner/detect")
        status_response = client.get("/app/speakers/api/owner/status")

    data = response.get_json()
    assert response.status_code == 200
    assert data["status"] == "candidate"
    assert data["cluster_size"] == 40
    assert data["streams_represented"] == 2
    assert data["recommendation"] == "ready"
    assert status_response.status_code == 200
    assert status_response.get_json()["status"] == "candidate"


def test_api_owner_detect_no_pool_does_not_loop_needs_detection(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _owner_embeddings(40, np.random.default_rng(1))},
        stream="mic",
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        first_status = client.get("/app/speakers/api/owner/status")
        detect_response = client.post("/app/speakers/api/owner/detect")
        second_status = client.get("/app/speakers/api/owner/status")

    assert first_status.status_code == 200
    assert first_status.get_json()["status"] == "needs_detection"
    assert detect_response.status_code == 200
    assert detect_response.get_json()["status"] == "no_cluster"
    assert second_status.status_code == 200
    assert second_status.get_json()["status"] != "needs_detection"
    assert second_status.get_json()["status"] == "no_cluster"


def test_api_owner_detect_small_pool_does_not_loop_needs_detection(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _owner_embeddings(12, np.random.default_rng(1))},
        stream="mic",
    )
    _write_candidate_pool(
        env.journal,
        [
            _candidate_record(
                1,
                [_source_segment("20240101", "090000_300", stream="mic")],
                n_intervals=12,
                n_segments=1,
            )
        ],
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        first_status = client.get("/app/speakers/api/owner/status")
        detect_response = client.post("/app/speakers/api/owner/detect")
        second_status = client.get("/app/speakers/api/owner/status")

    assert first_status.status_code == 200
    assert first_status.get_json()["status"] == "needs_detection"
    assert detect_response.status_code == 200
    assert detect_response.get_json()["status"] == "low_quality"
    assert detect_response.get_json()["low_quality_reason"] == "too_few_stmts"
    assert second_status.status_code == 200
    assert second_status.get_json()["status"] != "needs_detection"
    assert second_status.get_json()["status"] == "low_quality"


def test_api_owner_detect_confirmed_centroid_repairs_awareness_no_loop(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _owner_embeddings(40, np.random.default_rng(1))},
        stream="mic",
    )
    _write_confirmed_owner_centroid(env, cluster_size=60)

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        first_status = client.get("/app/speakers/api/owner/status")
        detect_response = client.post("/app/speakers/api/owner/detect")
        second_status = client.get("/app/speakers/api/owner/status")

    assert first_status.status_code == 200
    assert first_status.get_json()["status"] == "needs_detection"
    assert detect_response.status_code == 200
    assert detect_response.get_json()["status"] == "confirmed"
    assert get_current()["voiceprint"]["status"] == "confirmed"
    assert second_status.status_code == 200
    assert second_status.get_json()["status"] != "needs_detection"
    assert second_status.get_json()["status"] == "confirmed"


def test_api_owner_status_does_not_detect_or_materialize_embeddings(
    speakers_env, monkeypatch
):
    from solstone.apps.speakers import owner as owner_module
    from solstone.apps.speakers import routes as speakers_routes
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    _write_labeled_segment(
        env,
        "20240101",
        "090000_300",
        {1: _owner_embeddings(40, np.random.default_rng(1))},
        stream="mic",
    )

    def fail_detect():
        raise AssertionError("status called detect_owner_candidate")

    def fail_materialize(*args, **kwargs):
        raise AssertionError("status materialized embedding arrays")

    monkeypatch.setattr(speakers_routes, "detect_owner_candidate", fail_detect)
    monkeypatch.setattr(owner_module, "load_npz", fail_materialize)

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.get("/app/speakers/api/owner/status")

    assert response.status_code == 200
    assert response.get_json()["status"] == "needs_detection"


def test_confirm_owner_candidate_no_candidate(speakers_env):
    from solstone.apps.speakers.owner import confirm_owner_candidate

    speakers_env()
    result = confirm_owner_candidate()
    assert "error" in result
    assert "No candidate" in result["error"]


def test_confirm_owner_candidate_success(speakers_env):
    from solstone.apps.speakers.owner import OWNER_THRESHOLD, confirm_owner_candidate

    env = speakers_env()
    principal_dir = env.create_entity("Self Person", is_principal=True)
    candidate_path = _candidate_path(env.journal)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    centroid = _normalized(np.array([1.0] + [0.0] * 255, dtype=np.float32))
    np.savez_compressed(
        candidate_path,
        centroid=centroid,
        cluster_size=np.array(88, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        version=np.array("2026-03-19T12:00:00"),
    )

    result = confirm_owner_candidate()

    assert result["status"] == "confirmed"
    assert result["principal_id"] is not None
    assert result["cluster_size"] == 88
    assert not candidate_path.exists()
    assert (principal_dir / "owner_centroid.npz").exists()
    assert get_current()["voiceprint"]["status"] == "confirmed"


def test_reject_owner_candidate(speakers_env):
    from solstone.apps.speakers.owner import reject_owner_candidate

    env = speakers_env()
    candidate_path = _candidate_path(env.journal)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_bytes(b"test")

    result = reject_owner_candidate()

    assert result["status"] == "rejected"
    assert not candidate_path.exists()
    state = get_current()
    assert state["voiceprint"]["status"] == "rejected"
    assert "rejected_at" in state["voiceprint"]


def test_reject_owner_candidate_enforces_detection_cooldown(speakers_env, monkeypatch):
    from solstone.apps.speakers.owner import (
        owner_detection_ready,
        reject_owner_candidate,
    )

    env = speakers_env()
    candidate_path = _candidate_path(env.journal)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_bytes(b"test")

    reject_owner_candidate()
    rejected_at = get_current()["voiceprint"]["rejected_at"]
    assert rejected_at.endswith("Z")

    detection_calls = []

    def fake_detect_owner_candidate():
        detection_calls.append(True)
        return {
            "status": "candidate",
            "recommendation": "ready",
            "cluster_size": 88,
            "streams_represented": 2,
            "samples": [],
        }

    monkeypatch.setattr(
        "solstone.apps.speakers.owner.load_owner_centroid",
        lambda: None,
    )
    monkeypatch.setattr(
        "solstone.apps.speakers.owner.detect_owner_candidate",
        fake_detect_owner_candidate,
    )

    result = owner_detection_ready()

    assert result["ready"] is False
    assert result["reason"] == "cooldown"
    assert result["days_remaining"] == 14
    assert detection_calls == []
