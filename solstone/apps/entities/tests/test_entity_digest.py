# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from solstone.apps.entities.talent.entity_digest import (
    assemble_facet_day_digest,
    pre_process,
)
from solstone.think.activities import assemble_activity_records_and_narratives

GOLDEN_MONTAGUE_20260306 = (
    "## Existing records\n"
    "- id=engineering_143000_300 | activity=engineering | title=Deep work on Verona Platform integration | description=Deep work on Verona Platform integration | segments=143000_300 | active_entities=romeo_montague, juliet_capulet\n"
    "\n"
    "## Per-span narratives\n"
    "### engineering_143000_300/session_review.md\n"
    "segment_key=143000_300\n"
    "\n"
    "# Engineering Session Review\n"
    "\n"
    "## Summary\n"
    "Deep integration work on the Verona Platform. Romeo focused on routing layer while Juliet handled schema translation.\n"
    "\n"
    "## Key Changes\n"
    "- Integrated mesh routing with schema translation pipeline\n"
    "- Achieved end-to-end test coverage\n"
    "- Platform renamed from Balcony App to Verona Platform\n"
    "\n"
    "## Engagement\n"
    "High focus, pair programming session."
)


def test_assemble_facet_day_digest_includes_records_narratives_and_entities(
    journal_copy,
):
    digest = assemble_facet_day_digest("montague", "20260306")

    assert "# Entity digest for montague on 20260306" in digest
    assert "## Existing records" in digest
    assert "## Per-span narratives" in digest
    assert "## Detected entities (this day)" in digest
    assert "## Already-attached entities" in digest
    assert "engineering_143000_300" in digest
    assert "schema translation" in digest
    assert "Romeo Montague" in digest
    assert "Verona Platform" in digest
    assert "Montague Tech" in digest


def test_assemble_activity_records_and_narratives_byte_identical(journal_copy):
    assert (
        assemble_activity_records_and_narratives("montague", "20260306")
        == GOLDEN_MONTAGUE_20260306
    )


def test_pre_process_returns_facet_day_digest_template_vars(journal_copy):
    result = pre_process({"facet": "montague", "day": "20260306"})

    assert isinstance(result, dict)
    assert result["template_vars"]["facet_day_digest"]


def test_pre_process_missing_facet_or_day_returns_none():
    assert pre_process({"day": "20260306"}) is None
    assert pre_process({"facet": "montague"}) is None


def test_assemble_facet_day_digest_empty_sections_render_placeholders(journal_copy):
    digest = assemble_facet_day_digest("empty-entities", "20990101")

    assert "No existing activity records." in digest
    assert "No per-span narratives." in digest
    assert "No entities detected on this day." in digest
    assert "No entities attached to this facet." in digest


def test_assemble_facet_day_digest_skips_bad_records_and_narratives(journal_copy):
    facet = "empty-entities"
    day = "20990102"
    activities_dir = journal_copy / "facets" / facet / "activities"
    records_path = activities_dir / f"{day}.jsonl"
    records_path.parent.mkdir(parents=True, exist_ok=True)
    records_path.write_text(
        "\n".join(
            [
                "{bad json",
                json.dumps(
                    {
                        "id": "meeting_090000_300",
                        "activity": "meeting",
                        "title": "Valid activity",
                        "description": "Valid activity description",
                        "segments": ["090000_300"],
                        "active_entities": ["alice"],
                        "created_at": 1,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    narrative_dir = activities_dir / day / "meeting_090000_300"
    narrative_dir.mkdir(parents=True, exist_ok=True)
    (narrative_dir / "bad.md").write_bytes(b"\xff")
    (narrative_dir / "good.md").write_text("Good narrative body", encoding="utf-8")

    entities_dir = journal_copy / "facets" / facet / "entities"
    entities_dir.mkdir(parents=True, exist_ok=True)
    (entities_dir / f"{day}.jsonl").write_text(
        "\n".join(
            [
                "{bad json",
                json.dumps(
                    {
                        "type": "Person",
                        "name": "Valid Detected",
                        "description": "Valid detected description",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    digest = assemble_facet_day_digest(facet, day)

    assert "meeting_090000_300" in digest
    assert "Valid activity description" in digest
    assert "meeting_090000_300/good.md" in digest
    assert "Good narrative body" in digest
    assert "meeting_090000_300/bad.md" not in digest
    assert "Valid Detected" in digest
    assert "Valid detected description" in digest
    assert "## Detected entities (this day)" in digest
    assert "## Already-attached entities" in digest


def test_digest_route_returns_digest_content(client):
    response = client.get("/app/entities/api/montague/digest?day=20260306")

    assert response.status_code == 200
    body = response.get_json()
    assert body["facet"] == "montague"
    assert body["day"] == "20260306"
    assert body["content"] == assemble_facet_day_digest("montague", "20260306")


def test_digest_route_requires_day(client):
    response = client.get("/app/entities/api/montague/digest")

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "missing_required_field"
