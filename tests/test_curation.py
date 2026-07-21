# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

import solstone.think.curation as curation
from solstone.apps.speakers.candidate_tracker import (
    CandidateProfile,
    CandidateTracker,
    canonical_candidate_anchor,
)
from solstone.think.curation import (
    KIND_ENTITY_AMBIGUITY,
    KIND_SPEAKER_CANDIDATE_PAIR,
    KIND_SPEAKER_NAME_VARIANT,
    accept_entity_candidate,
    accept_entity_candidate_batch,
    accept_facet_candidate,
    accept_speaker_candidate_pair,
    dismiss_entity_candidate,
    dismiss_entity_candidate_batch,
    dismiss_facet_candidate,
    dismiss_speaker_candidate_pair,
    load_open_items,
    merge_preview_fields,
)
from solstone.think.entities import (
    EntityResolutionOutcome,
    ResolutionOrigin,
    ResolutionScope,
    record_entity_resolution,
    undo_entity_merge,
)
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
from solstone.think.facet_review_candidates import (
    save_candidates as save_facet_candidates,
)
from solstone.think.indexer.edges import insert_edges
from solstone.think.indexer.journal import get_journal_index
from solstone.think.journal_io import LockTimeout
from solstone.think.speaker_candidate_pair_review_candidates import (
    load_candidates as load_pair_candidates,
)
from solstone.think.speaker_candidate_pair_review_candidates import (
    record_candidate_pair,
)
from solstone.think.speaker_keep_separate import record_keep_separate_assertion
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


def test_open_ambiguity_is_a_ranked_curation_item(curation_journal):
    save_journal_entity(
        {"id": "sarah_connor", "name": "Sarah Connor", "type": "Person"}
    )
    save_journal_entity({"id": "sarah_lee", "name": "Sarah Lee", "type": "Person"})
    resolution = record_entity_resolution(
        "Sarah",
        [load_journal_entity("sarah_connor"), load_journal_entity("sarah_lee")],
        scope=ResolutionScope.journal(),
        origin=ResolutionOrigin(lane="test.curation", field="entity"),
    )

    assert resolution.outcome == EntityResolutionOutcome.AMBIGUOUS
    item = next(
        item for item in load_open_items() if item.kind == KIND_ENTITY_AMBIGUITY
    )
    assert item.key == resolution.ambiguity_id
    assert item.name == "Sarah"
    assert {row["id"] for row in item.evidence["ranked_candidates"]} == {
        "sarah_connor",
        "sarah_lee",
    }
    assert item.evidence["origins"] == [{"lane": "test.curation", "field": "entity"}]


def _entity_candidate_row(
    source_slug: str = "kognova_inc",
    target_slug: str = "kognova",
    *,
    source: str = "Kognova Inc",
    target: str = "Kognova",
    status: str = "open",
    detection_count: int = 4,
) -> dict[str, Any]:
    return {
        "facet": "work",
        "source": source,
        "source_slug": source_slug,
        "target": target,
        "target_slug": target_slug,
        "status": status,
        "evidence": {
            "basis": "name-variant",
            "summary": f"{source} / {target}",
            "detection_count": detection_count,
            "needs": 0,
        },
    }


def _seed_entity_candidates(rows: list[dict[str, Any]]) -> None:
    save_entity_candidates(rows)


def _insert_edges(journal: Path, rows: list[dict[str, Any]]) -> None:
    conn, _ = get_journal_index(str(journal))
    try:
        insert_edges(conn, rows)
        conn.commit()
    finally:
        conn.close()


def _edge(
    src: str,
    dst: str,
    path: str,
    *,
    day: str | None = "20260601",
) -> dict[str, Any]:
    return {
        "src": src,
        "dst": dst,
        "kind": "works-with",
        "day": day,
        "facet": "work",
        "source": "curation-test",
        "path": path,
        "weight": 1,
    }


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _seed_entity_candidate(status: str = "open", detection_count: int = 4) -> None:
    _seed_entity_candidates(
        [_entity_candidate_row(status=status, detection_count=detection_count)]
    )


def _seed_entity_pair(
    source_slug: str,
    source_name: str,
    target_slug: str,
    target_name: str,
) -> None:
    save_journal_entity(
        {
            "id": source_slug,
            "name": source_name,
            "type": "Company",
            "aka": [],
        }
    )
    save_journal_entity(
        {
            "id": target_slug,
            "name": target_name,
            "type": "Company",
            "aka": [],
        }
    )


