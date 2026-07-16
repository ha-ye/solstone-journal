# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for speakers app - sentence-based embeddings."""

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from flask import Flask

SERVE_AUDIO_DAY = "20240101"
SERVE_AUDIO_STREAM = "test"
SERVE_AUDIO_SEGMENT = "143022_300"
SERVE_AUDIO_SOURCE = "mic_audio"
SERVE_AUDIO_URL = (
    f"/app/speakers/api/serve_audio/{SERVE_AUDIO_DAY}/"
    f"{SERVE_AUDIO_STREAM}/{SERVE_AUDIO_SEGMENT}/{SERVE_AUDIO_SOURCE}.flac"
)
OWNER_DISPLAY_NAME = "Jer Miller"
OWNER_ID = "jer_miller"


def _convey_client(journal_root):
    from solstone.convey import create_app

    app = create_app(str(journal_root))
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def serve_audio_client(tmp_path, monkeypatch):
    from solstone.convey import create_app

    journal = tmp_path / "journal"
    config_dir = journal / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text(
        json.dumps({"setup": {"completed_at": 1700000000000}}) + "\n",
        encoding="utf-8",
    )
    segment_dir = (
        journal
        / "chronicle"
        / SERVE_AUDIO_DAY
        / SERVE_AUDIO_STREAM
        / SERVE_AUDIO_SEGMENT
    )
    segment_dir.mkdir(parents=True)
    (segment_dir / f"{SERVE_AUDIO_SOURCE}.flac").write_bytes(b"fLaC")
    (segment_dir / f"{SERVE_AUDIO_SOURCE}.m4a").write_bytes(b"m4a")
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    app = create_app(str(journal))
    app.config["TESTING"] = True
    return app.test_client(), journal


def _read_action_entries(journal_root):
    """Read journal-level action log entries for today."""
    today = datetime.now().strftime("%Y%m%d")
    log_path = journal_root / "config" / "actions" / f"{today}.jsonl"
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _speakers_client():
    from solstone.apps.speakers.routes import speakers_bp

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)
    return app.test_client()


def _review_payload() -> dict:
    with _speakers_client() as client:
        resp = client.get(
            "/app/speakers/api/review/"
            f"{SERVE_AUDIO_DAY}/{SERVE_AUDIO_STREAM}/"
            f"{SERVE_AUDIO_SEGMENT}/{SERVE_AUDIO_SOURCE}"
        )
    assert resp.status_code == 200
    return resp.get_json()


def _post_assign(speaker: str, sentence_id: int = 1):
    with _speakers_client() as client:
        return client.post(
            "/app/speakers/api/assign-attribution",
            json={
                "day": SERVE_AUDIO_DAY,
                "stream": SERVE_AUDIO_STREAM,
                "segment_key": SERVE_AUDIO_SEGMENT,
                "source": SERVE_AUDIO_SOURCE,
                "sentence_id": sentence_id,
                "speaker": speaker,
            },
        )


def _post_correct(new_speaker: str, sentence_id: int = 1):
    with _speakers_client() as client:
        return client.post(
            "/app/speakers/api/correct-attribution",
            json={
                "day": SERVE_AUDIO_DAY,
                "stream": SERVE_AUDIO_STREAM,
                "segment_key": SERVE_AUDIO_SEGMENT,
                "source": SERVE_AUDIO_SOURCE,
                "sentence_id": sentence_id,
                "new_speaker": new_speaker,
            },
        )


def _labels_path(env) -> Path:
    return (
        env.journal
        / SERVE_AUDIO_DAY
        / SERVE_AUDIO_STREAM
        / SERVE_AUDIO_SEGMENT
        / "talents"
        / "speaker_labels.json"
    )


def _load_labels(env) -> dict:
    with open(_labels_path(env), encoding="utf-8") as f:
        return json.load(f)


def _load_owner_entity(env) -> dict:
    with open(
        env.journal / "entities" / OWNER_ID / "entity.json",
        encoding="utf-8",
    ) as f:
        return json.load(f)


def _voiceprint_row_count(env, entity_id: str = OWNER_ID) -> int:
    with np.load(
        env.journal / "entities" / entity_id / "voiceprints.npz",
        allow_pickle=False,
    ) as data:
        return int(data["embeddings"].shape[0])


def _chronicle_snapshot(journal: Path) -> dict[Path, int]:
    chronicle = journal / "chronicle"
    if not chronicle.exists():
        return {}
    return {path: path.stat().st_mtime_ns for path in sorted(chronicle.rglob("*"))}


def test_api_index_reports_nonzero_coverage_and_months(speakers_env):
    env = speakers_env()
    env.create_segment("20240101", "090000_300", ["mic_audio"])
    env.create_segment("20240101", "100000_300", ["mic_audio"])
    env.create_segment("20240203", "090000_300", ["mic_audio"])
    client = _convey_client(env.journal)

    response = client.get("/app/speakers/api/index")

    assert response.status_code == 200
    assert response.get_json() == {
        "coverage": {"start": "20240101", "end": "20240203"},
        "months": {"202401": 2, "202402": 1},
    }


def test_api_index_month_totals_match_api_stats(speakers_env):
    env = speakers_env()
    env.create_segment("20240101", "090000_300", ["mic_audio"])
    env.create_segment("20240102", "090000_300", ["mic_audio"])
    client = _convey_client(env.journal)

    response = client.get("/app/speakers/api/index")

    assert response.status_code == 200
    body = response.get_json()
    for month, total in body["months"].items():
        month_response = client.get(f"/app/speakers/api/stats/{month}")
        assert month_response.status_code == 200
        assert total == sum(month_response.get_json().values())


def test_api_index_empty_journal(speakers_env):
    env = speakers_env()
    client = _convey_client(env.journal)

    response = client.get("/app/speakers/api/index")

    assert response.status_code == 200
    assert response.get_json() == {"coverage": None, "months": {}}


def test_api_index_is_read_only(speakers_env):
    env = speakers_env()
    env.create_segment("20240101", "090000_300", ["mic_audio"])
    before = _chronicle_snapshot(env.journal)
    client = _convey_client(env.journal)

    response = client.get("/app/speakers/api/index")

    assert response.status_code == 200
    assert _chronicle_snapshot(env.journal) == before


def _save_principal_manual_tags(
    env,
    principal_id: str,
    count: int,
    *,
    day: str = "20240101",
    segment_key: str = "143022_300",
    source: str = "mic_audio",
    embeddings: np.ndarray | None = None,
) -> np.ndarray:
    from solstone.apps.speakers.routes import _save_voiceprint

    if embeddings is None:
        embeddings = np.zeros((count, 256), dtype=np.float32)
        embeddings[:, 0] = 1.0
    env.create_segment(
        day,
        segment_key,
        [source],
        num_sentences=count,
        embeddings=embeddings,
    )
    env.create_speaker_labels(
        day,
        segment_key,
        [
            {
                "sentence_id": idx,
                "speaker": principal_id,
                "confidence": "high",
                "method": "user_assigned",
            }
            for idx in range(1, count + 1)
        ],
    )
    for idx, embedding in enumerate(embeddings, start=1):
        _save_voiceprint(
            principal_id,
            embedding,
            day,
            segment_key,
            source,
            idx,
            stream="test",
        )
    return embeddings


