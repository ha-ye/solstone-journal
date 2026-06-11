# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for curation app routes."""

from __future__ import annotations

import html as html_lib
import json
from pathlib import Path

import numpy as np

from solstone.apps.curation.copy import CUR_EMPTY_STATE, CUR_HEADING
from solstone.think.entities.journal import load_journal_entity, save_journal_entity
from solstone.think.entities.review_candidates import (
    load_candidates as load_entity_candidates,
)
from solstone.think.entities.review_candidates import (
    save_candidates as save_entity_candidates,
)
from solstone.think.facet_review_candidates import (
    load_candidates as load_facet_candidates,
)
from solstone.think.facet_review_candidates import record_facet_candidate
from solstone.think.speaker_review_candidates import (
    candidate_key as speaker_candidate_key,
)
from solstone.think.speaker_review_candidates import (
    load_candidates as load_speaker_candidates,
)
from solstone.think.speaker_review_candidates import record_name_variant_candidate


def _seed_facet_candidate(count: int = 3) -> None:
    record_facet_candidate(
        "Home Reno",
        "home reno",
        count,
        14,
        [{"day": "20260602", "stream": "archon", "segment": "090000_300"}],
        "20260602",
    )


def _seed_entities() -> None:
    save_journal_entity(
        {
            "id": "kognova_inc",
            "name": "Kognova Inc",
            "type": "Company",
            "aka": ["Kognova Incorporated"],
        }
    )
    save_journal_entity(
        {
            "id": "kognova",
            "name": "Kognova",
            "type": "Company",
            "aka": [],
        }
    )


def _seed_entity_candidate(status: str = "open", detection_count: int = 4) -> None:
    save_entity_candidates(
        [
            {
                "facet": "work",
                "source": "Kognova Inc",
                "source_slug": "kognova_inc",
                "target": "Kognova",
                "target_slug": "kognova",
                "status": status,
                "evidence": {
                    "basis": "name-variant",
                    "summary": "Kognova Inc / Kognova",
                    "detection_count": detection_count,
                    "needs": 0,
                },
            }
        ]
    )


def _entity_payload() -> dict[str, str]:
    return {
        "facet": "work",
        "source_slug": "kognova_inc",
        "target_slug": "kognova",
    }


def _speaker_payload(
    source_id: str = "alice",
    target_id: str = "alice_johnson",
) -> dict[str, str]:
    return {
        "key": speaker_candidate_key(source_id, target_id),
        "source_id": source_id,
        "target_id": target_id,
    }


def _embedding(vector: list[float]) -> np.ndarray:
    embedding = np.array(vector + [0.0] * (256 - len(vector)), dtype=np.float32)
    return embedding / np.linalg.norm(embedding)


def _write_voiceprints(
    entity_dir: Path,
    embedding: np.ndarray,
    *,
    offset: int = 0,
) -> None:
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


