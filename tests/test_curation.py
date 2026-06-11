# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

from solstone.think.curation import (
    KIND_SPEAKER_NAME_VARIANT,
    accept_entity_candidate,
    accept_facet_candidate,
    dismiss_entity_candidate,
    dismiss_facet_candidate,
    load_open_items,
    merge_preview_fields,
)
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
from solstone.think.facet_review_candidates import (
    save_candidates as save_facet_candidates,
)
from solstone.think.speaker_review_candidates import record_name_variant_candidate


@pytest.fixture
def curation_journal(monkeypatch, tmp_path) -> Path:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    return Path(tmp_path)


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


def test_load_open_items_normalizes_and_orders(curation_journal):
    save_facet_candidates(
        [
            {
                "name": "Home Reno",
                "name_key": "home reno",
                "status": "open",
                "count": 3,
                "window_days": 14,
                "evidence": {"samples": [{"day": "20260602"}]},
            },
            {
                "name": "Done",
                "name_key": "done",
                "status": "dismissed",
                "count": 9,
            },
        ]
    )
    _seed_entity_candidate(detection_count=5)

    items = load_open_items()

    assert [item.key for item in items] == ["work|kognova_inc|kognova", "home reno"]
    assert items[0].kind == "entity_merge"
    assert items[0].strength == 5
    assert items[1].kind == "facet_candidate"
    assert items[1].evidence["count"] == 3


def test_load_open_items_includes_speaker_name_variant(curation_journal):
    record_name_variant_candidate(
        source_id="alice",
        source_label="Alice",
        target_id="alice_johnson",
        target_label="Alice Johnson",
        similarity=0.934,
    )

    items = load_open_items()

    assert len(items) == 1
    item = items[0]
    assert item.kind == KIND_SPEAKER_NAME_VARIANT
    assert item.key == "alice|alice_johnson"
    assert item.name is None
    assert item.facet is None
    assert item.source == "Alice"
    assert item.source_slug == "alice"
    assert item.target == "Alice Johnson"
    assert item.target_slug == "alice_johnson"
    assert item.evidence["similarity"] == 0.934
    assert item.evidence["readiness"] == "ready"
    assert item.strength == 93


def test_accept_facet_candidate_creates_facet_then_marks_accepted(curation_journal):
    save_facet_candidates(
        [
            {
                "name": "Home Reno",
                "name_key": "home reno",
                "status": "open",
                "count": 3,
            }
        ]
    )

    result = accept_facet_candidate("home reno")

    assert result["status"] == "accepted"
    assert result["facet_slug"] == "home-reno"
    assert (curation_journal / "facets" / "home-reno" / "facet.json").exists()
    assert load_facet_candidates()[0]["status"] == "accepted"


def test_accept_facet_candidate_is_idempotent_when_already_accepted(curation_journal):
    save_facet_candidates(
        [
            {
                "name": "Home Reno",
                "name_key": "home reno",
                "status": "accepted",
                "count": 3,
            }
        ]
    )

    result = accept_facet_candidate("home reno")

    assert result["status"] == "already_accepted"
    assert not (curation_journal / "facets" / "home-reno").exists()


def test_accept_facet_candidate_duplicate_keeps_candidate_open(curation_journal):
    save_facet_candidates(
        [
            {
                "name": "Home Reno",
                "name_key": "home reno",
                "status": "open",
                "count": 3,
            }
        ]
    )
    first = accept_facet_candidate("home reno")
    save_facet_candidates(
        [
            {
                "name": "Home Reno",
                "name_key": "home reno",
                "status": "open",
                "count": 3,
            }
        ]
    )

    second = accept_facet_candidate("home reno")

    assert first["status"] == "accepted"
    assert second["status"] == "error"
    assert "already exists" in second["error"]
    assert load_facet_candidates()[0]["status"] == "open"


def test_dismiss_facet_candidate_sets_watermark_and_is_idempotent(curation_journal):
    save_facet_candidates(
        [
            {
                "name": "Home Reno",
                "name_key": "home reno",
                "status": "open",
                "count": 4,
            }
        ]
    )

    first = dismiss_facet_candidate("home reno")
    second = dismiss_facet_candidate("home reno")

    assert first["status"] == "dismissed"
    assert second["status"] == "already_dismissed"
    assert load_facet_candidates()[0]["dismissed_count"] == 4


def test_entity_preview_is_read_only(curation_journal):
    _seed_entities()
    _seed_entity_candidate()

    result = accept_entity_candidate(
        "work",
        "kognova_inc",
        "kognova",
        commit=False,
    )

    assert result["status"] == "preview"
    assert result["merge"]["would_identity"]["akas_added"] == [
        "Kognova Inc",
        "Kognova Incorporated",
    ]
    assert load_entity_candidates()[0]["status"] == "open"


def test_accept_entity_candidate_commits_then_marks_accepted(curation_journal):
    _seed_entities()
    _seed_entity_candidate()

    result = accept_entity_candidate(
        "work",
        "kognova_inc",
        "kognova",
        commit=True,
    )

    assert result["status"] == "accepted"
    assert result["merge"]["merged"] is True
    assert load_entity_candidates()[0]["status"] == "accepted"


def test_accept_entity_candidate_error_keeps_status_open(curation_journal):
    _seed_entity_candidate()

    result = accept_entity_candidate(
        "work",
        "kognova_inc",
        "kognova",
        commit=True,
    )

    assert result["status"] == "error"
    assert "Source entity not found" in result["error"]
    assert load_entity_candidates()[0]["status"] == "open"


def test_dismiss_entity_candidate_sets_watermark_and_is_idempotent(curation_journal):
    _seed_entity_candidate()

    first = dismiss_entity_candidate("work", "kognova_inc", "kognova")
    second = dismiss_entity_candidate("work", "kognova_inc", "kognova")

    assert first["status"] == "dismissed"
    assert second["status"] == "already_dismissed"
    assert load_entity_candidates()[0]["dismissed_detection_count"] == 4


def test_merge_preview_fields_returns_compact_summary():
    fields = merge_preview_fields(
        {
            "would_identity": {
                "akas_added": ["Kognova Inc"],
                "emails_added_count": 1,
            },
            "would_facets": {
                "moved_count": 2,
                "merged_count": 3,
                "observations_appended": 4,
            },
            "would_segments": {
                "labels_rewritten": 5,
                "corrections_rewritten": 6,
                "errors": [{"message": "bad"}],
            },
            "would_voiceprints": {
                "added": 7,
                "target_total": 8,
            },
        }
    )

    assert fields == {
        "akas_added": ["Kognova Inc"],
        "emails_added_count": 1,
        "facet_moved_count": 2,
        "facet_merged_count": 3,
        "observations_appended": 4,
        "labels_rewritten": 5,
        "corrections_rewritten": 6,
        "segment_errors": [{"message": "bad"}],
        "voiceprints_added": 7,
        "voiceprints_target_total": 8,
    }
