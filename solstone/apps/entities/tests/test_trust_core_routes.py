# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""HTTP integration tests for entity trust-core consumers."""

from __future__ import annotations

from pathlib import Path

import pytest

from solstone.apps.entities import routes as entity_routes
from solstone.convey import create_app
from solstone.think.entities import (
    EntityResolutionOutcome,
    ResolutionOrigin,
    ResolutionScope,
    iter_entity_history,
    load_all_journal_entities,
    load_journal_entity,
    record_entity_resolution,
    save_journal_entity,
)
from solstone.think.journal_io import LockTimeout


def _save(entity_id: str, name: str, **fields) -> None:
    save_journal_entity({"id": entity_id, "name": name, "type": "Person", **fields})


@pytest.fixture
def trust_client(speakers_env):
    env = speakers_env()
    return create_app(str(env.journal)).test_client()


def test_merge_preview_commit_history_and_recorded_undo(trust_client) -> None:
    client = trust_client
    _save("route_source", "Route Source", aka=["Source Alias"])
    _save("route_target", "Route Target")

    preview = client.post(
        "/app/entities/api/merge",
        json={"source_slug": "route_source", "target_slug": "route_target"},
    )
    assert preview.status_code == 200
    assert preview.get_json()["merged"] is False
    assert load_journal_entity("route_source") is not None

    committed = client.post(
        "/app/entities/api/merge",
        json={
            "source_slug": "route_source",
            "target_slug": "route_target",
            "commit": True,
        },
    )
    assert committed.status_code == 200
    body = committed.get_json()
    merge_id = body["merge_id"]
    assert body["merged"] is True
    assert body["undo"] == {
        "available": True,
        "merge_id": merge_id,
        "reason": None,
    }
    assert load_journal_entity("route_source") is None

    history = client.get("/app/entities/api/journal/entity/route_target/history")
    assert history.status_code == 200
    merge_event = next(
        item for item in history.get_json()["items"] if item["kind"] == "merge"
    )
    assert merge_event["merge_id"] == merge_id
    assert merge_event["merge_state"] == "open"

    undone = client.post(f"/app/entities/api/merge/{merge_id}/undo", json={})
    assert undone.status_code == 200
    undo_body = undone.get_json()
    assert undo_body["undone"] is True
    assert undo_body["history_version_id"].startswith("vh_")
    assert undo_body["restored_reference_counts"]
    assert undo_body["edge_rebuild"]["verified"] is True
    assert load_journal_entity("route_source")["name"] == "Route Source"

    history = client.get("/app/entities/api/journal/entity/route_target/history")
    merge_event = next(
        item for item in history.get_json()["items"] if item["kind"] == "merge"
    )
    assert merge_event["merge_state"] == "undone"

    repeated = client.post(f"/app/entities/api/merge/{merge_id}/undo", json={})
    assert repeated.status_code == 410
    assert repeated.get_json()["reason_code"] == "operation_no_longer_available"


def test_merge_route_uses_standard_error_envelopes(client) -> None:
    _save("same_entity", "Same Entity")

    same = client.post(
        "/app/entities/api/merge",
        json={
            "source_slug": "same_entity",
            "target_slug": "same_entity",
            "commit": True,
        },
    )
    assert same.status_code == 400
    assert same.get_json()["reason_code"] == "invalid_request_value"

    missing = client.post(
        "/app/entities/api/merge",
        json={
            "source_slug": "missing",
            "target_slug": "same_entity",
            "commit": True,
        },
    )
    assert missing.status_code == 404
    assert missing.get_json()["reason_code"] == "entity_not_found"


def test_history_and_ordinary_restore_routes(client) -> None:
    _save("versioned", "Version One")
    first_version = list(iter_entity_history("versioned"))[0]["version_id"]
    _save("versioned", "Version Two", aka=["V2"])

    history = client.get("/app/entities/api/journal/entity/versioned/history")
    assert history.status_code == 200
    history_items = history.get_json()["items"]
    assert [item["kind"] for item in history_items] == [
        "create",
        "update",
    ]
    assert all(item["restore_available"] for item in history_items)

    restored = client.post(
        "/app/entities/api/journal/entity/versioned/restore",
        json={"version_id": first_version},
    )
    assert restored.status_code == 200
    body = restored.get_json()
    assert body["entity"]["name"] == "Version One"
    assert body["event"]["kind"] == "restore"
    assert body["event"]["operation"]["restored_version_id"] == first_version

    missing = client.post(
        "/app/entities/api/journal/entity/versioned/restore",
        json={"version_id": "vh_missing"},
    )
    assert missing.status_code == 404