def _write_prefilter_failing_candidate_pool(env) -> Path:
    path = env.journal / "awareness" / "speaker_candidates.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "next_id": 2,
                "candidates": [
                    {
                        "cand_id": 1,
                        "centroid": [1.0] + [0.0] * 255,
                        "n_segments": 1,
                        "n_intervals": 1,
                        "total_duration_s": 5.0,
                        "source_segments": [
                            {
                                "day": SERVE_AUDIO_DAY,
                                "stream": SERVE_AUDIO_STREAM,
                                "segment_key": SERVE_AUDIO_SEGMENT,
                                "source": SERVE_AUDIO_SOURCE,
                                "cluster_label": 1,
                            }
                        ],
                        "confirmed_entity": None,
                        "status": "pending",
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_normalize_embedding():
    """Test L2 normalization of embeddings."""
    from solstone.apps.speakers.routes import _normalize_embedding

    emb = np.array([3.0, 4.0, 0.0] + [0.0] * 253, dtype=np.float32)
    normalized = _normalize_embedding(emb)

    assert normalized is not None
    assert np.isclose(np.linalg.norm(normalized), 1.0)
    # 3-4-5 right triangle, normalized to unit vector
    assert np.isclose(normalized[0], 0.6)
    assert np.isclose(normalized[1], 0.8)


def test_normalize_embedding_zero_vector():
    """Test that zero vector returns None."""
    from solstone.apps.speakers.routes import _normalize_embedding

    emb = np.zeros(256, dtype=np.float32)
    normalized = _normalize_embedding(emb)

    assert normalized is None


def test_parse_time_to_seconds():
    """Test time string parsing."""
    from solstone.apps.speakers.routes import _parse_time_to_seconds

    assert _parse_time_to_seconds("00:00:00") == 0
    assert _parse_time_to_seconds("00:01:30") == 90
    assert _parse_time_to_seconds("01:00:00") == 3600
    assert _parse_time_to_seconds("14:30:22") == 52222


def test_scan_segment_embeddings_empty(speakers_env):
    """Test scanning when no embeddings exist."""
    from solstone.apps.speakers.routes import _scan_segment_embeddings

    env = speakers_env()

    # Create a day dir but no segments
    day_dir = env.journal / "20240101"
    day_dir.mkdir()

    segments = _scan_segment_embeddings("20240101")
    assert segments == []


def test_scan_segment_embeddings_with_data(speakers_env):
    """Test scanning when embeddings and speakers exist."""
    from solstone.apps.speakers.routes import _scan_segment_embeddings

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio", "sys_audio"])
    env.create_speakers_json("20240101", "143022_300", ["Alice", "Bob"])

    segments = _scan_segment_embeddings("20240101")
    assert len(segments) == 1
    assert segments[0]["key"] == "143022_300"
    assert segments[0]["start"] == "14:30"
    assert segments[0]["end"] == "14:35"
    assert segments[0]["duration"] == 300
    assert set(segments[0]["sources"]) == {"mic_audio", "sys_audio"}
    assert segments[0]["speakers"] == ["Alice", "Bob"]
    assert segments[0]["speaker_count"] == 2


def test_scan_segment_embeddings_plain_audio(speakers_env):
    """Test scanning finds plain 'audio' source (not just *_audio pattern)."""
    from solstone.apps.speakers.routes import _scan_segment_embeddings

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["audio"])
    env.create_speakers_json("20240101", "143022_300", ["Alice", "Bob"])

    segments = _scan_segment_embeddings("20240101")
    assert len(segments) == 1
    assert segments[0]["sources"] == ["audio"]


def test_load_sentences(speakers_env):
    """Test loading sentences with embeddings."""
    from solstone.apps.speakers.routes import _load_sentences

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"], num_sentences=3)

    sentences, emb_data = _load_sentences(
        "20240101", "143022_300", "mic_audio", stream="test"
    )

    assert len(sentences) == 3
    assert sentences[0]["id"] == 1
    assert sentences[0]["text"] == "This is sentence 1."
    assert sentences[0]["offset"] == 0
    assert sentences[0]["has_embedding"] is True

    assert emb_data is not None
    embeddings, statement_ids, durations_s = emb_data
    assert embeddings.shape == (3, 256)
    assert len(statement_ids) == 3
    assert durations_s is None


def test_load_sentences_no_transcript(speakers_env):
    """Test loading sentences when no transcript exists."""
    from solstone.apps.speakers.routes import _load_sentences

    env = speakers_env()

    # Create day dir but no segment
    day_dir = env.journal / "20240101" / "test" / "143022_300"
    day_dir.mkdir(parents=True)

    sentences, emb_data = _load_sentences(
        "20240101", "143022_300", "mic_audio", stream="test"
    )
    assert sentences == []
    assert emb_data is None


def test_get_sentence_embedding(speakers_env):
    """Test getting a specific sentence's embedding."""
    from solstone.apps.speakers.routes import _get_sentence_embedding

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"], num_sentences=5)

    # Get embedding for sentence 3
    emb = _get_sentence_embedding(
        "20240101", "143022_300", "mic_audio", 3, stream="test"
    )

    assert emb is not None
    assert emb.shape == (256,)
    assert np.isclose(np.linalg.norm(emb), 1.0)


def test_get_sentence_embedding_not_found(speakers_env):
    """Test getting embedding for non-existent sentence."""
    from solstone.apps.speakers.routes import _get_sentence_embedding

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"], num_sentences=3)

    # Try to get embedding for sentence that doesn't exist
    emb = _get_sentence_embedding(
        "20240101", "143022_300", "mic_audio", 99, stream="test"
    )
    assert emb is None


def test_load_entity_voiceprints_file(speakers_env):
    """Test loading voiceprints from consolidated file."""
    from solstone.apps.speakers.routes import _load_entity_voiceprints_file

    env = speakers_env()
    env.create_entity(
        "Bob Test",
        voiceprints=[
            ("20240101", "120000_300", "mic_audio", 1),
            ("20240102", "130000_300", "audio", 2),
        ],
    )

    result = _load_entity_voiceprints_file("bob_test")

    assert result is not None
    embeddings, metadata_list = result
    assert embeddings.shape == (2, 256)
    assert len(metadata_list) == 2
    assert metadata_list[0]["day"] == "20240101"
    assert metadata_list[1]["day"] == "20240102"
    assert metadata_list[0]["source"] == "mic_audio"
    assert metadata_list[1]["source"] == "audio"


def test_load_entity_voiceprints_file_not_found(speakers_env):
    """Test loading voiceprints for non-existent entity returns None."""
    from solstone.apps.speakers.routes import _load_entity_voiceprints_file

    env = speakers_env()

    # Create entities dir but no entity
    entities_dir = env.journal / "entities"
    entities_dir.mkdir(parents=True)

    result = _load_entity_voiceprints_file("nobody")
    assert result is None


def test_save_voiceprint(speakers_env):
    """Test saving voiceprint to consolidated voiceprints.npz."""
    import json

    from solstone.apps.speakers.routes import _save_voiceprint
    from solstone.think.utils import segment_start_ts_ms

    env = speakers_env()

    # Create entities dir
    entities_dir = env.journal / "entities"
    entities_dir.mkdir(parents=True)

    emb = np.array([1.0, 0.0, 0.0] + [0.0] * 253, dtype=np.float32)

    path = _save_voiceprint("john_doe", emb, "20240101", "143022_300", "mic_audio", 5)

    assert path.exists()
    assert path.name == "voiceprints.npz"
    assert "john_doe" in str(path.parent)

    # Verify format content
    data = np.load(path)
    assert "embeddings" in data
    assert "metadata" in data
    assert data["embeddings"].shape == (1, 256)

    # Verify metadata
    metadata = json.loads(data["metadata"][0])
    assert metadata["day"] == "20240101"
    assert metadata["segment_key"] == "143022_300"
    assert metadata["source"] == "mic_audio"
    assert metadata["sentence_id"] == 5
    assert "added_at" in metadata
    assert metadata["last_seen_ts"] == segment_start_ts_ms("20240101", "143022_300")


def test_save_voiceprint_appends(speakers_env):
    """Test saving multiple voiceprints appends to existing file."""
    import json

    from solstone.apps.speakers.routes import _save_voiceprint

    env = speakers_env()

    # Create entities dir
    entities_dir = env.journal / "entities"
    entities_dir.mkdir(parents=True)

    emb1 = np.array([1.0, 0.0, 0.0] + [0.0] * 253, dtype=np.float32)
    emb2 = np.array([0.0, 1.0, 0.0] + [0.0] * 253, dtype=np.float32)

    # Save first voiceprint
    path = _save_voiceprint("john_doe", emb1, "20240101", "143022_300", "mic_audio", 5)

    # Save second voiceprint
    path2 = _save_voiceprint("john_doe", emb2, "20240102", "150000_300", "audio", 3)

    assert path == path2  # Same file

    # Verify both are in the file
    data = np.load(path)
    assert data["embeddings"].shape == (2, 256)
    assert len(data["metadata"]) == 2

    meta1 = json.loads(data["metadata"][0])
    meta2 = json.loads(data["metadata"][1])
    assert meta1["day"] == "20240101"
    assert meta2["day"] == "20240102"


def test_check_owner_contamination_uses_provisional_centroid(speakers_env):
    from solstone.apps.speakers.routes import _check_owner_contamination

    env = speakers_env()
    env.create_entity("Self Person", is_principal=True)
    _save_principal_manual_tags(env, "self_person", 5)

    similar = np.zeros(256, dtype=np.float32)
    similar[0] = 1.0
    dissimilar = np.zeros(256, dtype=np.float32)
    dissimilar[1] = 1.0

    assert _check_owner_contamination(similar) is True
    assert _check_owner_contamination(dissimilar) is False


def test_check_owner_contamination_below_provisional_min_tags(speakers_env):
    from solstone.apps.speakers.routes import _check_owner_contamination

    env = speakers_env()
    env.create_entity("Self Person", is_principal=True)
    _save_principal_manual_tags(env, "self_person", 4)

    similar = np.zeros(256, dtype=np.float32)
    similar[0] = 1.0
    dissimilar = np.zeros(256, dtype=np.float32)
    dissimilar[1] = 1.0

    assert _check_owner_contamination(similar) is False
    assert _check_owner_contamination(dissimilar) is False


def test_check_owner_contamination_invalidates_cached_provisional_count(speakers_env):
    from solstone.apps.speakers.routes import _check_owner_contamination

    env = speakers_env()
    env.create_entity("Self Person", is_principal=True)
    _save_principal_manual_tags(env, "self_person", 5)

    similar = np.zeros(256, dtype=np.float32)
    similar[0] = 1.0

    assert _check_owner_contamination(similar) is True

    labels_path = (
        env.journal
        / "chronicle"
        / "20240101"
        / "test"
        / "143022_300"
        / "talents"
        / "speaker_labels.json"
    )
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    labels["labels"][0]["speaker"] = "other_person"
    labels_path.write_text(json.dumps(labels, indent=2), encoding="utf-8")

    assert _check_owner_contamination(similar) is False


def test_check_owner_contamination_prefers_confirmed_centroid(speakers_env):
    from solstone.apps.speakers.encoder_config import OWNER_THRESHOLD
    from solstone.apps.speakers.routes import _check_owner_contamination

    env = speakers_env()
    principal_dir = env.create_entity("Self Person", is_principal=True)
    _save_principal_manual_tags(env, "self_person", 5)

    similar = np.zeros(256, dtype=np.float32)
    similar[0] = 1.0
    confirmed = np.zeros(256, dtype=np.float32)
    confirmed[1] = 1.0

    assert _check_owner_contamination(similar) is True

    np.savez_compressed(
        principal_dir / "owner_centroid.npz",
        centroid=confirmed,
        cluster_size=np.array(30, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        last_refreshed_at=np.array("2026-04-25T12:00:00Z"),
    )

    assert _check_owner_contamination(similar) is False
    assert _check_owner_contamination(confirmed) is True


def test_api_owner_build_from_tags(speakers_env):
    from solstone.apps.speakers.encoder_config import OWNER_BOOTSTRAP_MIN_STMTS
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    principal_dir = env.create_entity("Self Person", is_principal=True)
    batch_count = 3
    batch_size = OWNER_BOOTSTRAP_MIN_STMTS // batch_count
    seeded_count = 0
    for idx in range(batch_count):
        count = (
            OWNER_BOOTSTRAP_MIN_STMTS - seeded_count
            if idx == batch_count - 1
            else batch_size
        )
        seeded_count += count
        _save_principal_manual_tags(
            env,
            "self_person",
            count,
            day="20240101",
            segment_key=f"{9 + idx:02d}0000_300",
            source="audio",
        )

    centroid_path = principal_dir / "owner_centroid.npz"
    assert not centroid_path.exists()

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.post("/app/speakers/api/owner/build-from-tags")

    data = resp.get_json()
    assert resp.status_code == 200
    assert data["status"] == "confirmed"
    assert data["principal_id"] == "self_person"
    assert data["cluster_size"] == seeded_count
    assert centroid_path.exists()
    with np.load(centroid_path, allow_pickle=False) as centroid:
        assert int(np.asarray(centroid["cluster_size"]).item()) == seeded_count


def test_api_owner_bootstrap_full_loop_from_stubbed_labels(speakers_env):
    from solstone.apps.speakers.encoder_config import OWNER_BOOTSTRAP_MIN_STMTS
    from solstone.apps.speakers.routes import speakers_bp
    from solstone.apps.speakers.tests.test_owner import (
        _candidate_record,
        _source_segment,
        _write_candidate_pool,
    )

    env = speakers_env()
    env.set_identity(preferred=OWNER_DISPLAY_NAME)
    embeddings = np.zeros((OWNER_BOOTSTRAP_MIN_STMTS, 256), dtype=np.float32)
    embeddings[:, 0] = 1.0
    env.create_segment(
        SERVE_AUDIO_DAY,
        SERVE_AUDIO_SEGMENT,
        [SERVE_AUDIO_SOURCE],
        embeddings=embeddings,
    )
    env.create_speaker_labels(
        SERVE_AUDIO_DAY,
        SERVE_AUDIO_SEGMENT,
        [],
        metadata={"skipped": True, "reason": "pre-bootstrap owner"},
    )
    _write_candidate_pool(
        env.journal,
        [
            _candidate_record(
                1,
                [
                    _source_segment(
                        SERVE_AUDIO_DAY,
                        SERVE_AUDIO_SEGMENT,
                        stream=SERVE_AUDIO_STREAM,
                        source=SERVE_AUDIO_SOURCE,
                    )
                ],
                n_intervals=1,
            )
        ],
    )

    assert _load_labels(env) == {
        "labels": [],
        "skipped": True,
        "reason": "pre-bootstrap owner",
    }
    centroid_path = env.journal / "entities" / OWNER_ID / "owner_centroid.npz"
    assert not centroid_path.exists()

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        detect_resp = client.post("/app/speakers/api/owner/detect")
        detect = detect_resp.get_json()

        assert detect_resp.status_code == 200
        assert detect["status"] == "low_quality"
        assert detect["next_step"] == "seed_manual_tags"
        assert detect["manual_tags_count"] == 0
        assert detect["segments_available"] == 1
        assert detect["embeddings_available"] == 1

        for sentence_id in range(1, OWNER_BOOTSTRAP_MIN_STMTS + 1):
            tag_resp = client.post(
                "/app/speakers/api/owner/tag-cli",
                json={
                    "day": SERVE_AUDIO_DAY,
                    "stream": SERVE_AUDIO_STREAM,
                    "segment_key": SERVE_AUDIO_SEGMENT,
                    "source": SERVE_AUDIO_SOURCE,
                    "sentence_id": sentence_id,
                },
            )
            assert tag_resp.status_code == 200
            assert tag_resp.get_json()["status"] == "assigned"

        labels = _load_labels(env)
        assert labels["labels"] == [
            {
                "sentence_id": sentence_id,
                "speaker": OWNER_ID,
                "confidence": "high",
                "method": "user_assigned",
            }
            for sentence_id in range(1, OWNER_BOOTSTRAP_MIN_STMTS + 1)
        ]
        assert labels["skipped"] is True
        assert labels["reason"] == "pre-bootstrap owner"

        assert centroid_path.exists()
        with np.load(centroid_path, allow_pickle=False) as centroid:
            assert set(centroid.files) == {
                "centroid",
                "cluster_size",
                "threshold",
                "last_refreshed_at",
            }
            assert int(np.asarray(centroid["cluster_size"]).item()) == (
                OWNER_BOOTSTRAP_MIN_STMTS
            )

        build_resp = client.post("/app/speakers/api/owner/build-from-tags")
        build = build_resp.get_json()

    assert build_resp.status_code == 200
    assert build["status"] == "confirmed"
    assert build["principal_id"] == OWNER_ID
    assert build["cluster_size"] == OWNER_BOOTSTRAP_MIN_STMTS


def test_load_embeddings_file(speakers_env):
    """Test loading embeddings from NPZ file."""
    from solstone.apps.speakers.routes import _load_embeddings_file

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"], num_sentences=3)

    npz_path = env.journal / "20240101" / "test" / "143022_300" / "mic_audio.npz"
    result = _load_embeddings_file(npz_path)

    assert result is not None
    embeddings, statement_ids, durations_s = result
    assert embeddings.shape == (3, 256)
    assert len(statement_ids) == 3
    assert durations_s is None


def test_load_embeddings_file_with_durations(speakers_env):
    """Test loading embeddings from NPZ file with durations."""
    from solstone.apps.speakers.routes import _load_embeddings_file

    env = speakers_env()
    embeddings = np.eye(3, 256, dtype=np.float32)
    statement_ids = np.arange(1, 4, dtype=np.int32)
    durations_s = np.array([1.6, 2.1, 2.8], dtype=np.float32)
    npz_path = env.journal / "20240101" / "test" / "143022_300" / "mic_audio.npz"
    npz_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        npz_path,
        embeddings=embeddings,
        statement_ids=statement_ids,
        durations_s=durations_s,
    )

    result = _load_embeddings_file(npz_path)

    assert result is not None
    loaded_embeddings, loaded_ids, loaded_durations = result
    assert loaded_embeddings.shape == (3, 256)
    assert np.array_equal(loaded_ids, statement_ids)
    assert loaded_durations is not None
    assert np.allclose(loaded_durations, durations_s)


def test_load_embeddings_file_not_found():
    """Test loading non-existent embeddings file returns None."""
    from pathlib import Path

    from solstone.apps.speakers.routes import _load_embeddings_file

    result = _load_embeddings_file(Path("/nonexistent/file.npz"))

    assert result is None


def test_load_segment_speakers(speakers_env):
    """Test loading speakers from speakers.json."""
    from solstone.apps.speakers.routes import _load_segment_speakers

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_speakers_json("20240101", "143022_300", ["Alice", "Bob", "Charlie"])

    segment_dir = env.journal / "20240101" / "test" / "143022_300"
    speakers = _load_segment_speakers(segment_dir)

    assert speakers == ["Alice", "Bob", "Charlie"]


def test_load_segment_speakers_not_found(speakers_env):
    """Test loading speakers returns empty list when file missing."""
    from solstone.apps.speakers.routes import _load_segment_speakers

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    # No speakers.json created

    segment_dir = env.journal / "20240101" / "test" / "143022_300"
    speakers = _load_segment_speakers(segment_dir)

    assert speakers == []


def test_load_segment_speakers_invalid_json(speakers_env):
    """Test loading speakers returns empty list for invalid JSON."""
    from solstone.apps.speakers.routes import _load_segment_speakers

    env = speakers_env()
    segment_dir = env.journal / "20240101" / "test" / "143022_300"
    agents_dir = segment_dir / "talents"
    agents_dir.mkdir(parents=True)

    # Write invalid JSON
    speakers_path = agents_dir / "speakers.json"
    speakers_path.write_text("not valid json")

    speakers = _load_segment_speakers(segment_dir)
    assert speakers == []


def test_load_segment_speakers_not_list(speakers_env):
    """Test loading speakers returns empty list when JSON is not a list."""
    import json

    from solstone.apps.speakers.routes import _load_segment_speakers

    env = speakers_env()
    segment_dir = env.journal / "20240101" / "test" / "143022_300"
    agents_dir = segment_dir / "talents"
    agents_dir.mkdir(parents=True)

    # Write object instead of list
    speakers_path = agents_dir / "speakers.json"
    speakers_path.write_text(json.dumps({"speaker": "Alice"}))

    speakers = _load_segment_speakers(segment_dir)
    assert speakers == []


def test_scan_segment_embeddings_without_speakers(speakers_env):
    """Test that segments without speakers.json are included with empty speakers."""
    from solstone.apps.speakers.routes import _scan_segment_embeddings

    env = speakers_env()
    # Create segment with embeddings but NO speakers.json
    env.create_segment("20240101", "143022_300", ["mic_audio"])

    segments = _scan_segment_embeddings("20240101")
    assert len(segments) == 1
    assert segments[0]["key"] == "143022_300"
    assert segments[0]["speakers"] == []
    assert segments[0]["speaker_count"] == 0


def test_scan_segment_embeddings_single_speaker(speakers_env):
    """Test that segments with 1 speaker are included."""
    from solstone.apps.speakers.routes import _scan_segment_embeddings

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_speakers_json("20240101", "143022_300", ["OnlyAlice"])  # Just 1 speaker

    segments = _scan_segment_embeddings("20240101")
    assert len(segments) == 1
    assert segments[0]["speakers"] == ["OnlyAlice"]
    assert segments[0]["speaker_count"] == 1


def test_scan_segment_embeddings_empty_speakers(speakers_env):
    """Test that segments with empty speakers.json are included."""
    from solstone.apps.speakers.routes import _scan_segment_embeddings

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_speakers_json("20240101", "143022_300", [])  # No speakers

    segments = _scan_segment_embeddings("20240101")
    assert len(segments) == 1
    assert segments[0]["speakers"] == []
    assert segments[0]["speaker_count"] == 0


def test_scan_segment_embeddings_includes_speaker_data(speakers_env):
    """Test that segments include speaker names and count."""
    from solstone.apps.speakers.routes import _scan_segment_embeddings

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_speakers_json("20240101", "143022_300", ["Alice", "Bob"])

    segments = _scan_segment_embeddings("20240101")

    assert len(segments) == 1
    assert segments[0]["speakers"] == ["Alice", "Bob"]
    assert segments[0]["speaker_count"] == 2


def test_api_speakers_empty_when_no_speakers_json(speakers_env):
    """Test /api/speakers/ returns empty matched/unmatched when no speakers.json."""
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    # No speakers.json created

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.get("/app/speakers/api/speakers/20240101/test/143022_300")
        assert response.status_code == 200
        data = response.get_json()
        assert data["matched"] == []
        assert data["unmatched"] == []


def test_discovery_identify_route_is_idempotent_after_success(
    speakers_env, monkeypatch
):
    from solstone.apps.speakers import routes
    from solstone.apps.speakers.routes import speakers_bp

    speakers_env()
    calls = {"count": 0}

    def fake_identify(cluster_id, name):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "status": "identified",
                "entity_id": "bob_smith",
                "entity_name": "Bob Smith",
                "entity_created": True,
                "voiceprints_saved": 20,
                "segments_updated": 4,
                "sentences_attributed": 20,
            }
        return {"error": "No discovery scan results. Run scan first."}

    monkeypatch.setattr(routes, "identify_cluster", fake_identify)
    monkeypatch.setattr(
        routes,
        "load_resolved_cluster",
        lambda cluster_id: {"entity_id": "bob_smith", "label": "Bob Smith"},
    )
    monkeypatch.setattr(routes, "log_app_action", lambda **kwargs: None)

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        first = client.post(
            "/app/speakers/api/discovery/identify",
            json={"cluster_id": 0, "name": "Bob Smith"},
        )
        second = client.post(
            "/app/speakers/api/discovery/identify",
            json={"cluster_id": 0, "name": "Bob Smith"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json()["entity_id"] == "bob_smith"
    assert second.get_json()["voiceprints_saved"] == 0


def test_serve_audio_sets_flac_mimetype(serve_audio_client):
    """Serve audio endpoint returns FLAC mimetype for sample playback."""
    client, _journal = serve_audio_client

    response = client.get(SERVE_AUDIO_URL)

    assert response.status_code == 200
    assert response.mimetype == "audio/flac"


def test_serve_audio_sets_registered_mimetype(serve_audio_client):
    """Serve audio endpoint returns the registered mimetype for sample playback."""
    client, _journal = serve_audio_client

    response = client.get(
        f"/app/speakers/api/serve_audio/{SERVE_AUDIO_DAY}/"
        f"{SERVE_AUDIO_STREAM}/{SERVE_AUDIO_SEGMENT}/{SERVE_AUDIO_SOURCE}.m4a"
    )

    assert response.status_code == 200
    assert response.mimetype == "audio/mp4"


def test_serve_audio_unregistered_extension_raises(serve_audio_client):
    """Serve audio refuses existing files with unregistered extensions."""
    client, journal = serve_audio_client
    segment_dir = (
        journal
        / "chronicle"
        / SERVE_AUDIO_DAY
        / SERVE_AUDIO_STREAM
        / SERVE_AUDIO_SEGMENT
    )
    (segment_dir / f"{SERVE_AUDIO_SOURCE}.aac").write_bytes(b"aac")

    with pytest.raises(
        ValueError,
        match=r"unregistered media extension for serve_audio: \.aac",
    ):
        client.get(
            f"/app/speakers/api/serve_audio/{SERVE_AUDIO_DAY}/"
            f"{SERVE_AUDIO_STREAM}/{SERVE_AUDIO_SEGMENT}/{SERVE_AUDIO_SOURCE}.aac"
        )


def test_serve_audio_path_traversal_is_forbidden(serve_audio_client):
    """A rel_path resolving to a real file outside the day dir is refused 403."""
    client, journal = serve_audio_client
    chronicle = journal / "chronicle"
    # Real file OUTSIDE the day dir, reachable via ../ from it.
    (chronicle / "leak.flac").write_bytes(b"secret")

    response = client.get(
        f"/app/speakers/api/serve_audio/{SERVE_AUDIO_DAY}/../leak.flac"
    )

    assert response.status_code == 403
    assert response.get_json()["reason_code"] == "invalid_path"


def test_serve_audio_malformed_day_returns_404(serve_audio_client):
    """A day segment that doesn't match the YYYYMMDD regex returns 404."""
    client, _journal = serve_audio_client

    response = client.get("/app/speakers/api/serve_audio/notadate/foo")

    assert response.status_code == 404


def test_confirm_attribution_rejects_escaping_stream(speakers_env):
    """Confirm attribution rejects stream traversal before creating directories."""
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.post(
            "/app/speakers/api/confirm-attribution",
            json={
                "day": "20240101",
                "stream": "../../outside",
                "segment_key": "143022_300",
                "source": "mic_audio",
                "sentence_id": 0,
            },
        )

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_segment_or_stream"
    assert not (env.journal / "outside").exists()


def test_correct_attribution_rejects_escaping_segment(speakers_env):
    """Correct attribution rejects segment traversal before creating directories."""
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.post(
            "/app/speakers/api/correct-attribution",
            json={
                "day": "20240101",
                "stream": "test",
                "segment_key": "143022_300/../../../outside",
                "source": "mic_audio",
                "sentence_id": 0,
                "new_speaker": "alice_test",
            },
        )

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_segment_or_stream"
    assert not (env.journal / "outside").exists()


def test_assign_attribution_rejects_escaping_stream(speakers_env):
    """Assign attribution rejects stream traversal before creating directories."""
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.post(
            "/app/speakers/api/assign-attribution",
            json={
                "day": "20240101",
                "stream": "../../outside",
                "segment_key": "143022_300",
                "source": "mic_audio",
                "sentence_id": 0,
                "speaker": "alice_test",
            },
        )

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_segment_or_stream"
    assert not (env.journal / "outside").exists()


def test_attribute_segment_rejects_escaping_segment(speakers_env):
    """Attribute segment rejects segment traversal before creating directories."""
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        response = client.post(
            "/app/speakers/api/attribute-segment",
            json={
                "day": "20240101",
                "stream": "test",
                "segment": "../../outside",
            },
        )

    # Pre-validation guards both the attribute_segment and direct get_segment_path
    # mkdir sinks.
    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_segment_or_stream"
    assert not (env.journal / "outside").exists()


def test_get_journal_principal(speakers_env):
    """Test get_journal_principal returns the principal entity."""
    from solstone.think.entities.journal import get_journal_principal

    env = speakers_env()
    # Create some entities, one as principal
    env.create_entity("Alice Test")
    env.create_entity("Self Person", is_principal=True)
    env.create_entity("Bob Test")

    principal = get_journal_principal()
    assert principal is not None
    assert principal["name"] == "Self Person"
    assert principal["is_principal"] is True


def test_get_journal_principal_none(speakers_env):
    """Test get_journal_principal returns None when no principal exists."""
    from solstone.think.entities.journal import get_journal_principal

    env = speakers_env()
    # Create entities without principal
    env.create_entity("Alice Test")
    env.create_entity("Bob Test")

    principal = get_journal_principal()
    assert principal is None


def test_api_review_with_labels(speakers_env):
    """Review endpoint returns sentences with speaker label data."""
    from flask import Flask

    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_entity("Alice Test")
    env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "alice_test",
                "confidence": "high",
                "method": "acoustic",
            },
            {
                "sentence_id": 2,
                "speaker": "alice_test",
                "confidence": "medium",
                "method": "acoustic",
            },
            {"sentence_id": 3, "speaker": None, "confidence": None, "method": None},
        ],
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.get("/app/speakers/api/review/20240101/test/143022_300/mic_audio")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_labels"] is True
        assert data["summary"]["total"] > 0
        assert data["audio_file"] == (
            "/app/speakers/api/serve_audio/20240101/test/143022_300/mic_audio.flac"
        )
        assert data["audio_mimetype"] == "audio/flac"
        assert data["all_entities"][0]["name"] == "Alice Test"
        sentences = data["sentences"]
        s1 = next(s for s in sentences if s["id"] == 1)
        assert s1["speaker_name"] == "Alice Test"
        assert s1["confidence"] == "high"
        assert s1["needs_review"] is False


