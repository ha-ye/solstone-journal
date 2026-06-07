# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from typing import Any

from solstone.apps.entities.routes import get_journal_entities_data
from solstone.think.entities.observations import add_observation, count_observations


def _entity(name: str) -> dict[str, Any]:
    return {
        "type": "Person",
        "name": name,
        "description": "Test entity",
        "attached_at": 1000,
        "updated_at": 1000,
    }


def test_observation_count_reflects_fresh_writes(entity_env):
    facet = "personal"
    entity_name = "Alice Johnson"
    entity_env(
        attached=[_entity(entity_name)],
        observations=["Prefers async updates"],
        observation_entity=entity_name,
        facet=facet,
    )

    assert count_observations(facet, entity_name) == 1

    add_observation(facet, entity_name, "Prefers morning meetings", "20260427")

    assert count_observations(facet, entity_name) == 2


def test_journal_entities_data_reflects_fresh_relationship_writes(entity_env):
    facet = "personal"
    entity_name = "Alice Johnson"
    journal = entity_env(attached=[_entity(entity_name)], facet=facet)
    facet_dir = journal / "facets" / facet
    facet_dir.mkdir(parents=True, exist_ok=True)
    (facet_dir / "facet.json").write_text(
        json.dumps({"title": "Personal", "description": "Personal facet"}),
        encoding="utf-8",
    )

    first = get_journal_entities_data()
    assert len(first["entities"]) == 1
    assert first["entities"][0]["facets"][0]["description"] == "Test entity"

    relationship_path = (
        journal / "facets" / facet / "entities" / "alice_johnson" / "entity.json"
    )
    relationship = json.loads(relationship_path.read_text(encoding="utf-8"))
    relationship["description"] = "Updated relationship"
    relationship_path.write_text(
        json.dumps(relationship, indent=2) + "\n",
        encoding="utf-8",
    )

    second = get_journal_entities_data()
    assert len(second["entities"]) == 1
    assert second["entities"][0]["facets"][0]["description"] == "Updated relationship"
