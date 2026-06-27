# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest

from solstone.think.entities.core import entity_slug
from solstone.think.entities.loading import load_entities
from solstone.think.entities.saving import (
    save_detected_entity,
    upsert_detection_segment,
)

DAY = "20250101"
SEG_A = "20250101/default/090000_300"
SEG_B = "20250101/default/100000_300"


def _set_journal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None


def _detection(
    name: str = "Sarah Chen",
    contribution: str = "Sarah reviewed the migration plan.",
    *,
    entity_type: str = "Person",
    activity: str = "planning",
    level: str = "high",
) -> dict:
    return {
        "name": name,
        "type": entity_type,
        "facet_activity": activity,
        "level": level,
        "contribution": contribution,
    }


def _entity(name: str = "Sarah Chen") -> dict:
    return next(e for e in load_entities("work", DAY) if e["name"] == name)


def test_upsert_detection_segment_creates_new_entity(tmp_path, monkeypatch):
    _set_journal(tmp_path, monkeypatch)

    result = upsert_detection_segment("work", DAY, SEG_A, [_detection()])

    assert result == {"wrote": 1, "retracted": 0, "dropped": 0}
    entity = _entity()
    assert entity["id"] == "sarah_chen"
    assert entity["type"] == "Person"
    assert entity["name"] == "Sarah Chen"
    assert entity["description"] == "Sarah reviewed the migration plan."
    assert entity["segments"] == [
        {
            "segment": SEG_A,
            "facet_activity": "planning",
            "level": "high",
            "contribution": "Sarah reviewed the migration plan.",
        }
    ]
    assert isinstance(entity["updated_at"], int)


def test_upsert_detection_segment_rolls_description_across_segments(
    tmp_path,
    monkeypatch,
):
    _set_journal(tmp_path, monkeypatch)

    upsert_detection_segment(
        "work", DAY, SEG_B, [_detection(contribution="Sarah approved the launch.")]
    )
    upsert_detection_segment(
        "work", DAY, SEG_A, [_detection(contribution="Sarah reviewed the plan.")]
    )

    entity = _entity()
    assert [row["segment"] for row in entity["segments"]] == [SEG_A, SEG_B]
    assert (
        entity["description"] == "Sarah reviewed the plan. Sarah approved the launch."
    )


def test_upsert_detection_segment_idempotent_reprocess(tmp_path, monkeypatch):
    _set_journal(tmp_path, monkeypatch)

    upsert_detection_segment("work", DAY, SEG_A, [_detection()])
    first = _entity()
    first_segments = first["segments"]
    first_description = first["description"]

    upsert_detection_segment("work", DAY, SEG_A, [_detection()])

    second = _entity()
    assert second["segments"] == first_segments
    assert second["description"] == first_description
    assert len(second["segments"]) == 1


def test_upsert_detection_segment_retracts_one_segment(tmp_path, monkeypatch):
    _set_journal(tmp_path, monkeypatch)

    upsert_detection_segment(
        "work", DAY, SEG_A, [_detection(contribution="Sarah reviewed the plan.")]
    )
    upsert_detection_segment(
        "work", DAY, SEG_B, [_detection(contribution="Sarah approved the launch.")]
    )

    result = upsert_detection_segment("work", DAY, SEG_A, [])

    assert result == {"wrote": 0, "retracted": 1, "dropped": 0}
    entity = _entity()
    assert [row["segment"] for row in entity["segments"]] == [SEG_B]
    assert entity["description"] == "Sarah approved the launch."


def test_upsert_detection_segment_drops_entity_after_last_retraction(
    tmp_path,
    monkeypatch,
):
    _set_journal(tmp_path, monkeypatch)

    upsert_detection_segment("work", DAY, SEG_A, [_detection()])
    result = upsert_detection_segment("work", DAY, SEG_A, [])

    assert result == {"wrote": 0, "retracted": 1, "dropped": 0}
    assert load_entities("work", DAY) == []


def test_upsert_detection_segment_preserves_extra_keys(tmp_path, monkeypatch):
    _set_journal(tmp_path, monkeypatch)
    path = tmp_path / "facets" / "work" / "entities" / f"{DAY}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "id": "sarah_chen",
                "type": "Person",
                "name": "Sarah Chen",
                "description": "Legacy",
                "custom": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    upsert_detection_segment("work", DAY, SEG_A, [_detection()])

    entity = _entity()
    assert entity["custom"] == 1
    assert entity["segments"][0]["segment"] == SEG_A


def test_upsert_detection_segment_coexists_tender_then_daily(
    tmp_path,
    monkeypatch,
):
    _set_journal(tmp_path, monkeypatch)

    upsert_detection_segment("work", DAY, SEG_A, [_detection()])
    with pytest.raises(ValueError, match="already detected"):
        save_detected_entity("work", DAY, "Person", "Sarah Chen", "Daily duplicate")

    save_detected_entity("work", DAY, "Person", "Bob Smith", "Daily row")

    entities = load_entities("work", DAY)
    assert {entity["name"] for entity in entities} == {"Sarah Chen", "Bob Smith"}
    tender = _entity()
    assert tender["id"] == "sarah_chen"
    assert tender["segments"][0]["segment"] == SEG_A


def test_upsert_detection_segment_coexists_daily_then_tender(
    tmp_path,
    monkeypatch,
):
    _set_journal(tmp_path, monkeypatch)

    save_detected_entity("work", DAY, "Person", "Sarah Chen", "Daily description")
    upsert_detection_segment("work", DAY, SEG_A, [_detection()])

    entity = _entity()
    assert entity["id"] == entity_slug("Sarah Chen")
    assert entity["description"] == "Sarah reviewed the migration plan."
    assert entity["segments"][0]["segment"] == SEG_A


def test_upsert_detection_segment_handles_legacy_line_without_id(
    tmp_path,
    monkeypatch,
):
    _set_journal(tmp_path, monkeypatch)
    path = tmp_path / "facets" / "work" / "entities" / f"{DAY}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "type": "Person",
                "name": "Sarah Chen",
                "description": "Legacy description",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    upsert_detection_segment("work", DAY, SEG_A, [_detection()])

    entity = _entity()
    assert entity["id"] == "sarah_chen"
    assert entity["segments"][0]["segment"] == SEG_A
    assert entity["description"] == "Sarah reviewed the migration plan."