def _assert_folded(source_slug: str, target_slug: str, source_name: str) -> None:
    assert load_journal_entity(source_slug) is None
    target = load_journal_entity(target_slug)
    assert target is not None
    assert source_name in target["aka"]


def _unit(vector: list[float]) -> np.ndarray:
    embedding = np.array(vector + [0.0] * (256 - len(vector)), dtype=np.float32)
    return embedding / np.linalg.norm(embedding)


def _source_segment(day: str, cluster_label: int = 1) -> dict[str, object]:
    return {
        "day": day,
        "segment_key": "090000_300",
        "stream": "test",
        "source": "mic_audio",
        "cluster_label": cluster_label,
    }


def _candidate_profile(
    cand_id: int,
    centroid: np.ndarray,
    *,
    status: str = "pending",
    confirmed_entity: str | None = None,
) -> CandidateProfile:
    source_segment = _source_segment(f"2026010{cand_id}", cand_id)
    return CandidateProfile(
        cand_id=cand_id,
        centroid=centroid,
        n_segments=1,
        n_intervals=30,
        total_duration_s=30.0,
        source_segments=[source_segment],
        status=status,
        confirmed_entity=confirmed_entity,
    )


def _seed_candidate_tracker(
    journal: Path,
    candidates: list[CandidateProfile],
) -> CandidateTracker:
    tracker = CandidateTracker(journal / "awareness" / "speaker_candidates.json")
    tracker._candidates = {candidate.cand_id: candidate for candidate in candidates}
    tracker._next_id = (
        max((candidate.cand_id for candidate in candidates), default=0) + 1
    )
    tracker.save()
    return CandidateTracker(journal / "awareness" / "speaker_candidates.json")


def _record_pair_for_candidates(
    left: CandidateProfile,
    right: CandidateProfile,
    *,
    similarity: float = 0.70,
) -> tuple[str, str]:
    anchor_a = canonical_candidate_anchor(left)
    anchor_b = canonical_candidate_anchor(right)
    record_candidate_pair(
        source_anchor=anchor_a,
        target_anchor=anchor_b,
        source_anchors={anchor_a},
        target_anchors={anchor_b},
        similarity=similarity,
        source_intervals=left.n_intervals,
        target_intervals=right.n_intervals,
        source_samples=[],
        target_samples=[],
    )
    return anchor_a, anchor_b


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


def test_load_open_items_filters_keep_separate_speaker_name_variant(curation_journal):
    record_name_variant_candidate(
        source_id="alice",
        source_label="Alice",
        target_id="alice_johnson",
        target_label="Alice Johnson",
        similarity=0.934,
    )
    record_keep_separate_assertion(
        "alice",
        "alice_johnson",
        source_kind="explicit_create_near_match",
        operation_id="idop_test",
        detection_count=1,
    )

    items = load_open_items()

    assert [item for item in items if item.kind == KIND_SPEAKER_NAME_VARIANT] == []


def test_load_open_items_includes_speaker_candidate_pair(curation_journal):
    left = _candidate_profile(1, _unit([1.0, 0.0]))
    right = _candidate_profile(2, _unit([0.62, np.sqrt(1.0 - 0.62**2)]))
    anchor_a, anchor_b = _record_pair_for_candidates(left, right, similarity=0.62)

    items = load_open_items()

    assert len(items) == 1
    item = items[0]
    assert item.kind == KIND_SPEAKER_CANDIDATE_PAIR
    assert item.key == curation._speaker_candidate_pair_key(anchor_a, anchor_b)
    assert item.source_slug == anchor_a
    assert item.target_slug == anchor_b
    assert item.source == "candidate A"
    assert item.target == "candidate B"
    assert item.evidence["similarity"] == 0.62
    assert item.evidence["source_intervals"] == 30
    assert item.evidence["target_intervals"] == 30
    assert item.strength == 62