def test_api_review_no_labels(speakers_env):
    """Review endpoint works for segments without speaker_labels.json."""
    from flask import Flask

    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.get("/app/speakers/api/review/20240101/test/143022_300/mic_audio")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_labels"] is False
        assert data["summary"]["needs_review"] == 0


def test_api_review_offers_configured_identity_without_principal(speakers_env):
    env = speakers_env()
    env.set_identity(preferred=OWNER_DISPLAY_NAME)
    env.create_segment(SERVE_AUDIO_DAY, SERVE_AUDIO_SEGMENT, [SERVE_AUDIO_SOURCE])

    data = _review_payload()

    assert data["all_entities"] == [
        {"entity_id": OWNER_ID, "name": OWNER_DISPLAY_NAME, "is_principal": True}
    ]


def test_api_assign_creates_principal_from_identity(speakers_env):
    env = speakers_env()
    env.set_identity(preferred=OWNER_DISPLAY_NAME)
    env.create_segment(SERVE_AUDIO_DAY, SERVE_AUDIO_SEGMENT, [SERVE_AUDIO_SOURCE])
    env.create_speaker_labels(
        SERVE_AUDIO_DAY,
        SERVE_AUDIO_SEGMENT,
        [{"sentence_id": 1, "speaker": None, "confidence": None, "method": None}],
    )

    resp = _post_assign(OWNER_ID)

    assert resp.status_code == 200
    entity = _load_owner_entity(env)
    assert entity["is_principal"] is True
    assert entity["name"] == OWNER_DISPLAY_NAME
    labels = _load_labels(env)
    assert labels["labels"][0]["speaker"] == OWNER_ID
    assert labels["labels"][0]["method"] == "user_assigned"
    assert _voiceprint_row_count(env) == 1


