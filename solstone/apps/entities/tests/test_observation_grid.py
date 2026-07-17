# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from typing import Any

from solstone.think.entities import (
    attach_or_reactivate_entity,
    create_journal_entity,
    entity_slug,
    save_facet_relationship,
    save_observations,
)


def _row(content: str, source_day: Any = "20260401") -> dict[str, Any]:
    row = {"content": content, "observed_at": 1000}
    if source_day is not None:
        row["source_day"] = source_day
    return row


def _attach(facet: str, name: str) -> str:
    attach_or_reactivate_entity(
        facet,
        entity_type="Person",
        name=name,
        description="Test relationship",
    )
    return entity_slug(name)


def test_entity_observation_grid_returns_dated_days(client):
    facet = "work"
    name = "Grid Route Person"
    entity_id = _attach(facet, name)
    save_observations(
        facet,
        name,
        [
            _row("First", "20260401"),
            _row("Second", "20260401"),
            _row("Third", "20260402"),
        ],
    )

    response = client.get(f"/app/entities/api/{facet}/entity/{entity_id}/grid")

    assert response.status_code == 200
    assert response.get_json() == {
        "coverage": {"start": "20260401", "end": "20260402"},
        "days": {"20260401": 2, "20260402": 1},
        "pending": {},
    }


def test_entity_observation_grid_empty_when_no_dated_observations(client):
    facet = "work"
    name = "Undated Grid Person"
    entity_id = _attach(facet, name)
    save_observations(
        facet,
        name,
        [
            _row("No source day", None),
            _row("Integer source day", 1),
            _row("Dashed source day", "2026-04-01"),
        ],
    )

    response = client.get(f"/app/entities/api/{facet}/entity/{entity_id}/grid")

    assert response.status_code == 200
    assert response.get_json() == {"coverage": None, "days": {}, "pending": {}}


def test_entity_observation_grid_journal_only_and_unknown_are_distinct(client):
    create_journal_entity(
        "journal_only_grid_person",
        "Journal Only Grid Person",
        "Person",
        skip_principal=True,
    )

    journal_only = client.get(
        "/app/entities/api/work/entity/journal_only_grid_person/grid"
    )
    unknown = client.get("/app/entities/api/work/entity/missing_grid_person/grid")

    assert journal_only.status_code == 200
    assert journal_only.get_json() == {"coverage": None, "days": {}, "pending": {}}
    assert unknown.status_code == 404
    assert unknown.get_json()["reason_code"] == "entity_not_found"


def test_entity_observation_grid_keys_observations_by_resolved_name(client):
    facet = "work"
    entity_id = "custom_grid_identity"
    name = "Resolved Grid Name"
    create_journal_entity(entity_id, name, "Person", skip_principal=True)
    save_facet_relationship(
        facet,
        entity_id,
        {
            "description": "Custom id relationship",
            "attached_at": 1000,
            "updated_at": 1000,
        },
    )
    save_observations(facet, name, [_row("Stored under resolved name", "20260403")])

    response = client.get(f"/app/entities/api/{facet}/entity/{entity_id}/grid")

    assert response.status_code == 200
    assert response.get_json()["days"] == {"20260403": 1}


def test_entity_observation_grid_uses_day_grid_payload_builder(client, monkeypatch):
    from solstone.apps.entities import routes

    facet = "work"
    name = "Builder Spy Grid Person"
    entity_id = _attach(facet, name)
    save_observations(facet, name, [_row("Builder input", "20260404")])
    calls = []

    def spy_builder(counts, watermark, **kwargs):
        calls.append((dict(counts), watermark, kwargs))
        return {
            "coverage": {"start": "spy", "end": "spy"},
            "days": {"spy": 1},
            "pending": {},
        }

    monkeypatch.setattr(routes, "build_day_grid_payload", spy_builder)

    response = client.get(f"/app/entities/api/{facet}/entity/{entity_id}/grid")

    assert response.status_code == 200
    assert response.get_json() == {
        "coverage": {"start": "spy", "end": "spy"},
        "days": {"spy": 1},
        "pending": {},
    }
    assert calls == [({"20260404": 1}, "20260404", {})]
