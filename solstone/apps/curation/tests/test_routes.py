# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for curation app routes."""

from __future__ import annotations

import html as html_lib
import json
from pathlib import Path

from solstone.apps.curation.copy import CUR_EMPTY_STATE, CUR_HEADING
from solstone.think.entities.journal import save_journal_entity
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


def test_rendered_payload_matches_copy_source(curation_env):
    env = curation_env()

    resp = env.client.get("/app/curation/")

    assert resp.status_code == 200
    assert "CUR_COPY" in resp.get_data(as_text=True)
    assert json.dumps(CUR_HEADING) in resp.get_data(as_text=True)


def test_app_metadata_exists():
    metadata = json.loads(Path("solstone/apps/curation/app.json").read_text())

    assert metadata["label"] == "suggestions"
    assert metadata["facets"]["disabled"] is True