def test_api_owner_tag_creates_principal_from_identity_and_counts_tag(speakers_env):
    from solstone.apps.speakers.owner import load_manual_tag_stats
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.set_identity(preferred=OWNER_DISPLAY_NAME)
    env.create_segment(
        SERVE_AUDIO_DAY,
        SERVE_AUDIO_SEGMENT,
        [SERVE_AUDIO_SOURCE],
        num_sentences=1,
    )
    env.create_speaker_labels(
        SERVE_AUDIO_DAY,
        SERVE_AUDIO_SEGMENT,
        [],
        metadata={"skipped": True, "reason": "pre-bootstrap owner"},
    )
    assert not (env.journal / "entities" / OWNER_ID).exists()

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.post(
            "/app/speakers/api/owner/tag-cli",
            json={
                "day": SERVE_AUDIO_DAY,
                "stream": SERVE_AUDIO_STREAM,
                "segment_key": SERVE_AUDIO_SEGMENT,
                "source": SERVE_AUDIO_SOURCE,
                "sentence_id": 1,
            },
        )

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "assigned"
    entity = _load_owner_entity(env)
    assert entity["is_principal"] is True
    assert entity["name"] == OWNER_DISPLAY_NAME
    assert load_manual_tag_stats(OWNER_ID)["manual_tags_count"] == 1


