# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from solstone.apps.entities.talent import entities_review
from solstone.think.entities import (
    attach_or_reactivate_entity,
    entity_slug,
    load_entities,
)
from solstone.think.entities.journal import load_journal_entity
from solstone.think.entities.review_candidates import (
    load_candidates,
    record_merge_candidate,
)
from solstone.think.entities.saving import save_entities

DAY = "20250115"
FACET = "work"


def _set_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    return tmp_path


def _write_facet(root: Path, slug: str, description: str = "Professional work") -> None:
    facet_dir = root / "facets" / slug
    facet_dir.mkdir(parents=True, exist_ok=True)
    (facet_dir / "facet.json").write_text(
        json.dumps(
            {
                "title": slug.title(),
                "description": description,
                "color": "#00796b",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _entity(entity_type: str, name: str, description: str) -> dict:
    return {"type": entity_type, "name": name, "description": description}


def _save_detected(
    facet: str,
    day: str,
    *rows: dict,
) -> None:
    save_entities(facet, list(rows), day=day)


def _context() -> dict:
    return {"facet": FACET, "day": DAY}


def _outcome(root: Path, facet: str = FACET, day: str = DAY) -> dict:
    path = root / "facets" / facet / "entities" / f"{day}_review_outcome.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _valid_result(
    *, promotions: list[dict] | None = None, merges: list[dict] | None = None
) -> str:
    return json.dumps({"promotions": promotions or [], "merges": merges or []})


def _promotion(
    name: str,
    description: str = "Stable useful context.",
    *,
    promote: bool = True,
    aliases: list[str] | None = None,
) -> dict:
    return {
        "name": name,
        "description": description,
        "promote": promote,
        "aliases": aliases or [],
    }


def _merge(
    source: str, canonical: str, evidence: str = "Looks like the same thing."
) -> dict:
    return {"source": source, "canonical": canonical, "evidence": evidence}


def test_build_review_inputs_aggregates_threshold_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _write_facet(tmp_path, FACET)
    _save_detected(
        FACET,
        "20250110",
        _entity("Tool", "Vector DB", "Benchmarked retrieval."),
        _entity("Methodology", "Review Loop", "Used for planning."),
    )
    _save_detected(
        FACET,
        "20250111",
        _entity("Tool", "Vector DB", "Tuned indexing."),
        _entity("Methodology", "Review Loop", "Used for follow-up."),
    )
    _save_detected(
        FACET,
        "20250112",
        _entity("Company", "Acme Corp", "Discussed pricing."),
        _entity("Company", "Beta Inc", "Reviewed contract."),
        _entity("Tool", "Vector DB", "Checked query quality."),
        _entity("Methodology", "Review Loop", "Used for synthesis."),
    )
    _save_detected(
        FACET,
        "20250113",
        _entity("Person", "Sarah Chen", "Reviewed design."),
        _entity("Company", "Acme Corp", "Mentioned renewal."),
        _entity("Company", "Beta Inc", "Met with legal."),
        _entity("Tool", "Vector DB", "Compared latency."),
        _entity("Methodology", "Review Loop", "Used for decisions."),
    )
    _save_detected(
        FACET,
        "20250114",
        _entity("Person", "Sarah Chen", "Approved launch."),
        _entity("Company", "Beta Inc", "Signed terms."),
        _entity("Tool", "Vector DB", "Shipped changes."),
        _entity("Methodology", "Review Loop", "Closed the review."),
    )

    eligible, _, _ = entities_review.build_review_inputs(FACET, DAY)

    by_name = {row["name"]: row for row in eligible}
    assert by_name["Sarah Chen"]["day_count"] == 2
    assert by_name["Sarah Chen"]["type"] == "Person"
    assert by_name["Beta Inc"]["day_count"] == 3
    assert by_name["Vector DB"]["day_count"] == 5
    assert by_name["Review Loop"]["day_count"] == 5
    assert "Acme Corp" not in by_name
    assert [context["day"] for context in by_name["Sarah Chen"]["contexts"]] == [
        "20250113",
        "20250114",
    ]


def test_build_review_inputs_excludes_run_day_from_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _write_facet(tmp_path, FACET)
    _save_detected(
        FACET, "20250113", _entity("Person", "Sarah Chen", "Reviewed design.")
    )
    _save_detected(
        FACET, "20250114", _entity("Person", "Sarah Chen", "Approved launch.")
    )
    _save_detected(FACET, DAY, _entity("Person", "Sarah Chen", "Met on run day."))

    eligible, _, _ = entities_review.build_review_inputs(FACET, DAY)

    by_name = {row["name"]: row for row in eligible}
    assert by_name["Sarah Chen"]["day_count"] == 2
    assert [context["day"] for context in by_name["Sarah Chen"]["contexts"]] == [
        "20250113",
        "20250114",
    ]


def test_build_review_inputs_drops_type_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _write_facet(tmp_path, FACET)
    _save_detected(FACET, "20250113", _entity("Person", "Mercury", "Met with Mercury."))
    _save_detected(FACET, "20250114", _entity("Project", "Mercury", "Built Mercury."))

    eligible, _, _ = entities_review.build_review_inputs(FACET, DAY)

    assert eligible == []


def test_build_review_inputs_excludes_high_confidence_attached_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _write_facet(tmp_path, FACET)
    _save_detected(
        FACET, "20250113", _entity("Person", "Sarah Chen", "Reviewed design.")
    )
    _save_detected(
        FACET, "20250114", _entity("Person", "Sarah Chen", "Approved launch.")
    )
    attach_or_reactivate_entity(
        FACET,
        entity_type="Person",
        name="Sarah Chen",
        description="Existing backend lead.",
    )

    eligible, _, _ = entities_review.build_review_inputs(FACET, DAY)

    assert eligible == []


def test_variant_hints_surface_variants_and_skip_same_slug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _write_facet(tmp_path, FACET)
    _save_detected(
        FACET,
        "20250114",
        _entity("Company", "Kognova", "Discussed roadmap."),
        _entity("Company", "Kognova Inc", "Reviewed pricing."),
        _entity("Company", "Acme Corp", "Mentioned account."),
        _entity("Company", "Acme  Corp", "Duplicate spacing."),
    )

    _, variant_hints, _ = entities_review.build_review_inputs(FACET, DAY)

    assert ("Kognova", "Kognova Inc") in variant_hints
    assert not any(
        frozenset((entity_slug(a), entity_slug(b))) == frozenset(("acme_corp",))
        for a, b in variant_hints
    )


def test_pre_process_surfaces_prior_merges_in_packet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _write_facet(tmp_path, FACET)
    _save_detected(
        FACET,
        "20250114",
        _entity("Company", "Kognova", "Discussed roadmap."),
        _entity("Company", "Kognova Inc", "Reviewed pricing."),
    )
    record_merge_candidate(
        facet=FACET,
        day="20250114",
        source="Kognova Inc",
        source_slug="kognova_inc",
        target="Kognova",
        target_slug="kognova",
        evidence="Prior review.",
    )

    result = entities_review.pre_process(_context())

    assert isinstance(result, dict)
    packet = result["template_vars"]["review_packet"]
    assert "Kognova / Kognova Inc" in packet
    assert "Kognova Inc -> Kognova (open)" in packet
    assert "sol call" not in packet


def test_pre_process_skip_taxonomy_no_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _write_facet(tmp_path, FACET)

    assert entities_review.pre_process({"facet": FACET}) == {"skip_reason": "no_day"}
    assert entities_review.pre_process({"day": DAY}) == {"skip_reason": "no_facet"}
    assert entities_review.pre_process(_context()) == {"skip_reason": "no_candidates"}
    assert not (
        tmp_path / "facets" / FACET / "entities" / f"{DAY}_review_outcome.json"
    ).exists()


def test_entities_review_schema_validates_sample_and_rejects_malformed():
    schema_path = Path(__file__).parents[1] / "talent" / "entities_review.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    validator.validate(
        {
            "promotions": [
                {
                    "name": "Sarah Chen",
                    "description": "Backend lead for launch planning.",
                    "promote": True,
                    "aliases": ["S Chen"],
                }
            ],
            "merges": [
                {
                    "source": "Kognova Inc",
                    "canonical": "Kognova",
                    "evidence": "Same company name.",
                }
            ],
        }
    )
    with pytest.raises(ValidationError):
        validator.validate(
            {
                "promotions": [
                    {
                        "name": "Sarah Chen",
                        "description": "Backend lead.",
                        "promote": True,
                        "aliases": [],
                        "type": "Person",
                    }
                ],
                "merges": [],
            }
        )
    with pytest.raises(ValidationError):
        validator.validate({"promotions": []})


def test_post_process_promotes_aliases_records_merge_and_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _write_facet(tmp_path, FACET)
    _save_detected(
        FACET, "20250113", _entity("Person", "Sarah Chen", "Reviewed design.")
    )
    _save_detected(
        FACET,
        "20250114",
        _entity("Person", "Sarah Chen", "Approved launch."),
        _entity("Company", "Kognova", "Discussed roadmap."),
        _entity("Company", "Kognova Inc", "Reviewed pricing."),
    )

    result = entities_review.post_process(
        _valid_result(
            promotions=[
                _promotion(
                    "Sarah Chen",
                    "Backend lead for launch planning.",
                    aliases=["S Chen"],
                )
            ],
            merges=[_merge("Kognova Inc", "Kognova", "Same company name.")],
        ),
        _context(),
    )

    assert result is None
    attached = load_entities(FACET)
    assert len(attached) == 1
    assert attached[0]["name"] == "Sarah Chen"
    assert attached[0]["type"] == "Person"
    assert attached[0]["description"] == "Backend lead for launch planning."
    assert "S Chen" in load_journal_entity("sarah_chen")["aka"]
    candidates = load_candidates()
    assert len(candidates) == 1
    assert candidates[0]["source"] == "Kognova Inc"
    assert candidates[0]["target"] == "Kognova"
    outcome = _outcome(tmp_path)
    assert outcome["promoted"] == 1
    assert outcome["aliased"] == 1
    assert outcome["merges"] == 1
    assert outcome["skipped"] == 0
    assert outcome["errored"] == 0
    assert outcome["error"] is None


def test_post_process_bad_json_writes_zero_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _write_facet(tmp_path, FACET)
    _save_detected(
        FACET, "20250113", _entity("Person", "Sarah Chen", "Reviewed design.")
    )
    _save_detected(
        FACET, "20250114", _entity("Person", "Sarah Chen", "Approved launch.")
    )

    assert entities_review.post_process("{bad json", _context()) is None

    outcome = _outcome(tmp_path)
    assert outcome["promoted"] == 0
    assert outcome["aliased"] == 0
    assert outcome["merges"] == 0
    assert outcome["skipped"] == 0
    assert outcome["errored"] == 0
    assert outcome["error"] is None


def test_post_process_counts_dropped_rows_without_disk_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _write_facet(tmp_path, FACET)
    _save_detected(
        FACET, "20250113", _entity("Person", "Sarah Chen", "Reviewed design.")
    )
    _save_detected(
        FACET, "20250114", _entity("Person", "Sarah Chen", "Approved launch.")
    )

    entities_review.post_process(
        _valid_result(
            promotions=[
                _promotion("Invented Person", "Invented context."),
                _promotion("Sarah Chen", "Backend lead.", promote=False),
            ],
            merges=[
                _merge("Sarah Chen", "Sarah Chen", "Self merge."),
                _merge("Alpha", "Beta", "Not hinted."),
            ],
        ),
        _context(),
    )

    assert load_entities(FACET) == []
    assert load_candidates() == []
    outcome = _outcome(tmp_path)
    assert outcome["skipped"] == 4
    assert outcome["errored"] == 0


def test_post_process_idempotent_for_existing_promotion_and_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _write_facet(tmp_path, FACET)
    _save_detected(
        FACET, "20250113", _entity("Person", "Sarah Chen", "Reviewed design.")
    )
    _save_detected(
        FACET,
        "20250114",
        _entity("Person", "Sarah Chen", "Approved launch."),
        _entity("Company", "Kognova", "Discussed roadmap."),
        _entity("Company", "Kognova Inc", "Reviewed pricing."),
    )
    result = _valid_result(
        promotions=[
            _promotion(
                "Sarah Chen",
                "Backend lead for launch planning.",
                aliases=["S Chen"],
            )
        ],
        merges=[_merge("Kognova Inc", "Kognova", "Same company name.")],
    )

    entities_review.post_process(result, _context())
    entities_review.post_process(result, _context())

    attached = load_entities(FACET)
    assert [entity["name"] for entity in attached] == ["Sarah Chen"]
    pair_rows = [
        row
        for row in load_candidates()
        if row["source_slug"] == "kognova_inc" and row["target_slug"] == "kognova"
    ]
    assert len(pair_rows) == 1


def test_post_process_records_substrate_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _set_journal(tmp_path, monkeypatch)
    _write_facet(tmp_path, FACET)
    _save_detected(
        FACET, "20250113", _entity("Person", "Sarah Chen", "Reviewed design.")
    )
    _save_detected(
        FACET, "20250114", _entity("Person", "Sarah Chen", "Approved launch.")
    )

    def fail_attach(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(entities_review, "attach_or_reactivate_entity", fail_attach)

    entities_review.post_process(
        _valid_result(promotions=[_promotion("Sarah Chen", "Backend lead.")]),
        _context(),
    )

    outcome = _outcome(tmp_path)
    assert outcome["errored"] == 1
    assert "RuntimeError: boom" in outcome["error"]
