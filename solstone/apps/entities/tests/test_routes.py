# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Route adapter tests for entity write owner delegation."""

from __future__ import annotations

from pathlib import Path

from solstone.think.entities import (
    ResolutionScope,
    attach_or_reactivate_entity,
    detach_facet_entity,
    load_ambiguities,
    load_entities,
    load_facet_relationship,
    load_journal_entity,
    record_ambiguity_choice,
    save_entities,
)
from solstone.think.journal_io import LockTimeout


def test_add_entity_returns_created_resource(client):
    response = client.post(
        "/app/entities/api/personal",
        json={
            "type": "Person",
            "name": "Diana Prince",
            "description": "Friend",
        },
    )

    data = response.get_json()
    assert response.status_code == 201
    assert data["id"] == "diana_prince"
    assert data["name"] == "Diana Prince"
    assert data["type"] == "Person"
    assert data["description"] == "Friend"
    assert "attached_at" in data
    assert "updated_at" in data
    assert "success" not in data


def test_add_entity_reattaches_detached_relationship(client):
    attach_or_reactivate_entity(
        "personal",
        entity_type="Person",
        name="Detached Route Person",
        description="Old",
    )
    detach_facet_entity("personal", "detached_route_person")

    response = client.post(
        "/app/entities/api/personal",
        json={
            "type": "Person",
            "name": "Detached Route Person",
            "description": "New",
        },
    )

    data = response.get_json()
    relationship = load_facet_relationship("personal", "detached_route_person")
    assert response.status_code == 200
    assert data["success"] is True
    assert data["reattached"] is True
    assert "detached" not in relationship
    assert relationship["description"] == "New"


def test_detach_entity_by_id(client):
    attach_or_reactivate_entity(
        "personal",
        entity_type="Person",
        name="Detach Route Person",
        description="Friend",
    )

    response = client.delete("/app/entities/api/personal/entity/detach_route_person")

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert (
        load_facet_relationship("personal", "detach_route_person")["detached"] is True
    )


def test_update_description_by_id(client):
    attach_or_reactivate_entity(
        "personal",
        entity_type="Person",
        name="Description Route Person",
        description="Old",
    )

    response = client.put(
        "/app/entities/api/personal/entity/description_route_person/description",
        json={"description": "New"},
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert (
        load_facet_relationship("personal", "description_route_person")["description"]
        == "New"
    )


def test_add_aka_excludes_own_entity_by_id_when_request_name_misaligned(client):
    attach_or_reactivate_entity(
        "personal",
        entity_type="Person",
        name="Michael Bauer",
        description="Friend",
    )

    response = client.post(
        "/app/entities/api/personal/aka",
        json={
            "entity_id": "michael_bauer",
            "aka": "Michael Bauer (RadialNexus)",
            # Lowercase simulates the former name-exclusion mismatch; id exclusion
            # must still remove the current entity before fuzzy collision checks.
            "exclude_name": "michael bauer",
        },
    )

    assert 200 <= response.status_code < 300
    entity = load_journal_entity("michael_bauer")
    assert entity is not None
    assert "Michael Bauer (RadialNexus)" in entity["aka"]


def test_delete_detected_returns_days_modified(client):
    save_entities(
        "personal",
        [
            {"type": "Person", "name": "Detected Route Person", "description": "One"},
            {"type": "Tool", "name": "Keep Me", "description": "Two"},
        ],
        day="20240101",
    )

    response = client.delete(
        "/app/entities/api/personal/detected",
        json={"name": "Detected Route Person"},
    )

    assert response.status_code == 200
    assert response.get_json()["days_modified"] == ["20240101"]
    assert {entity["name"] for entity in load_entities("personal", "20240101")} == {
        "Keep Me"
    }


def test_detected_ambiguous_name_keeps_submitted_name_without_id(client):
    save_entities(
        "personal",
        [
            {"type": "Person", "name": "Ambig Route Alpha"},
            {"type": "Person", "name": "Ambig Route Beta"},
        ],
    )

    response = client.post(
        "/app/entities/api/personal/detected",
        json={
            "day": "20240102",
            "type": "Person",
            "entity": "Ambig",
            "description": "Detected ambiguous person",
        },
    )

    assert response.status_code == 200
    detected = load_entities("personal", "20240102")
    assert [entity["name"] for entity in detected] == ["Ambig"]
    rows = load_ambiguities()
    assert len(rows) == 1
    assert rows[0]["normalized_query"] == "ambig"

    record_ambiguity_choice(
        "Ambig",
        "ambig_route_alpha",
        load_entities("personal"),
        scope=ResolutionScope.facet_scope("personal"),
    )
    response = client.post(
        "/app/entities/api/personal/detected",
        json={
            "day": "20240103",
            "type": "Person",
            "entity": "Ambig",
            "description": "Detected resolved person",
        },
    )

    assert response.status_code == 200
    detected = load_entities("personal", "20240103")
    assert [entity["name"] for entity in detected] == ["Ambig Route Alpha"]


def test_owner_lock_timeout_maps_to_entity_busy(client, monkeypatch):
    def raise_busy(*args, **kwargs):
        raise LockTimeout(Path("busy"), 0.01)

    monkeypatch.setattr(
        "solstone.apps.entities.routes.attach_or_reactivate_entity",
        raise_busy,
    )

    response = client.post(
        "/app/entities/api/personal",
        json={"type": "Person", "name": "Busy Person", "description": "Friend"},
    )

    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "entity_busy"