def test_api_correct_creates_principal_from_identity(speakers_env):
    env = speakers_env()
    env.set_identity(preferred=OWNER_DISPLAY_NAME)
    env.create_segment(SERVE_AUDIO_DAY, SERVE_AUDIO_SEGMENT, [SERVE_AUDIO_SOURCE])
    env.create_speaker_labels(
        SERVE_AUDIO_DAY,
        SERVE_AUDIO_SEGMENT,
        [{"sentence_id": 1, "speaker": None, "confidence": None, "method": None}],
    )

    resp = _post_correct(OWNER_ID)

    assert resp.status_code == 200
    entity = _load_owner_entity(env)
    assert entity["is_principal"] is True
    assert entity["name"] == OWNER_DISPLAY_NAME
    labels = _load_labels(env)
    assert labels["labels"][0]["speaker"] == OWNER_ID
    assert labels["labels"][0]["method"] == "user_corrected"
    assert _voiceprint_row_count(env) == 1


def test_api_assign_reuses_principal_entity_for_second_sentence(speakers_env):
    env = speakers_env()
    env.set_identity(preferred=OWNER_DISPLAY_NAME)
    env.create_segment(SERVE_AUDIO_DAY, SERVE_AUDIO_SEGMENT, [SERVE_AUDIO_SOURCE])
    env.create_speaker_labels(
        SERVE_AUDIO_DAY,
        SERVE_AUDIO_SEGMENT,
        [
            {"sentence_id": 1, "speaker": None, "confidence": None, "method": None},
            {"sentence_id": 2, "speaker": None, "confidence": None, "method": None},
        ],
    )

    first = _post_assign(OWNER_ID, sentence_id=1)
    second = _post_assign(OWNER_ID, sentence_id=2)

    assert first.status_code == 200
    assert second.status_code == 200
    entity_dirs = sorted(p.name for p in (env.journal / "entities").iterdir())
    assert entity_dirs == [OWNER_ID]
    assert _voiceprint_row_count(env) == 2


