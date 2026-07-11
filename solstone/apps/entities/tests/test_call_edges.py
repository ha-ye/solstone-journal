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


def _combined_output(result) -> str:
    try:
        stderr = result.stderr
    except ValueError:
        stderr = ""
    return result.output + (stderr or "")


def test_network_truncated_header_and_evidence(indexed_entities_client) -> None:
    result = runner.invoke(
        entities_app,
        ["network", "romeo_montague", "--limit", "1", "--evidence-limit", "1"],
    )

    assert result.exit_code == 0
    assert "5 recorded connections for romeo_montague (showing 1):" in result.output
    assert "Juliet Capulet (juliet_capulet)" in result.output
    assert "score=" in result.output
    assert "kinds=" in result.output
    assert "seen=" in result.output
    assert "20260310 attended-with - Joint Board Meeting" in result.output


def test_history_truncated_header_and_source_path(indexed_entities_client) -> None:
    result = runner.invoke(
        entities_app,
        ["history", "juliet_capulet", "romeo_montague", "--limit", "1"],
    )

    assert result.exit_code == 0
    assert (
        "8 evidence rows for juliet_capulet <-> Romeo Montague (romeo_montague) "
        "(showing 1-1):"
    ) in result.output
    assert "20260310 attended-with - Joint Board Meeting" in result.output
    assert "[event-legacy] facets/montague/events/20260310.jsonl" in result.output


def test_overview_truncated_header(indexed_entities_client) -> None:
    result = runner.invoke(entities_app, ["overview", "--limit", "1"])

    assert result.exit_code == 0
    assert "Network overview: 30 edges across 8 entities (showing 1):" in result.output
    assert "Kinds: attended-with:25,mentioned:2,spoke-with:3" in result.output
    assert "Romeo Montague (romeo_montague)" in result.output


def test_network_empty_state_exits_zero(indexed_entities_client) -> None:
    result = runner.invoke(entities_app, ["network", "alice_johnson"])

    assert result.exit_code == 0
    assert result.output == "No recorded connections for alice_johnson.\n"


def test_resolution_failure_names_query_with_candidates(
    indexed_entities_client,
) -> None:
    result = runner.invoke(entities_app, ["network", "Jliet"])

    assert result.exit_code == 1
    output = _combined_output(result)
    assert "Error: Entity 'Jliet' not found. Did you mean:" in output
    assert "Juliet Capulet (juliet_capulet)" in output


def test_resolution_failure_names_query_without_candidates(
    indexed_entities_client,
) -> None:
    result = runner.invoke(entities_app, ["network", "zzzznotreal"])

    assert result.exit_code == 1
    assert "Error: Entity 'zzzznotreal' not found." in _combined_output(result)
    assert "Did you mean" not in _combined_output(result)


def test_history_json_is_route_payload(indexed_entities_client) -> None:
    result = runner.invoke(
        entities_app,
        ["history", "juliet_capulet", "romeo_montague", "--limit", "1", "--json"],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "success" not in data
    assert data["entity_id"] == "juliet_capulet"
    assert data["peer_id"] == "romeo_montague"
    assert data["limit"] == 1
    assert data["total"] == 8


def test_kinds_bad_kind_detail_survives_real_client(
    indexed_entities_client,
) -> None:
    result = runner.invoke(
        entities_app,
        ["network", "romeo_montague", "--kinds", "bad-kind"],
    )

    assert result.exit_code == 1
    assert "Error: Unknown edge kind: 'bad-kind'" in _combined_output(result)


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
    output = _combined_output(result)
    assert (
        "I couldn't read your connections because the index hasn't been built yet."
        in output
    )
    assert "journal indexer --rescan" in output