def test_entity_merge_equal_strength_uses_shared_neighborhood(curation_journal):
    _seed_entity_candidates(
        [
            _entity_candidate_row(
                source_slug="alpha_source",
                target_slug="alpha_target",
                source="Alpha Source",
                target="Alpha Target",
                detection_count=5,
            ),
            _entity_candidate_row(
                source_slug="zeta_source",
                target_slug="zeta_target",
                source="Zeta Source",
                target="Zeta Target",
                detection_count=5,
            ),
        ]
    )
    _insert_edges(
        curation_journal,
        [
            _edge("zeta_source", "shared_one", "curation/zeta-shared-one-a"),
            _edge("zeta_target", "shared_one", "curation/zeta-shared-one-b"),
            _edge("zeta_source", "shared_two", "curation/zeta-shared-two-a"),
            _edge("zeta_target", "shared_two", "curation/zeta-shared-two-b"),
            _edge("zeta_source", "source_only", "curation/zeta-source-only"),
            _edge("zeta_target", "target_only", "curation/zeta-target-only"),
        ],
    )

    items = load_open_items()

    assert [item.key for item in items] == [
        "work|zeta_source|zeta_target",
        "work|alpha_source|alpha_target",
    ]
    assert items[0].strength == 5
    assert items[0].evidence["shared_neighbors"] == ["shared_one", "shared_two"]
    assert items[0].evidence["neighborhood_similarity"] == pytest.approx(0.5)
    assert items[0].evidence["composite"] == pytest.approx(5.125)
    assert items[0].composite == pytest.approx(5.125)
    assert items[0].to_dict()["composite"] == pytest.approx(5.125)


def test_entity_merge_detection_strength_stays_primary(curation_journal):
    _seed_entity_candidates(
        [
            _entity_candidate_row(
                source_slug="alpha_high",
                target_slug="alpha_target",
                source="Alpha High",
                target="Alpha Target",
                detection_count=6,
            ),
            _entity_candidate_row(
                source_slug="zeta_low",
                target_slug="zeta_target",
                source="Zeta Low",
                target="Zeta Target",
                detection_count=5,
            ),
        ]
    )
    _insert_edges(
        curation_journal,
        [
            _edge("zeta_low", "shared_one", "curation/low-shared-one-a"),
            _edge("zeta_target", "shared_one", "curation/low-shared-one-b"),
            _edge("zeta_low", "shared_two", "curation/low-shared-two-a"),
            _edge("zeta_target", "shared_two", "curation/low-shared-two-b"),
        ],
    )

    items = load_open_items()

    assert [item.key for item in items] == [
        "work|alpha_high|alpha_target",
        "work|zeta_low|zeta_target",
    ]
    assert items[0].strength == 6
    assert items[0].composite == 6.0
    assert items[1].strength == 5
    assert items[1].composite == pytest.approx(5.25)


def test_load_open_items_missing_edge_index_degrades_without_mutation(
    curation_journal,
):
    _seed_entity_candidates(
        [
            _entity_candidate_row(
                source_slug="zeta_source",
                target_slug="zeta_target",
                source="Zeta Source",
                target="Zeta Target",
                detection_count=3,
            ),
            _entity_candidate_row(
                source_slug="alpha_source",
                target_slug="alpha_target",
                source="Alpha Source",
                target="Alpha Target",
                detection_count=3,
            ),
            _entity_candidate_row(
                source_slug="middle_source",
                target_slug="middle_target",
                source="Middle Source",
                target="Middle Target",
                detection_count=4,
            ),
        ]
    )
    before = _tree_snapshot(curation_journal)

    items = load_open_items()

    assert _tree_snapshot(curation_journal) == before
    assert [item.key for item in items] == [
        "work|middle_source|middle_target",
        "work|alpha_source|alpha_target",
        "work|zeta_source|zeta_target",
    ]
    assert [item.key for item in items] == [
        item.key for item in sorted(items, key=lambda item: (-item.strength, item.key))
    ]
    assert [item.composite for item in items] == [4.0, 3.0, 3.0]


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


def test_repair_required_merge_state_survives_single_and_batch_results(
    curation_journal,
    monkeypatch: pytest.MonkeyPatch,
):
    _seed_entity_candidate()
    repair = {
        "error": {"code": "repair_required", "message": "rollback failed"},
        "operation_state": "repair_required",
        "mutation_applied": True,
        "source_state": {"exists": True},
        "target_state": {"exists": True},
        "safe_remediation": "Inspect before retrying.",
    }
    monkeypatch.setattr(curation, "merge_entity", lambda *args, **kwargs: repair)

    single = accept_entity_candidate("work", "kognova_inc", "kognova", commit=True)
    batch = accept_entity_candidate_batch(
        [
            {
                "facet": "work",
                "source_slug": "kognova_inc",
                "target_slug": "kognova",
            }
        ]
    )

    assert single["error"] == "rollback failed"
    assert single["operation_state"] == "repair_required"
    assert single["mutation_applied"] is True
    assert single["safe_remediation"] == "Inspect before retrying."
    assert batch["results"][0]["operation_state"] == "repair_required"
    assert batch["results"][0]["safe_remediation"] == "Inspect before retrying."


