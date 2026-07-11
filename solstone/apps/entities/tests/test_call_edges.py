# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI tests for derived entity edge reads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from solstone.apps.entities.call import app as entities_app
from solstone.think.convey_client import ConveyClient
from solstone.think.indexer.journal import DB_NAME, INDEX_DIR, scan_journal
from tests._baseline_harness import make_test_client

runner = CliRunner()


@pytest.fixture
def indexed_entities_client(
    journal_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    scan_journal(str(journal_copy), full=True)

    def client() -> ConveyClient:
        return ConveyClient(
            session=make_test_client(journal_copy),
            base_url="",
        )

    monkeypatch.setattr("solstone.apps.entities.call.get_client", client)
    return journal_copy


@pytest.fixture
def entities_client_without_index(
    journal_copy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    db_path = journal_copy / INDEX_DIR / DB_NAME
    if db_path.exists():
        db_path.unlink()

    def client() -> ConveyClient:
        return ConveyClient(
            session=make_test_client(journal_copy),
            base_url="",
        )

    monkeypatch.setattr("solstone.apps.entities.call.get_client", client)
    return journal_copy


def _captured_output(result) -> str:
    return result.output


def _route_payload(journal: Path, path: str, query_string: dict[str, object]) -> dict:
    response = make_test_client(journal).get(path, query_string=query_string)
    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, dict)
    return data


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else plural or f"{singular}s"
    return f"{count} {word}"


def _display_entity(entity_id: object, name: object = None) -> str:
    entity_id_text = str(entity_id or "")
    name_text = str(name or "")
    if name_text and name_text != entity_id_text:
        return f"{name_text} ({entity_id_text})"
    return entity_id_text


def _format_kinds(kinds: object) -> str:
    if not isinstance(kinds, dict):
        return ""
    parts = []
    for kind in sorted(kinds):
        info = kinds.get(kind)
        count = info.get("count") if isinstance(info, dict) else None
        if count:
            parts.append(f"{kind}:{count}")
    return ", ".join(parts)


def test_network_truncated_header_and_evidence(indexed_entities_client: Path) -> None:
    payload = _route_payload(
        indexed_entities_client,
        "/app/entities/api/network",
        {
            "entity": "romeo_montague",
            "limit": "1",
            "evidence_limit": "1",
        },
    )
    result = runner.invoke(
        entities_app,
        ["network", "romeo_montague", "--limit", "1", "--evidence-limit", "1"],
    )

    assert result.exit_code == 0
    total = int(payload["total_neighbors"])
    shown = len(payload["neighbors"])
    assert shown < total
    expected_header = (
        f"{_plural(total, 'recorded connection')} for romeo_montague (showing {shown}):"
    )
    assert expected_header in result.output
    neighbor = payload["neighbors"][0]
    evidence = neighbor["evidence"][0]
    assert _display_entity(neighbor["entity_id"], neighbor.get("name")) in result.output
    assert "score=" in result.output
    assert "kinds=" in result.output
    assert "seen=" in result.output
    assert str(evidence["day"]) in result.output
    assert str(evidence["kind"]) in result.output
    assert str(evidence["label"]) in result.output


def test_history_truncated_header_and_source_path(
    indexed_entities_client: Path,
) -> None:
    payload = _route_payload(
        indexed_entities_client,
        "/app/entities/api/history",
        {
            "entity": "juliet_capulet",
            "peer": "romeo_montague",
            "limit": "1",
        },
    )
    result = runner.invoke(
        entities_app,
        ["history", "juliet_capulet", "romeo_montague", "--limit", "1"],
    )

    assert result.exit_code == 0
    total = int(payload["total"])
    shown = len(payload["evidence"])
    assert shown < total
    peer = _display_entity(payload["peer_id"], payload.get("peer_name"))
    expected_header = (
        f"{_plural(total, 'evidence row')} for juliet_capulet <-> {peer} "
        f"(showing 1-{shown}):"
    )
    assert expected_header in result.output
    evidence = payload["evidence"][0]
    assert str(evidence["day"]) in result.output
    assert str(evidence["kind"]) in result.output
    assert str(evidence["label"]) in result.output
    assert f"[{evidence['source']}]" in result.output
    assert str(evidence["path"]) in result.output


def test_overview_truncated_header(indexed_entities_client: Path) -> None:
    payload = _route_payload(
        indexed_entities_client,
        "/app/entities/api/overview",
        {"limit": "1"},
    )
    result = runner.invoke(entities_app, ["overview", "--limit", "1"])

    assert result.exit_code == 0
    totals = payload["totals"]
    shown = len(payload["entities"])
    assert shown < int(totals["entities"])
    expected_header = (
        f"Network overview: {_plural(int(totals['edges']), 'edge')} across "
        f"{_plural(int(totals['entities']), 'entity', 'entities')} "
        f"(showing {shown}):"
    )
    assert expected_header in result.output
    assert f"Kinds: {_format_kinds(payload['kinds'])}" in result.output
    entity = payload["entities"][0]
    assert _display_entity(entity["entity_id"], entity.get("name")) in result.output


def test_network_empty_state_exits_zero(indexed_entities_client) -> None:
    result = runner.invoke(entities_app, ["network", "alice_johnson"])

    assert result.exit_code == 0
    assert result.output == "No recorded connections for alice_johnson.\n"


def test_resolution_failure_names_query_with_candidates(
    indexed_entities_client,
) -> None:
    result = runner.invoke(entities_app, ["network", "Jliet"])

    assert result.exit_code == 1
    output = _captured_output(result)
    assert "Error: Entity 'Jliet' not found. Did you mean:" in output
    assert "Juliet Capulet (juliet_capulet)" in output


def test_resolution_failure_names_query_without_candidates(
    indexed_entities_client,
) -> None:
    result = runner.invoke(entities_app, ["network", "zzzznotreal"])

    assert result.exit_code == 1
    assert "Error: Entity 'zzzznotreal' not found." in _captured_output(result)
    assert "Did you mean" not in _captured_output(result)


def test_resolution_failure_json_is_route_payload(indexed_entities_client) -> None:
    result = runner.invoke(entities_app, ["network", "Jliet", "--json"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["resolved"] is None
    assert data["query"] == "Jliet"
    assert data["candidates"]
    assert "Error:" not in result.output


def test_history_json_is_route_payload(indexed_entities_client: Path) -> None:
    expected = _route_payload(
        indexed_entities_client,
        "/app/entities/api/history",
        {
            "entity": "juliet_capulet",
            "peer": "romeo_montague",
            "limit": "1",
        },
    )
    result = runner.invoke(
        entities_app,
        ["history", "juliet_capulet", "romeo_montague", "--limit", "1", "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "success" not in data
    assert data == expected


def test_kinds_bad_kind_detail_survives_real_client(
    indexed_entities_client,
) -> None:
    result = runner.invoke(
        entities_app,
        ["network", "romeo_montague", "--kinds", "bad-kind"],
    )

    assert result.exit_code == 1
    assert "Error: Unknown edge kind: 'bad-kind'" in _captured_output(result)


def test_kinds_accepts_comma_and_repeat_forms(indexed_entities_client) -> None:
    result = runner.invoke(
        entities_app,
        [
            "overview",
            "--kinds",
            "attended-with,spoke-with",
            "--kinds",
            "mentioned",
            "--json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["filters"]["kinds"] == [
        "attended-with",
        "spoke-with",
        "mentioned",
    ]


def test_unbuilt_index_message_survives_real_client(
    entities_client_without_index,
) -> None:
    result = runner.invoke(entities_app, ["overview"])

    assert result.exit_code == 1
    output = _captured_output(result)
    assert (
        "I couldn't read your connections because the index hasn't been built yet."
        in output
    )
    assert "journal indexer --rescan" in output