def test_api_assign_unknown_target_does_not_create_identity_principal(speakers_env):
    env = speakers_env()
    env.set_identity(preferred=OWNER_DISPLAY_NAME)
    env.create_segment(SERVE_AUDIO_DAY, SERVE_AUDIO_SEGMENT, [SERVE_AUDIO_SOURCE])
    env.create_speaker_labels(
        SERVE_AUDIO_DAY,
        SERVE_AUDIO_SEGMENT,
        [{"sentence_id": 1, "speaker": None, "confidence": None, "method": None}],
    )

    resp = _post_assign("some_other_person")

    assert resp.status_code == 404
    assert resp.get_json()["reason_code"] == "speaker_not_found"
    assert not (env.journal / "entities").exists()


def test_api_assign_owner_slug_with_foreign_principal_not_found(speakers_env):
    env = speakers_env()
    env.set_identity(preferred=OWNER_DISPLAY_NAME)
    env.create_segment(SERVE_AUDIO_DAY, SERVE_AUDIO_SEGMENT, [SERVE_AUDIO_SOURCE])
    env.create_speaker_labels(
        SERVE_AUDIO_DAY,
        SERVE_AUDIO_SEGMENT,
        [{"sentence_id": 1, "speaker": None, "confidence": None, "method": None}],
    )
    env.create_entity("Alice Test", is_principal=True)

    resp = _post_assign(OWNER_ID)

    assert resp.status_code == 404
    assert resp.get_json()["reason_code"] == "speaker_not_found"
    assert not (env.journal / "entities" / OWNER_ID).exists()
    assert not (env.journal / "entities" / OWNER_ID / "voiceprints.npz").exists()
    labels = _load_labels(env)
    assert labels["labels"][0]["speaker"] is None


def test_api_review_and_assign_without_identity_do_not_create_principal(speakers_env):
    env = speakers_env()
    env.create_segment(SERVE_AUDIO_DAY, SERVE_AUDIO_SEGMENT, [SERVE_AUDIO_SOURCE])
    env.create_speaker_labels(
        SERVE_AUDIO_DAY,
        SERVE_AUDIO_SEGMENT,
        [{"sentence_id": 1, "speaker": None, "confidence": None, "method": None}],
    )

    data = _review_payload()
    resp = _post_assign(OWNER_ID)

    assert data["all_entities"] == []
    assert resp.status_code == 404
    assert resp.get_json()["reason_code"] == "speaker_not_found"
    assert not (env.journal / "entities").exists()


def test_api_review_existing_principal_suppresses_identity_option(speakers_env):
    env = speakers_env()
    env.set_identity(preferred=OWNER_DISPLAY_NAME)
    env.create_segment(SERVE_AUDIO_DAY, SERVE_AUDIO_SEGMENT, [SERVE_AUDIO_SOURCE])
    env.create_entity("Alice Test", is_principal=True)

    data = _review_payload()

    assert data["all_entities"] == [
        {"entity_id": "alice_test", "name": "Alice Test", "is_principal": True}
    ]


def test_api_review_existing_owner_slug_entity_not_duplicated(speakers_env):
    env = speakers_env()
    env.set_identity(preferred=OWNER_DISPLAY_NAME)
    env.create_segment(SERVE_AUDIO_DAY, SERVE_AUDIO_SEGMENT, [SERVE_AUDIO_SOURCE])
    env.create_entity(OWNER_DISPLAY_NAME)

    data = _review_payload()
    owner_entries = [
        entity for entity in data["all_entities"] if entity["entity_id"] == OWNER_ID
    ]

    assert owner_entries == [
        {"entity_id": OWNER_ID, "name": OWNER_DISPLAY_NAME, "is_principal": False}
    ]


def test_api_review_blocked_owner_slug_entity_not_synthetic_option(speakers_env):
    env = speakers_env()
    env.set_identity(preferred=OWNER_DISPLAY_NAME)
    env.create_segment(SERVE_AUDIO_DAY, SERVE_AUDIO_SEGMENT, [SERVE_AUDIO_SOURCE])
    env.create_entity(OWNER_DISPLAY_NAME)
    entity_path = env.journal / "entities" / OWNER_ID / "entity.json"
    entity = json.loads(entity_path.read_text(encoding="utf-8"))
    entity["blocked"] = True
    entity_path.write_text(json.dumps(entity) + "\n", encoding="utf-8")

    data = _review_payload()

    assert data["all_entities"] == []


def test_api_review_uses_registered_audio_extension(speakers_env):
    """Review endpoint reports the actual registered segment audio file."""
    from flask import Flask

    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment(
        "20240101",
        "143022_300",
        ["mic_audio"],
        audio_extension=".m4a",
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.get("/app/speakers/api/review/20240101/test/143022_300/mic_audio")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["audio_file"] == (
            "/app/speakers/api/serve_audio/20240101/test/143022_300/mic_audio.m4a"
        )
        assert data["audio_mimetype"] == "audio/mp4"


def test_api_review_omits_audio_when_registered_audio_is_missing(speakers_env):
    """Review endpoint returns null audio metadata when source audio is purged."""
    from flask import Flask

    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    (
        env.journal
        / "chronicle"
        / "20240101"
        / "test"
        / "143022_300"
        / "mic_audio.flac"
    ).unlink()

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.get("/app/speakers/api/review/20240101/test/143022_300/mic_audio")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["audio_file"] is None
        assert data["audio_mimetype"] is None


def test_api_review_corrections_excludes_confirmed(speakers_env):
    """Corrections summary/filter state excludes user_confirmed labels."""
    import json

    from flask import Flask

    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_entity("Alice Test")
    env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "alice_test",
                "confidence": "high",
                "method": "user_confirmed",
            },
            {
                "sentence_id": 2,
                "speaker": "alice_test",
                "confidence": "high",
                "method": "user_corrected",
            },
        ],
    )
    corr_path = (
        env.journal
        / "20240101"
        / "test"
        / "143022_300"
        / "talents"
        / "speaker_corrections.json"
    )
    corr_path.write_text(
        json.dumps(
            {
                "corrections": [
                    {
                        "sentence_id": 1,
                        "original_speaker": "alice_test",
                        "corrected_speaker": "alice_test",
                        "original_method": "acoustic",
                        "timestamp": 1,
                    },
                    {
                        "sentence_id": 2,
                        "original_speaker": "alice_test",
                        "corrected_speaker": "alice_test",
                        "original_method": "acoustic",
                        "timestamp": 2,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.get("/app/speakers/api/review/20240101/test/143022_300/mic_audio")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["summary"]["corrections"] == 1
        sentences = {s["id"]: s for s in data["sentences"]}
        assert sentences[1]["is_correction"] is False
        assert sentences[2]["is_correction"] is True


def test_api_confirm_attribution(speakers_env):
    """Confirm promotes medium-confidence to high/user_confirmed."""
    import json

    from flask import Flask

    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_entity("Alice Test")
    env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "alice_test",
                "confidence": "medium",
                "method": "acoustic",
            },
        ],
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.post(
            "/app/speakers/api/confirm-attribution",
            json={
                "day": "20240101",
                "stream": "test",
                "segment_key": "143022_300",
                "source": "mic_audio",
                "sentence_id": 1,
            },
        )
        assert resp.status_code == 200

    labels_path = (
        env.journal
        / "20240101"
        / "test"
        / "143022_300"
        / "talents"
        / "speaker_labels.json"
    )
    with open(labels_path) as f:
        labels = json.load(f)
    updated = labels["labels"][0]
    assert updated["confidence"] == "high"
    assert updated["method"] == "user_confirmed"

    corr_path = (
        env.journal
        / "20240101"
        / "test"
        / "143022_300"
        / "talents"
        / "speaker_corrections.json"
    )
    assert corr_path.exists()
    with open(corr_path) as f:
        corrections = json.load(f)
    assert len(corrections["corrections"]) == 1

    vp_path = env.journal / "entities" / "alice_test" / "voiceprints.npz"
    assert vp_path.exists()
    vp_data = np.load(vp_path, allow_pickle=False)
    metadata = json.loads(vp_data["metadata"][0])
    assert metadata["stream"] == "test"


def test_api_confirm_attribution_labels_busy(speakers_env, monkeypatch):
    """Confirm returns speaker_labels_busy when the labels lock times out."""
    from pathlib import Path

    from flask import Flask

    from solstone.apps.speakers import attribution
    from solstone.apps.speakers.routes import speakers_bp
    from solstone.think.journal_io.errors import LockTimeout

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_entity("Alice Test")
    env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "alice_test",
                "confidence": "medium",
                "method": "acoustic",
            },
        ],
    )

    def busy_hold_lock(path: Path):
        raise LockTimeout(path=path, timeout=0.0)

    monkeypatch.setattr(attribution, "hold_lock", busy_hold_lock)

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.post(
            "/app/speakers/api/confirm-attribution",
            json={
                "day": "20240101",
                "stream": "test",
                "segment_key": "143022_300",
                "source": "mic_audio",
                "sentence_id": 1,
            },
        )

    assert resp.status_code == 503
    assert resp.get_json()["reason_code"] == "speaker_labels_busy"