def test_ambiguity_list_resolve_and_sticky_choice_routes(client) -> None:
    _save("sarah_connor", "Sarah Connor")
    _save("sarah_lee", "Sarah Lee")
    entities = list(load_all_journal_entities().values())
    ambiguous = record_entity_resolution(
        "Sarah",
        entities,
        scope=ResolutionScope.journal(),
        origin=ResolutionOrigin(lane="test.route", field="name"),
    )
    assert ambiguous.outcome == EntityResolutionOutcome.AMBIGUOUS

    listed = client.get("/app/entities/api/ambiguities?status=open")
    assert listed.status_code == 200
    rows = listed.get_json()["items"]
    assert [row["ambiguity_id"] for row in rows] == [ambiguous.ambiguity_id]

    resolved = client.post(
        f"/app/entities/api/ambiguities/{ambiguous.ambiguity_id}/resolve",
        json={"entity_id": "sarah_lee"},
    )
    assert resolved.status_code == 200
    assert resolved.get_json()["ambiguity"]["status"] == "resolved"
    assert resolved.get_json()["entity"]["id"] == "sarah_lee"

    rerun = record_entity_resolution(
        "Sarah",
        list(load_all_journal_entities().values()),
        scope=ResolutionScope.journal(),
        origin=ResolutionOrigin(lane="test.route.retry", field="name"),
    )
    assert rerun.outcome == EntityResolutionOutcome.RESOLVED
    assert rerun.entity["id"] == "sarah_lee"
    assert (
        client.get("/app/entities/api/ambiguities?status=open").get_json()["items"]
        == []
    )


def test_repair_required_state_survives_undo_route(client, monkeypatch) -> None:
    monkeypatch.setattr(
        entity_routes,
        "undo_entity_merge",
        lambda merge_id, caller: {
            "error": {"code": "repair_required", "message": "rollback failed"},
            "merge_id": merge_id,
            "source_id": "source",
            "target_id": "target",
            "operation_state": "repair_required",
            "mutation_applied": True,
            "source_state": {"exists": True},
            "target_state": {"exists": True},
            "safe_remediation": "Inspect before retrying.",
        },
    )

    response = client.post("/app/entities/api/merge/em_repair/undo", json={})

    assert response.status_code == 500
    body = response.get_json()
    assert body["reason_code"] == "entity_operation_failed"
    assert body["operation_state"] == "repair_required"
    assert body["mutation_applied"] is True
    assert body["safe_remediation"] == "Inspect before retrying."


def test_trust_routes_cover_domain_and_lock_error_matrix(client, monkeypatch) -> None:
    _save("matrix_open", "Matrix Open")
    _save("matrix_blocked", "Matrix Blocked", blocked=True)

    for source, target, status, reason in (
        ("matrix_blocked", "matrix_open", 400, "entity_blocked"),
        ("matrix_open", "matrix_blocked", 400, "entity_blocked"),
        ("matrix_open", "missing_target", 404, "entity_not_found"),
    ):
        response = client.post(
            "/app/entities/api/merge",
            json={"source_slug": source, "target_slug": target, "commit": True},
        )
        assert response.status_code == status
        assert response.get_json()["reason_code"] == reason

    missing_history = client.get(
        "/app/entities/api/journal/entity/missing_entity/history"
    )
    missing_merge = client.post("/app/entities/api/merge/em_missing/undo", json={})
    missing_ambiguity = client.post(
        "/app/entities/api/ambiguities/amb_missing/resolve",
        json={"entity_id": "matrix_open"},
    )
    bad_status = client.get("/app/entities/api/ambiguities?status=stale")
    assert missing_history.status_code == 404
    assert missing_merge.status_code == 404
    assert missing_ambiguity.status_code == 404
    assert bad_status.status_code == 400

    _save("matrix_sarah_one", "Matrix Sarah One")
    _save("matrix_sarah_two", "Matrix Sarah Two")
    ambiguity = record_entity_resolution(
        "Matrix Sarah",
        [
            load_journal_entity("matrix_sarah_one"),
            load_journal_entity("matrix_sarah_two"),
        ],
        scope=ResolutionScope.journal(),
        origin=ResolutionOrigin(lane="test.matrix", field="entity"),
    )
    stale_choice = client.post(
        f"/app/entities/api/ambiguities/{ambiguity.ambiguity_id}/resolve",
        json={"entity_id": "missing_choice"},
    )
    assert stale_choice.status_code == 400
    assert stale_choice.get_json()["reason_code"] == "invalid_request_value"

    timeout = LockTimeout(Path("entity-trust"), 1.0)
    monkeypatch.setattr(
        entity_routes,
        "merge_entity",
        lambda *args, **kwargs: (_ for _ in ()).throw(timeout),
    )
    busy = client.post(
        "/app/entities/api/merge",
        json={
            "source_slug": "matrix_open",
            "target_slug": "matrix_sarah_one",
            "commit": True,
        },
    )
    assert busy.status_code == 503
    assert busy.get_json()["reason_code"] == "entity_busy"

    monkeypatch.setattr(
        entity_routes,
        "undo_entity_merge",
        lambda *args, **kwargs: (_ for _ in ()).throw(timeout),
    )
    busy_undo = client.post("/app/entities/api/merge/em_busy/undo", json={})
    assert busy_undo.status_code == 503
    assert busy_undo.get_json()["reason_code"] == "entity_busy"

    monkeypatch.setattr(
        entity_routes,
        "restore_journal_entity_version",
        lambda *args, **kwargs: (_ for _ in ()).throw(timeout),
    )
    busy_restore = client.post(
        "/app/entities/api/journal/entity/matrix_open/restore",
        json={"version_id": "vh_busy"},
    )
    assert busy_restore.status_code == 503
    assert busy_restore.get_json()["reason_code"] == "entity_busy"

    monkeypatch.setattr(
        entity_routes,
        "record_ambiguity_choice",
        lambda *args, **kwargs: (_ for _ in ()).throw(timeout),
    )
    busy_resolve = client.post(
        f"/app/entities/api/ambiguities/{ambiguity.ambiguity_id}/resolve",
        json={"entity_id": "matrix_sarah_one"},
    )
    assert busy_resolve.status_code == 503
    assert busy_resolve.get_json()["reason_code"] == "entity_busy"