def _write_speaker_labels(env) -> Path:
    labels_path = (
        env.journal
        / "chronicle"
        / "20240101"
        / "test"
        / "143022_300"
        / "talents"
        / "speaker_labels.json"
    )
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    labels_path.write_text(
        json.dumps(
            {
                "labels": [
                    {
                        "sentence_id": 1,
                        "speaker": "alice",
                        "confidence": "high",
                        "method": "voiceprint",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return labels_path


def _seed_speaker_entities(env) -> Path:
    save_journal_entity(
        {
            "id": "alice",
            "name": "Alice",
            "type": "Person",
            "aka": [],
        }
    )
    save_journal_entity(
        {
            "id": "alice_johnson",
            "name": "Alice Johnson",
            "type": "Person",
            "aka": [],
        }
    )
    embedding = _embedding([1.0, 0.0, 0.0])
    _write_voiceprints(env.journal / "entities" / "alice", embedding)
    _write_voiceprints(
        env.journal / "entities" / "alice_johnson",
        embedding,
        offset=10,
    )
    labels_path = _write_speaker_labels(env)
    record_name_variant_candidate(
        source_id="alice",
        source_label="Alice",
        target_id="alice_johnson",
        target_label="Alice Johnson",
        similarity=0.99,
    )
    return labels_path


def test_index_renders_empty_state(curation_env):
    env = curation_env()

    resp = env.client.get("/app/curation/")

    assert resp.status_code == 200
    html = html_lib.unescape(resp.get_data(as_text=True))
    assert CUR_HEADING in html
    assert CUR_EMPTY_STATE in html


def test_index_renders_facet_and_entity_candidates(curation_env):
    env = curation_env()
    _seed_facet_candidate()
    _seed_entity_candidate()

    resp = env.client.get("/app/curation/")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Home Reno" in html
    assert "Kognova Inc" in html
    assert "Kognova" in html


def test_index_renders_speaker_candidate(curation_env):
    env = curation_env()
    record_name_variant_candidate(
        source_id="alice",
        source_label="Alice",
        target_id="alice_johnson",
        target_label="Alice Johnson",
        similarity=0.99,
    )

    resp = env.client.get("/app/curation/")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'data-kind="speaker_name_variant"' in html
    assert "Alice" in html
    assert "Alice Johnson" in html


def test_facet_accept_creates_facet_and_flips_status(curation_env):
    env = curation_env()
    _seed_facet_candidate()

    resp = env.client.post(
        "/app/curation/api/facet/accept",
        json={"name_key": "home reno"},
    )

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "accepted"
    assert (env.journal / "facets" / "home-reno" / "facet.json").exists()
    assert load_facet_candidates()[0]["status"] == "accepted"


def test_facet_dismiss_sets_watermark(curation_env):
    env = curation_env()
    _seed_facet_candidate(count=5)

    resp = env.client.post(
        "/app/curation/api/facet/dismiss",
        json={"name_key": "home reno"},
    )

    assert resp.status_code == 200
    row = load_facet_candidates()[0]
    assert row["status"] == "dismissed"
    assert row["dismissed_count"] == 5


def test_entity_preview_returns_summary_and_keeps_status_open(curation_env):
    env = curation_env()
    _seed_entities()
    _seed_entity_candidate()

    resp = env.client.post("/app/curation/api/entity/preview", json=_entity_payload())

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "preview"
    assert data["merge"]["would_identity"]["akas_added"] == [
        "Kognova Inc",
        "Kognova Incorporated",
    ]
    assert data["preview"]["akas_added"] == [
        "Kognova Inc",
        "Kognova Incorporated",
    ]
    assert load_entity_candidates()[0]["status"] == "open"


def test_entity_accept_flips_status(curation_env):
    env = curation_env()
    _seed_entities()
    _seed_entity_candidate()

    resp = env.client.post("/app/curation/api/entity/accept", json=_entity_payload())

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "accepted"
    assert load_entity_candidates()[0]["status"] == "accepted"


def test_entity_dismiss_sets_watermark(curation_env):
    env = curation_env()
    _seed_entity_candidate(detection_count=6)

    resp = env.client.post("/app/curation/api/entity/dismiss", json=_entity_payload())

    assert resp.status_code == 200
    row = load_entity_candidates()[0]
    assert row["status"] == "dismissed"
    assert row["dismissed_detection_count"] == 6


def test_missing_required_field_returns_standard_error(curation_env):
    env = curation_env()

    resp = env.client.post("/app/curation/api/facet/accept", json={})

    assert resp.status_code == 400
    assert resp.get_json()["reason_code"] == "missing_required_field"


def test_entity_preview_error_returns_400_without_flipping(curation_env):
    env = curation_env()
    _seed_entity_candidate()

    resp = env.client.post("/app/curation/api/entity/preview", json=_entity_payload())

    assert resp.status_code == 400
    assert resp.get_json()["status"] == "error"
    assert load_entity_candidates()[0]["status"] == "open"


def test_speaker_preview_returns_summary_and_keeps_state_open(curation_env):
    env = curation_env()
    labels_path = _seed_speaker_entities(env)
    source_before = load_journal_entity("alice")
    target_before = load_journal_entity("alice_johnson")
    source_voiceprints_before = (
        env.journal / "entities" / "alice" / "voiceprints.npz"
    ).read_bytes()
    target_voiceprints_before = (
        env.journal / "entities" / "alice_johnson" / "voiceprints.npz"
    ).read_bytes()
    labels_before = labels_path.read_text(encoding="utf-8")

    resp = env.client.post("/app/curation/api/speaker/preview", json=_speaker_payload())

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "preview"
    assert data["merge"]["would_identity"]["akas_added"] == ["Alice"]
    assert data["preview"]["akas_added"] == ["Alice"]
    assert data["preview"]["voiceprints_added"] == 5
    assert load_speaker_candidates()[0]["status"] == "open"
    assert load_journal_entity("alice") == source_before
    assert load_journal_entity("alice_johnson") == target_before
    assert (
        env.journal / "entities" / "alice" / "voiceprints.npz"
    ).read_bytes() == source_voiceprints_before
    assert (
        env.journal / "entities" / "alice_johnson" / "voiceprints.npz"
    ).read_bytes() == target_voiceprints_before
    assert labels_path.read_text(encoding="utf-8") == labels_before


def test_speaker_accept_merges_and_marks_candidate_accepted(curation_env):
    env = curation_env()
    labels_path = _seed_speaker_entities(env)

    resp = env.client.post("/app/curation/api/speaker/accept", json=_speaker_payload())

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "accepted"
    assert data["merge"]["merged"] is True
    rows = load_speaker_candidates()
    assert len(rows) == 1
    assert rows[0]["status"] == "accepted"
    assert [row for row in rows if row["status"] == "open"] == []
    assert load_journal_entity("alice") is None
    target = load_journal_entity("alice_johnson")
    assert target is not None
    assert "Alice" in target["aka"]
    labels = json.loads(labels_path.read_text(encoding="utf-8"))["labels"]
    assert labels[0]["speaker"] == "alice_johnson"


def test_speaker_accept_merge_error_keeps_candidate_open(curation_env):
    env = curation_env()
    record_name_variant_candidate(
        source_id="alice",
        source_label="Alice",
        target_id="alice_johnson",
        target_label="Alice Johnson",
        similarity=0.99,
    )

    resp = env.client.post("/app/curation/api/speaker/accept", json=_speaker_payload())

    assert resp.status_code == 400
    assert resp.get_json()["status"] == "error"
    assert "Source entity not found" in resp.get_json()["error"]
    assert load_speaker_candidates()[0]["status"] == "open"


def test_speaker_accept_rejects_swapped_direction(curation_env):
    env = curation_env()
    labels_path = _seed_speaker_entities(env)
    payload = _speaker_payload(source_id="alice_johnson", target_id="alice")

    resp = env.client.post("/app/curation/api/speaker/accept", json=payload)

    assert resp.status_code == 400
    data = resp.get_json()
    assert data["status"] == "error"
    assert data["error"] == "candidate direction mismatch"
    assert load_speaker_candidates()[0]["status"] == "open"
    assert load_journal_entity("alice") is not None
    assert load_journal_entity("alice_johnson") is not None
    labels = json.loads(labels_path.read_text(encoding="utf-8"))["labels"]
    assert labels[0]["speaker"] == "alice"


def test_speaker_dismiss_sets_watermark_and_removes_from_open_list(curation_env):
    env = curation_env()
    record_name_variant_candidate(
        source_id="alice",
        source_label="Alice",
        target_id="alice_johnson",
        target_label="Alice Johnson",
        similarity=0.99,
    )

    resp = env.client.post("/app/curation/api/speaker/dismiss", json=_speaker_payload())

    assert resp.status_code == 200
    row = load_speaker_candidates()[0]
    assert row["status"] == "dismissed"
    assert row["dismissed_detection_count"] == 1
    index_resp = env.client.get("/app/curation/")
    assert index_resp.status_code == 200
    assert 'data-kind="speaker_name_variant"' not in index_resp.get_data(as_text=True)


def test_speaker_payload_missing_field_returns_400(curation_env):
    env = curation_env()

    resp = env.client.post(
        "/app/curation/api/speaker/preview",
        json={"key": speaker_candidate_key("alice", "alice_johnson")},
    )

    assert resp.status_code == 400
    assert resp.get_json()["reason_code"] == "missing_required_field"


def test_speaker_payload_key_mismatch_returns_400(curation_env):
    env = curation_env()

    resp = env.client.post(
        "/app/curation/api/speaker/preview",
        json={
            "key": "wrong|key",
            "source_id": "alice",
            "target_id": "alice_johnson",
            "source_label": "Ignored",
            "target_label": "Also Ignored",
        },
    )

    assert resp.status_code == 400
    assert resp.get_json()["reason_code"] == "invalid_request_value"


def test_rendered_payload_matches_copy_source(curation_env):
    env = curation_env()

    resp = env.client.get("/app/curation/")

    assert resp.status_code == 200
    assert "CUR_COPY" in resp.get_data(as_text=True)
    assert json.dumps(CUR_HEADING) in resp.get_data(as_text=True)


def test_app_metadata_exists():
    metadata = json.loads(Path("solstone/apps/curation/app.json").read_text())

    assert metadata["label"] == "curation"
    assert metadata["facets"]["disabled"] is True