def test_api_confirm_idempotent(speakers_env):
    """Confirming an already-confirmed attribution is a no-op success."""
    from flask import Flask

    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_entity("Alice Test")
    env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "alice_test",
                "confidence": "high",
                "method": "user_confirmed",
            },
        ],
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.post(
            "/app/speakers/api/confirm-attribution",
            json={
                "day": "20240101",
                "stream": "test",
                "segment_key": "143022_300",
                "source": "mic_audio",
                "sentence_id": 1,
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "already_confirmed"


def test_api_confirm_wrong_confidence(speakers_env):
    """Cannot confirm a high-confidence attribution."""
    from flask import Flask

    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_entity("Alice Test")
    env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "alice_test",
                "confidence": "high",
                "method": "acoustic",
            },
        ],
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.post(
            "/app/speakers/api/confirm-attribution",
            json={
                "day": "20240101",
                "stream": "test",
                "segment_key": "143022_300",
                "source": "mic_audio",
                "sentence_id": 1,
            },
        )
        assert resp.status_code == 400


def test_api_correct_attribution(speakers_env):
    """Correct changes speaker attribution and manages voiceprints."""
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_entity(
        "Alice Test",
        voiceprints=[("20240101", "143022_300", "mic_audio", 1)],
    )
    env.create_entity("Bob Test")
    env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "alice_test",
                "confidence": "high",
                "method": "acoustic",
            },
        ],
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.post(
            "/app/speakers/api/correct-attribution",
            json={
                "day": "20240101",
                "stream": "test",
                "segment_key": "143022_300",
                "source": "mic_audio",
                "sentence_id": 1,
                "new_speaker": "bob_test",
            },
        )
        assert resp.status_code == 200

    labels_path = (
        env.journal
        / "20240101"
        / "test"
        / "143022_300"
        / "talents"
        / "speaker_labels.json"
    )
    with open(labels_path) as f:
        labels = json.load(f)
    assert labels["labels"][0]["speaker"] == "bob_test"
    assert labels["labels"][0]["method"] == "user_corrected"

    bob_vp = env.journal / "entities" / "bob_test" / "voiceprints.npz"
    assert bob_vp.exists()
    alice_vp = env.journal / "entities" / "alice_test" / "voiceprints.npz"
    assert not alice_vp.exists()

    action_entries = _read_action_entries(env.journal)
    assert len(action_entries) == 1
    assert action_entries[0]["action"] == "attribution_correct"
    assert action_entries[0]["params"]["voiceprints_removed"] == [
        "entities/alice_test/voiceprints.npz"
    ]


def test_api_correct_same_speaker(speakers_env):
    """Correcting to the same speaker is a no-op."""
    from flask import Flask

    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_entity("Alice Test")
    env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "alice_test",
                "confidence": "high",
                "method": "acoustic",
            },
        ],
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.post(
            "/app/speakers/api/correct-attribution",
            json={
                "day": "20240101",
                "stream": "test",
                "segment_key": "143022_300",
                "source": "mic_audio",
                "sentence_id": 1,
                "new_speaker": "alice_test",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "already_correct"


def test_api_correct_contextual_removes_old_voiceprint(speakers_env):
    """Correcting a contextual label removes the old auto-saved voiceprint."""
    from flask import Flask

    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_entity(
        "Alice Test",
        voiceprints=[("20240101", "143022_300", "mic_audio", 1)],
    )
    env.create_entity("Bob Test")
    env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "alice_test",
                "confidence": "medium",
                "method": "contextual",
            },
        ],
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.post(
            "/app/speakers/api/correct-attribution",
            json={
                "day": "20240101",
                "stream": "test",
                "segment_key": "143022_300",
                "source": "mic_audio",
                "sentence_id": 1,
                "new_speaker": "bob_test",
            },
        )
        assert resp.status_code == 200

    alice_vp = env.journal / "entities" / "alice_test" / "voiceprints.npz"
    assert not alice_vp.exists()


def test_api_assign_attribution(speakers_env):
    """Assign a speaker to an unattributed sentence."""
    import json

    from flask import Flask

    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_entity("Alice Test")
    env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {"sentence_id": 1, "speaker": None, "confidence": None, "method": None},
        ],
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.post(
            "/app/speakers/api/assign-attribution",
            json={
                "day": "20240101",
                "stream": "test",
                "segment_key": "143022_300",
                "source": "mic_audio",
                "sentence_id": 1,
                "speaker": "alice_test",
            },
        )
        assert resp.status_code == 200

    labels_path = (
        env.journal
        / "20240101"
        / "test"
        / "143022_300"
        / "talents"
        / "speaker_labels.json"
    )
    with open(labels_path) as f:
        labels = json.load(f)
    assert labels["labels"][0]["speaker"] == "alice_test"
    assert labels["labels"][0]["method"] == "user_assigned"