def test_dismiss_entity_candidate_sets_watermark_and_is_idempotent(curation_journal):
    _seed_entity_candidate()

    first = dismiss_entity_candidate("work", "kognova_inc", "kognova")
    second = dismiss_entity_candidate("work", "kognova_inc", "kognova")

    assert first["status"] == "dismissed"
    assert second["status"] == "already_dismissed"
    assert load_entity_candidates()[0]["dismissed_detection_count"] == 4


def test_accept_entity_candidate_batch_accepts_many_in_order(curation_journal):
    _seed_entity_pair("kognova_inc", "Kognova Inc", "kognova", "Kognova")
    _seed_entity_pair("octo_labs_inc", "Octo Labs Inc", "octo_labs", "Octo Labs")
    _seed_entity_candidates(
        [
            _entity_candidate_row(),
            _entity_candidate_row(
                "octo_labs_inc",
                "octo_labs",
                source="Octo Labs Inc",
                target="Octo Labs",
                detection_count=3,
            ),
        ]
    )

    result = accept_entity_candidate_batch(
        [
            {"facet": "work", "source_slug": "kognova_inc", "target_slug": "kognova"},
            {
                "facet": "work",
                "source_slug": "octo_labs_inc",
                "target_slug": "octo_labs",
            },
        ]
    )

    assert result["accepted"] == 2
    assert result["failed"] == 0
    assert len(result["results"]) == 2
    assert [row["source_slug"] for row in result["results"]] == [
        "kognova_inc",
        "octo_labs_inc",
    ]
    assert all(
        set(row)
        == {
            "facet",
            "source_slug",
            "target_slug",
            "status",
            "error",
            "merge_id",
            "undo",
        }
        for row in result["results"]
    )
    assert all(row["merge_id"].startswith("em_") for row in result["results"])
    assert all(row["undo"]["available"] for row in result["results"])
    assert all(
        "merge" not in row and "candidate" not in row for row in result["results"]
    )
    _assert_folded("kognova_inc", "kognova", "Kognova Inc")
    _assert_folded("octo_labs_inc", "octo_labs", "Octo Labs Inc")


def test_batch_merge_undo_one_preserves_later_success_on_same_target(
    curation_journal,
):
    save_journal_entity(
        {
            "id": "alpha_inc",
            "name": "Alpha Inc",
            "type": "Company",
            "aka": ["Alpha Alias"],
        }
    )
    save_journal_entity(
        {
            "id": "beta_inc",
            "name": "Beta Inc",
            "type": "Company",
            "aka": ["Beta Alias"],
        }
    )
    save_journal_entity(
        {"id": "anchor", "name": "Anchor", "type": "Company", "aka": []}
    )
    _seed_entity_candidates(
        [
            _entity_candidate_row(
                source_slug="alpha_inc",
                target_slug="anchor",
                source="Alpha Inc",
                target="Anchor",
            ),
            _entity_candidate_row(
                source_slug="beta_inc",
                target_slug="anchor",
                source="Beta Inc",
                target="Anchor",
            ),
        ]
    )

    batch = accept_entity_candidate_batch(
        [
            {
                "facet": "work",
                "source_slug": "alpha_inc",
                "target_slug": "anchor",
            },
            {
                "facet": "work",
                "source_slug": "beta_inc",
                "target_slug": "anchor",
            },
        ]
    )
    undone = undo_entity_merge(batch["results"][0]["merge_id"], caller="test.curation")

    assert undone["undone"] is True
    assert load_journal_entity("alpha_inc")["name"] == "Alpha Inc"
    assert load_journal_entity("beta_inc") is None
    anchor = load_journal_entity("anchor")
    assert "Beta Inc" in anchor["aka"]
    assert "Beta Alias" in anchor["aka"]
    assert "Alpha Inc" not in anchor["aka"]
    assert "Alpha Alias" not in anchor["aka"]


