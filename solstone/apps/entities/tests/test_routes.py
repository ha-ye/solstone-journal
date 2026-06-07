# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Route adapter tests for entity write owner delegation."""

from __future__ import annotations

from pathlib import Path

from solstone.think.entities import (
    attach_or_reactivate_entity,
    detach_facet_entity,
    load_entities,
    load_facet_relationship,
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