def test_api_assign_already_has_speaker(speakers_env):
    """Cannot assign to a sentence that already has a speaker."""
    from flask import Flask

    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "143022_300", ["mic_audio"])
    env.create_entity("Alice Test")
    env.create_entity("Bob Test")
    env.create_speaker_labels(
        "20240101",
        "143022_300",
        [
            {
                "sentence_id": 1,
                "speaker": "alice_test",
                "confidence": "high",
                "method": "acoustic",
            },
        ],
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.post(
            "/app/speakers/api/assign-attribution",
            json={
                "day": "20240101",
                "stream": "test",
                "segment_key": "143022_300",
                "source": "mic_audio",
                "sentence_id": 1,
                "speaker": "bob_test",
            },
        )
        assert resp.status_code == 400


def test_remove_voiceprint(speakers_env):
    """_remove_voiceprint removes matching entry and rewrites NPZ."""
    env = speakers_env()
    env.create_entity(
        "Alice Test",
        voiceprints=[
            ("20240101", "143022_300", "mic_audio", 1),
            ("20240101", "143022_300", "mic_audio", 2),
        ],
    )

    from solstone.apps.speakers.routes import _remove_voiceprint

    removed = _remove_voiceprint("alice_test", "20240101", "143022_300", "mic_audio", 1)
    assert removed is None

    vp_path = env.journal / "entities" / "alice_test" / "voiceprints.npz"
    data = np.load(vp_path, allow_pickle=False)
    assert data["embeddings"].shape[0] == 1
    assert data["metadata"].shape[0] == 1


def test_remove_voiceprint_unlinks_file_when_last_entry_removed(speakers_env):
    """_remove_voiceprint returns the NPZ path when the final entry is removed."""
    env = speakers_env()
    env.create_entity(
        "Alice Test",
        voiceprints=[("20240101", "143022_300", "mic_audio", 1)],
    )

    from solstone.apps.speakers.routes import _remove_voiceprint

    removed = _remove_voiceprint("alice_test", "20240101", "143022_300", "mic_audio", 1)

    vp_path = env.journal / "entities" / "alice_test" / "voiceprints.npz"
    assert removed == vp_path
    assert not vp_path.exists()


def test_remove_voiceprint_not_found(speakers_env):
    """_remove_voiceprint returns None when no matching entry."""
    env = speakers_env()
    env.create_entity(
        "Alice Test",
        voiceprints=[("20240101", "143022_300", "mic_audio", 1)],
    )

    from solstone.apps.speakers.routes import _remove_voiceprint

    removed = _remove_voiceprint(
        "alice_test", "20240101", "143022_300", "mic_audio", 999
    )
    assert removed is None


def test_remove_voiceprint_no_file(speakers_env):
    """_remove_voiceprint returns None when entity has no voiceprints."""
    env = speakers_env()
    env.create_entity("Alice Test")

    from solstone.apps.speakers.routes import _remove_voiceprint

    removed = _remove_voiceprint("alice_test", "20240101", "143022_300", "mic_audio", 1)
    assert removed is None


def test_api_segments_pagination(speakers_env):
    """Segments endpoint supports limit/offset pagination."""
    from flask import Flask

    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    for i in range(25):
        h = 8 + (i // 6)
        m = (i % 6) * 10
        key = f"{h:02d}{m:02d}00_300"
        env.create_segment("20240101", key, ["mic_audio"], num_sentences=2)

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.get("/app/speakers/api/segments/20240101")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 25
        assert len(data["segments"]) == 20

        resp = client.get("/app/speakers/api/segments/20240101?limit=20&offset=20")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 25
        assert len(data["segments"]) == 5

        resp = client.get("/app/speakers/api/segments/20240101?limit=10&offset=5")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] == 25
        assert len(data["segments"]) == 10

        keys = [s["key"] for s in data["segments"]]
        assert keys == sorted(keys)


def test_api_segments_speaker_filter_includes_attributed_segments_only(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "090000_300", ["mic_audio"], num_sentences=2)
    env.create_segment("20240101", "100000_300", ["mic_audio"], num_sentences=2)
    env.create_speaker_labels(
        "20240101",
        "090000_300",
        [{"sentence_id": 1, "speaker": "alice_test", "confidence": "high"}],
    )
    env.create_speaker_labels(
        "20240101",
        "100000_300",
        [{"sentence_id": 1, "speaker": "bob_test", "confidence": "high"}],
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.get("/app/speakers/api/segments/20240101?speaker=alice_test")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 1
    assert [segment["key"] for segment in data["segments"]] == ["090000_300"]


def test_api_segments_speaker_filter_unknown_entity_returns_empty_200(speakers_env):
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    env.create_segment("20240101", "090000_300", ["mic_audio"], num_sentences=2)
    env.create_speaker_labels(
        "20240101",
        "090000_300",
        [{"sentence_id": 1, "speaker": "alice_test", "confidence": "high"}],
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.get("/app/speakers/api/segments/20240101?speaker=unknown")

    assert resp.status_code == 200
    assert resp.get_json() == {"segments": [], "total": 0}


def test_api_speakers_known_returns_section_shape(speakers_env):
    from solstone.apps.speakers.copy import SPK_OVERVIEW_KNOWN_VOICES_SORTS
    from solstone.apps.speakers.routes import speakers_bp

    env = speakers_env()
    alice_dir = env.create_entity("Alice Test")
    bob_dir = env.create_entity("Bob Test")
    emb = np.zeros((2, 256), dtype=np.float32)
    emb[:, 0] = 1.0
    np.savez_compressed(
        alice_dir / "voiceprints.npz",
        embeddings=emb,
        metadata=np.asarray(
            [
                json.dumps(
                    {
                        "day": "20240101",
                        "segment_key": "090000_300",
                        "source": "audio",
                        "stream": "mic",
                        "sentence_id": 1,
                        "last_seen_ts": 10,
                    }
                ),
                json.dumps(
                    {
                        "day": "20240102",
                        "segment_key": "090000_300",
                        "source": "audio",
                        "stream": "mic",
                        "sentence_id": 2,
                        "last_seen_ts": 30,
                    }
                ),
            ],
            dtype=str,
        ),
    )
    np.savez_compressed(
        bob_dir / "voiceprints.npz",
        embeddings=emb[:1],
        metadata=np.asarray(
            [
                json.dumps(
                    {
                        "day": "20240101",
                        "segment_key": "100000_300",
                        "source": "audio",
                        "stream": "sys",
                        "sentence_id": 1,
                    }
                )
            ],
            dtype=str,
        ),
    )

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        recent = client.get("/app/speakers/api/speakers/known")
        alphabetical = client.get(
            f"/app/speakers/api/speakers/known?sort={SPK_OVERVIEW_KNOWN_VOICES_SORTS[2]}"
        )

    assert recent.status_code == 200
    data = recent.get_json()
    assert data["total"] == 2
    assert [speaker["entity_id"] for speaker in data["speakers"]] == [
        "alice_test",
        "bob_test",
    ]
    assert data["speakers"][0]["last_seen_ts"] == 30
    assert data["speakers"][0]["intra_cosine_p25"] == 1.0
    assert alphabetical.status_code == 200
    assert [
        speaker["entity_id"] for speaker in alphabetical.get_json()["speakers"]
    ] == [
        "alice_test",
        "bob_test",
    ]


def test_api_owner_status_confirmed_has_centroid_metadata(speakers_env):
    from solstone.apps.speakers.encoder_config import OWNER_THRESHOLD
    from solstone.apps.speakers.routes import speakers_bp
    from solstone.think.awareness import update_state

    env = speakers_env()
    principal_dir = env.create_entity("Self Person", is_principal=True)
    emb = np.zeros((2, 256), dtype=np.float32)
    emb[:, 0] = 1.0
    np.savez_compressed(
        principal_dir / "voiceprints.npz",
        embeddings=emb,
        metadata=np.asarray(
            [
                json.dumps(
                    {
                        "day": "20240101",
                        "segment_key": "090000_300",
                        "source": "audio",
                        "stream": "mic",
                        "sentence_id": 1,
                    }
                ),
                json.dumps(
                    {
                        "day": "20240101",
                        "segment_key": "100000_300",
                        "source": "audio",
                        "stream": "sys",
                        "sentence_id": 2,
                    }
                ),
            ],
            dtype=str,
        ),
    )
    np.savez_compressed(
        principal_dir / "owner_centroid.npz",
        centroid=emb[0],
        cluster_size=np.array(2, dtype=np.int32),
        threshold=np.array(OWNER_THRESHOLD, dtype=np.float32),
        last_refreshed_at=np.array("2026-03-15T12:00:00Z"),
    )
    update_state("voiceprint", {"status": "confirmed"})

    app = Flask(__name__)
    app.register_blueprint(speakers_bp)

    with app.test_client() as client:
        resp = client.get("/app/speakers/api/owner/status")

    assert resp.status_code == 200
    metadata = resp.get_json()["centroid_metadata"]
    assert set(metadata) == {
        "cluster_size",
        "streams",
        "last_refreshed_at",
        "intra_cosine_p25",
    }
    assert metadata == {
        "cluster_size": 2,
        "streams": ["mic", "sys"],
        "last_refreshed_at": "2026-03-15T12:00:00Z",
        "intra_cosine_p25": 1.0,
    }


def test_index_serves_spa_shell(speakers_env):
    env = speakers_env()
    client = _convey_client(env.journal)

    resp = client.get("/app/speakers/")

    assert resp.status_code == 200
    assert b'data-solstone-shell="spa"' in resp.data


def test_day_route_serves_spa_shell_and_invalid_day_404(speakers_env):
    env = speakers_env()
    client = _convey_client(env.journal)

    day_resp = client.get("/app/speakers/20240101")
    invalid_resp = client.get("/app/speakers/not-a-day")

    assert day_resp.status_code == 200
    assert b'data-solstone-shell="spa"' in day_resp.data
    assert invalid_resp.status_code == 404


def test_overview_renders_four_section_markers():
    template = Path("solstone/apps/speakers/workspace.html").read_text(encoding="utf-8")

    assert 'data-section="your-voice"' in template
    assert 'data-section="known-voices"' in template
    assert 'data-section="new-voices"' in template
    assert 'data-section="today"' in template


def test_speakers_state_endpoint_shape(speakers_env):
    from solstone.apps.speakers.copy import speaker_copy_payload
    from solstone.apps.speakers.owner import OWNER_BOOTSTRAP_MIN_STMTS

    env = speakers_env()
    client = _convey_client(env.journal)

    resp = client.get("/app/speakers/api/state")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert set(payload) == {
        "today",
        "owner_min_statements",
        "speaker_copy",
        "speaker_filter_name",
    }
    assert len(payload["today"]) == 8
    assert payload["today"].isdigit()
    assert payload["owner_min_statements"] == OWNER_BOOTSTRAP_MIN_STMTS
    assert payload["speaker_copy"] == speaker_copy_payload()
    assert payload["speaker_filter_name"] is None


def test_speakers_state_path_resolves(speakers_env):
    env = speakers_env()
    client = _convey_client(env.journal)
    adapter = client.application.url_map.bind("localhost")

    endpoint, _args = adapter.match("/app/speakers/api/state", method="GET")

    assert endpoint == "app:speakers.api_state"


def test_speakers_state_resolves_known_speaker_filter(speakers_env):
    env = speakers_env()
    env.create_entity("Alice Test")
    client = _convey_client(env.journal)

    resp = client.get("/app/speakers/api/state?speaker=alice_test")

    assert resp.status_code == 200
    assert resp.get_json()["speaker_filter_name"] == "Alice Test"


def test_speakers_state_missing_speaker_filter_is_null(speakers_env):
    env = speakers_env()
    client = _convey_client(env.journal)

    resp = client.get("/app/speakers/api/state?speaker=unknown")

    assert resp.status_code == 200
    assert resp.get_json()["speaker_filter_name"] is None


def test_speakers_state_unexpected_failure_returns_envelope(
    speakers_env,
    monkeypatch,
):
    from solstone.apps.speakers import routes

    env = speakers_env()

    def raise_lookup(_speaker):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(routes, "load_journal_entity", raise_lookup)
    client = _convey_client(env.journal)

    resp = client.get("/app/speakers/api/state?speaker=alice_test")

    assert resp.status_code == 500
    assert resp.get_json()["reason_code"] == "file_read_failed"
