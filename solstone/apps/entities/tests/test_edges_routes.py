# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""HTTP route tests for derived entity edge reads."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from solstone.think.indexer.journal import DB_NAME, INDEX_DIR, scan_journal
from tests._baseline_harness import make_test_client


@pytest.fixture
def indexed_client(journal_copy: Path):
    scan_journal(str(journal_copy), full=True)
    return make_test_client(journal_copy)


def _index_db(journal: Path) -> Path:
    return journal / INDEX_DIR / DB_NAME


def _create_empty_edges_db(journal: Path) -> None:
    db_path = _index_db(journal)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE edges(
                src TEXT NOT NULL,
                dst TEXT NOT NULL,
                kind TEXT NOT NULL,
                directed INTEGER NOT NULL,
                src_name TEXT,
                dst_name TEXT,
                day TEXT,
                facet TEXT,
                source TEXT NOT NULL,
                path TEXT NOT NULL,
                anchor TEXT,
                label TEXT,
                ts INTEGER,
                weight INTEGER NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_network_route_returns_loader_payload_for_facetless_name(indexed_client):
    response = indexed_client.get(
        "/app/entities/api/network",
        query_string={
            "entity": "Juliet Capulet",
            "limit": "1",
            "evidence_limit": "1",
            "include_principal": "true",
        },
    )

    data = response.get_json()
    assert response.status_code == 200
    assert "success" not in data
    assert data["entity_id"] == "juliet_capulet"
    assert data["limit"] == 1
    assert data["evidence_limit"] == 1
    assert data["total_neighbors"] >= 1
    assert len(data["neighbors"]) == 1
    assert data["neighbors"][0]["evidence_class"] in {
        "attendance",
        "semantic",
        "mixed",
    }
    assert data["neighbors"][0]["evidence"]


def test_overview_route_returns_evidence_class_per_entity(indexed_client):
    response = indexed_client.get(
        "/app/entities/api/overview",
        query_string={"limit": "2"},
    )

    data = response.get_json()
    assert response.status_code == 200
    assert data["entities"]
    assert all("type" in entity for entity in data["entities"])
    assert all(
        entity["evidence_class"] in {"attendance", "semantic", "mixed"}
        for entity in data["entities"]
    )


def test_network_route_facet_resolution_returns_journal_entity_id(indexed_client):
    response = indexed_client.get(
        "/app/entities/api/network",
        query_string={
            "entity": "Juliet",
            "facet": "montague",
            "include_principal": "true",
        },
    )

    data = response.get_json()
    assert response.status_code == 200
    assert data["entity_id"] == "juliet_capulet"


def test_history_route_defaults_peer_to_principal(indexed_client):
    response = indexed_client.get(
        "/app/entities/api/history",
        query_string={"entity": "Juliet Capulet", "limit": "1"},
    )

    data = response.get_json()
    assert response.status_code == 200
    assert data["entity_id"] == "juliet_capulet"
    assert data["peer_id"] == "romeo_montague"
    assert data["total"] >= 1
    assert len(data["evidence"]) == 1


def test_resolution_failure_returns_200_query_and_candidates(indexed_client):
    response = indexed_client.get(
        "/app/entities/api/network",
        query_string={"entity": "Jliet"},
    )

    data = response.get_json()
    assert response.status_code == 200
    assert data["resolved"] is None
    assert data["query"] == "Jliet"
    assert data["candidates"]
    assert {"name", "id", "type"} <= set(data["candidates"][0])


def test_resolution_failure_empty_candidates_on_full_miss(indexed_client):
    response = indexed_client.get(
        "/app/entities/api/network",
        query_string={"entity": "zzzznotreal"},
    )

    data = response.get_json()
    assert response.status_code == 200
    assert data == {"resolved": None, "query": "zzzznotreal", "candidates": []}


def test_unknown_kind_maps_to_invalid_request_value(indexed_client):
    response = indexed_client.get(
        "/app/entities/api/overview",
        query_string={"kinds": "bad-kind"},
    )

    data = response.get_json()
    assert response.status_code == 400
    assert data["reason_code"] == "invalid_request_value"
    assert data["detail"] == "Unknown edge kind: 'bad-kind'"


def test_missing_index_maps_to_edge_index_unavailable(journal_copy: Path):
    db_path = _index_db(journal_copy)
    if db_path.exists():
        db_path.unlink()
    client = make_test_client(journal_copy)

    response = client.get("/app/entities/api/overview")

    data = response.get_json()
    assert response.status_code == 503
    assert data["reason_code"] == "edge_index_unavailable"
    assert "journal indexer --rebuild-edges" in data["error"]


def test_malformed_edges_schema_maps_to_edge_index_unavailable(journal_copy: Path):
    db_path = _index_db(journal_copy)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE edges(src TEXT)")
        conn.commit()
    finally:
        conn.close()
    client = make_test_client(journal_copy)

    response = client.get("/app/entities/api/overview")

    data = response.get_json()
    assert response.status_code == 503
    assert data["reason_code"] == "edge_index_unavailable"
    assert "journal indexer --rebuild-edges" in data["error"]


def test_zero_edge_entity_matches_empty_edges_table(
    indexed_client,
    journal_copy: Path,
):
    populated_response = indexed_client.get(
        "/app/entities/api/network",
        query_string={"entity": "alice_johnson"},
    )
    populated_payload = populated_response.get_json()

    _index_db(journal_copy).unlink()
    _create_empty_edges_db(journal_copy)
    empty_client = make_test_client(journal_copy)
    empty_response = empty_client.get(
        "/app/entities/api/network",
        query_string={"entity": "alice_johnson"},
    )

    assert populated_response.status_code == 200
    assert empty_response.status_code == 200
    assert populated_payload == empty_response.get_json()
    assert populated_payload["neighbors"] == []
    assert populated_payload["total_neighbors"] == 0


def test_history_without_peer_or_principal_is_invalid_request(journal_copy: Path):
    for entity_path in (journal_copy / "entities").glob("*/entity.json"):
        data = json.loads(entity_path.read_text(encoding="utf-8"))
        data.pop("is_principal", None)
        entity_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    client = make_test_client(journal_copy)

    response = client.get(
        "/app/entities/api/history",
        query_string={"entity": "Juliet Capulet"},
    )

    data = response.get_json()
    assert response.status_code == 400
    assert data["reason_code"] == "invalid_request_value"
    assert (
        data["detail"]
        == "history requires PEER because no principal entity is configured"
    )