def test_accept_entity_candidate_batch_malformed_first_continues(curation_journal):
    _seed_entities()
    _seed_entity_candidate()

    result = accept_entity_candidate_batch(
        [
            {"facet": "work", "source_slug": "missing_target"},
            {"facet": "work", "source_slug": "kognova_inc", "target_slug": "kognova"},
        ]
    )

    assert result["accepted"] == 1
    assert result["failed"] == 1
    assert len(result["results"]) == 2
    assert result["results"][0] == {
        "facet": "work",
        "source_slug": "missing_target",
        "target_slug": "",
        "status": "error",
        "error": "candidate is missing facet, source_slug, or target_slug",
    }
    _assert_folded("kognova_inc", "kognova", "Kognova Inc")


def test_accept_entity_candidate_batch_lock_timeout_continues(
    curation_journal,
    monkeypatch,
):
    _seed_entities()
    _seed_entity_candidates(
        [
            _entity_candidate_row(
                "busy_inc",
                "busy",
                source="Busy Inc",
                target="Busy",
            ),
            _entity_candidate_row(),
        ]
    )
    real_merge = curation.merge_entity

    def raise_busy_for_one(source_id: str, target_id: str, **kwargs):
        if source_id == "busy_inc":
            raise LockTimeout(Path("busy"), 0.01)
        return real_merge(source_id, target_id, **kwargs)

    monkeypatch.setattr(curation, "merge_entity", raise_busy_for_one)

    result = accept_entity_candidate_batch(
        [
            {"facet": "work", "source_slug": "busy_inc", "target_slug": "busy"},
            {"facet": "work", "source_slug": "kognova_inc", "target_slug": "kognova"},
        ]
    )

    assert result["accepted"] == 1
    assert result["failed"] == 1
    assert result["results"][0]["status"] == "error"
    assert (
        result["results"][0]["error"] == "entity merge candidates are busy; try again"
    )
    _assert_folded("kognova_inc", "kognova", "Kognova Inc")


def test_accept_entity_candidate_batch_counts_already_accepted(curation_journal):
    _seed_entities()
    _seed_entity_candidates(
        [
            _entity_candidate_row(
                "accepted_inc",
                "accepted",
                source="Accepted Inc",
                target="Accepted",
                status="accepted",
            ),
            _entity_candidate_row(),
        ]
    )

    result = accept_entity_candidate_batch(
        [
            {
                "facet": "work",
                "source_slug": "accepted_inc",
                "target_slug": "accepted",
            },
            {"facet": "work", "source_slug": "kognova_inc", "target_slug": "kognova"},
        ]
    )

    assert result["accepted"] == 2
    assert result["failed"] == 0
    assert [row["status"] for row in result["results"]] == [
        "already_accepted",
        "accepted",
    ]
    _assert_folded("kognova_inc", "kognova", "Kognova Inc")


def test_dismiss_entity_candidate_batch_dismisses_many(curation_journal):
    _seed_entity_candidates(
        [
            _entity_candidate_row(),
            _entity_candidate_row(
                "octo_labs_inc",
                "octo_labs",
                source="Octo Labs Inc",
                target="Octo Labs",
            ),
        ]
    )

    result = dismiss_entity_candidate_batch(
        [
            {"facet": "work", "source_slug": "kognova_inc", "target_slug": "kognova"},
            {
                "facet": "work",
                "source_slug": "octo_labs_inc",
                "target_slug": "octo_labs",
            },
        ]
    )

    assert result["dismissed"] == 2
    assert result["failed"] == 0
    assert len(result["results"]) == 2
    assert all(
        set(row) == {"facet", "source_slug", "target_slug", "status", "error"}
        for row in result["results"]
    )
    assert [row["status"] for row in load_entity_candidates()] == [
        "dismissed",
        "dismissed",
    ]


def test_dismiss_entity_candidate_batch_malformed_first_continues(curation_journal):
    _seed_entity_candidate()

    result = dismiss_entity_candidate_batch(
        [
            {"facet": "work", "source_slug": "missing_target"},
            {"facet": "work", "source_slug": "kognova_inc", "target_slug": "kognova"},
        ]
    )

    assert result["dismissed"] == 1
    assert result["failed"] == 1
    assert result["results"][0]["status"] == "error"
    assert result["results"][0]["error"] == (
        "candidate is missing facet, source_slug, or target_slug"
    )
    assert load_entity_candidates()[0]["status"] == "dismissed"