def test_corrupt_history_is_explicit_non_2xx(speakers_env) -> None:
    env = speakers_env()
    client = create_app(str(env.journal)).test_client()
    _save("corrupt_history", "Corrupt History")
    event_path = next(
        (env.journal / "entities" / "corrupt_history" / "history" / "events").glob(
            "*.json"
        )
    )
    event_path.write_text("{", encoding="utf-8")

    response = client.get("/app/entities/api/journal/entity/corrupt_history/history")

    assert response.status_code == 500
    assert response.get_json()["reason_code"] == "entity_operation_failed"


def test_end_to_end_trust_core_flask_fixture(speakers_env) -> None:
    env = speakers_env()
    client = create_app(str(env.journal)).test_client()
    _save("e2e_source", "E2E Source", aka=["Source Alias"])
    _save("e2e_target", "E2E Target")
    env.create_facet_relationship(
        "work", "e2e_source", observations=["source reference"]
    )
    env.create_facet_relationship(
        "work", "e2e_target", observations=["target reference"]
    )

    preview = client.post(
        "/app/entities/api/merge",
        json={"source_slug": "e2e_source", "target_slug": "e2e_target"},
    )
    assert preview.status_code == 200
    assert preview.get_json()["merged"] is False

    committed = client.post(
        "/app/entities/api/merge",
        json={
            "source_slug": "e2e_source",
            "target_slug": "e2e_target",
            "commit": True,
        },
    ).get_json()
    merge_id = committed["merge_id"]
    assert load_journal_entity("e2e_source") is None
    history = client.get(
        "/app/entities/api/journal/entity/e2e_target/history"
    ).get_json()["items"]
    assert (
        next(row for row in history if row["kind"] == "merge")["merge_id"] == merge_id
    )

    undone = client.post(f"/app/entities/api/merge/{merge_id}/undo", json={})
    assert undone.status_code == 200
    assert load_journal_entity("e2e_source") is not None
    source_observations = (
        env.journal
        / "facets"
        / "work"
        / "entities"
        / "e2e_source"
        / "observations.jsonl"
    )
    assert "source reference" in source_observations.read_text(encoding="utf-8")

    baseline_edit = client.put(
        "/app/entities/api/journal/entity/e2e_target",
        json={"name": "E2E Target Baseline", "type": "Person", "aka": []},
    )
    assert baseline_edit.status_code == 200
    before_edit = list(iter_entity_history("e2e_target"))[-1]["version_id"]
    edited = client.put(
        "/app/entities/api/journal/entity/e2e_target",
        json={"name": "E2E Target Edited", "type": "Person", "aka": []},
    )
    assert edited.status_code == 200
    restored = client.post(
        "/app/entities/api/journal/entity/e2e_target/restore",
        json={"version_id": before_edit},
    )
    assert restored.status_code == 200
    assert restored.get_json()["event"]["kind"] == "restore"
    assert restored.get_json()["entity"]["name"] == "E2E Target Baseline"

    _save("e2e_sarah_one", "E2E Sarah One")
    _save("e2e_sarah_two", "E2E Sarah Two")
    resolution = record_entity_resolution(
        "E2E Sarah",
        [
            load_journal_entity("e2e_sarah_one"),
            load_journal_entity("e2e_sarah_two"),
        ],
        scope=ResolutionScope.journal(),
        origin=ResolutionOrigin(lane="test.e2e", field="entity"),
    )
    assert resolution.outcome == EntityResolutionOutcome.AMBIGUOUS
    resolved = client.post(
        f"/app/entities/api/ambiguities/{resolution.ambiguity_id}/resolve",
        json={"entity_id": "e2e_sarah_two"},
    )
    assert resolved.status_code == 200
    rerun = record_entity_resolution(
        "E2E Sarah",
        list(load_all_journal_entities().values()),
        scope=ResolutionScope.journal(),
        origin=ResolutionOrigin(lane="test.e2e.rerun", field="entity"),
    )
    assert rerun.outcome == EntityResolutionOutcome.RESOLVED
    assert rerun.entity["id"] == "e2e_sarah_two"