def test_dismiss_entity_candidate_batch_counts_already_dismissed(curation_journal):
    _seed_entity_candidates(
        [
            _entity_candidate_row(
                "dismissed_inc",
                "dismissed",
                source="Dismissed Inc",
                target="Dismissed",
                status="dismissed",
            ),
            _entity_candidate_row(),
        ]
    )

    result = dismiss_entity_candidate_batch(
        [
            {
                "facet": "work",
                "source_slug": "dismissed_inc",
                "target_slug": "dismissed",
            },
            {"facet": "work", "source_slug": "kognova_inc", "target_slug": "kognova"},
        ]
    )

    assert result["dismissed"] == 2
    assert result["failed"] == 0
    assert [row["status"] for row in result["results"]] == [
        "already_dismissed",
        "dismissed",
    ]


def test_accept_speaker_candidate_pair_merges_tracker_without_entity_merge(
    curation_journal,
    monkeypatch,
):
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def merge_entity_spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((args, kwargs))
        raise AssertionError("speaker candidate-pair accept must not merge entities")

    monkeypatch.setattr(curation, "merge_entity", merge_entity_spy)
    left = _candidate_profile(1, _unit([1.0, 0.0]))
    right = _candidate_profile(
        2,
        _unit([0.70, np.sqrt(1.0 - 0.70**2)]),
        status="confirmed",
        confirmed_entity="alice_test",
    )
    tracker = _seed_candidate_tracker(curation_journal, [left, right])
    anchor_a, anchor_b = _record_pair_for_candidates(
        tracker.load_all_candidates()[0],
        tracker.load_all_candidates()[1],
    )

    result = accept_speaker_candidate_pair(anchor_b, anchor_a)

    assert calls == []
    assert result["status"] == "accepted"
    assert result["kind"] == KIND_SPEAKER_CANDIDATE_PAIR
    assert result["merge"]["status"] == "merged"
    assert result["undo"] == {
        "available": False,
        "merge_id": None,
        "reason": "Speaker candidate-pair merges cannot be undone.",
    }
    survivor = CandidateTracker(
        curation_journal / "awareness" / "speaker_candidates.json"
    ).load_all_candidates()
    assert len(survivor) == 1
    assert survivor[0].cand_id == 1
    assert survivor[0].status == "confirmed"
    assert survivor[0].confirmed_entity == "alice_test"
    assert load_pair_candidates()[0]["status"] == "accepted"


def test_accept_speaker_candidate_pair_stale_tracker_error_keeps_row_open(
    curation_journal,
    monkeypatch,
):
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        curation,
        "merge_entity",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    left = _candidate_profile(1, _unit([1.0, 0.0]))
    right = _candidate_profile(2, _unit([0.70, np.sqrt(1.0 - 0.70**2)]))
    anchor_a, anchor_b = _record_pair_for_candidates(left, right)

    result = accept_speaker_candidate_pair(anchor_a, anchor_b)

    assert calls == []
    assert result == {
        "status": "error",
        "kind": KIND_SPEAKER_CANDIDATE_PAIR,
        "key": curation._speaker_candidate_pair_key(anchor_a, anchor_b),
        "error": "candidate anchor not found",
    }
    assert load_pair_candidates()[0]["status"] == "open"
    assert (
        CandidateTracker(
            curation_journal / "awareness" / "speaker_candidates.json"
        ).load_all_candidates()
        == []
    )


def test_dismiss_speaker_candidate_pair_sets_status_and_removes_open_item(
    curation_journal,
):
    left = _candidate_profile(1, _unit([1.0, 0.0]))
    right = _candidate_profile(2, _unit([0.62, np.sqrt(1.0 - 0.62**2)]))
    anchor_a, anchor_b = _record_pair_for_candidates(left, right, similarity=0.62)

    result = dismiss_speaker_candidate_pair(anchor_b, anchor_a)

    assert result["status"] == "dismissed"
    assert result["kind"] == KIND_SPEAKER_CANDIDATE_PAIR
    assert load_pair_candidates()[0]["status"] == "dismissed"
    assert [
        item for item in load_open_items() if item.kind == KIND_SPEAKER_CANDIDATE_PAIR
    ] == []


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
