# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest

from solstone.apps.entities import copy as entity_copy
from solstone.apps.home.connections import _kind_words, build_connections_card
from solstone.think.indexer import edges as edge_index
from solstone.think.indexer.edges import insert_edges
from solstone.think.indexer.journal import get_journal_index

EDGE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "edges_journal"
CONTRACT_KIND_WORDS = {
    "works-with": "works with",
    "works-at": "works at",
    "reports-to": "reports to",
    "family-of": "family",
    "knows": "knows",
    "uses": "uses",
    "created": "created",
    "decided-with": "decided together",
    "committed-to": "commitments",
    "spoke-with": "spoke",
    "mentioned": "mentions",
    "messaged-with": "messaged",
    "scheduled-with": "scheduled",
    "party-of": "party to",
    "other": "related",
}


@pytest.fixture
def edges_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    journal = tmp_path / "edges_journal"
    shutil.copytree(EDGE_FIXTURE, journal)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal.resolve()))
    return journal


def _insert(journal: Path, rows: list[dict[str, Any]]) -> None:
    conn, _ = get_journal_index(str(journal))
    insert_edges(conn, rows)
    conn.commit()
    conn.close()


def _row(
    src: str,
    dst: str,
    kind: str,
    path: str,
    *,
    day: str = "20260701",
    weight: int = 1,
    label: str | None = None,
    ts: int | None = 0,
    src_name: str | None = "Alice Edge",
    dst_name: str | None = None,
) -> dict[str, Any]:
    return {
        "src": src,
        "dst": dst,
        "kind": kind,
        "src_name": src_name,
        "dst_name": dst_name,
        "day": day,
        "facet": "home-test",
        "source": "home-test",
        "path": path,
        "anchor": None,
        "label": label,
        "ts": ts,
        "weight": weight,
    }


def _write_entity(journal: Path, entity_id: str, *, principal: bool = False) -> None:
    entity_dir = journal / "entities" / entity_id
    entity_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "id": entity_id,
        "name": entity_id.replace("_", " ").title(),
        "type": "Person",
        "created_at": 0,
    }
    if principal:
        payload["is_principal"] = True
    (entity_dir / "entity.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def _minimal_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, principal: bool
) -> Path:
    journal = tmp_path / "journal"
    (journal / "entities").mkdir(parents=True)
    _write_entity(journal, "home_self", principal=principal)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal.resolve()))
    return journal


def test_connections_card_populated_trims_loader_payload(edges_journal: Path):
    _insert(
        edges_journal,
        [
            _row(
                "edge_alice",
                "home_juliet",
                "works-with",
                "home/juliet-work.jsonl",
                dst_name="Home Juliet",
                label="Earlier work",
                ts=1,
            ),
            _row(
                "edge_alice",
                "home_juliet",
                "family-of",
                "home/juliet-family.jsonl",
                dst_name="Home Juliet",
                label="Family tie",
                ts=2,
            ),
            _row(
                "edge_alice",
                "home_juliet",
                "mentioned",
                "home/juliet-mentioned.jsonl",
                weight=2,
                dst_name="Home Juliet",
                label="Latest mention",
                ts=3,
            ),
            _row(
                "edge_alice",
                "home_friar",
                "attended-with",
                "home/friar-event.jsonl",
                dst_name="Home Friar",
                label="Shared event",
                ts=4,
            ),
        ],
    )

    payload = build_connections_card()

    assert payload["state"] == "ok"
    assert payload["total"] == 2
    assert payload["attendance_kinds"] == sorted(edge_index.ATTENDANCE_KINDS)
    assert payload["kind_words"] == CONTRACT_KIND_WORDS
    assert [neighbor["entity_id"] for neighbor in payload["neighbors"]] == [
        "home_juliet",
        "home_friar",
    ]
    juliet = payload["neighbors"][0]
    assert set(juliet) == {
        "entity_id",
        "name",
        "evidence_class",
        "count",
        "last_seen",
        "kinds",
        "latest_label",
        "latest_kind",
        "latest_day",
    }
    assert juliet["name"] == "Home Juliet"
    assert juliet["evidence_class"] == "semantic"
    assert juliet["count"] == 3
    assert juliet["last_seen"] == "20260701"
    assert [kind["kind"] for kind in juliet["kinds"]] == [
        "mentioned",
        "family-of",
        "works-with",
    ]
    assert juliet["latest_label"] == "Latest mention"
    assert juliet["latest_kind"] == "mentioned"
    assert juliet["latest_day"] == "20260701"
    assert "score" not in juliet
    assert "directed" not in juliet
    assert "evidence" not in juliet


def test_connections_card_no_principal_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _minimal_journal(tmp_path, monkeypatch, principal=False)

    assert build_connections_card() == {"state": "empty"}


def test_connections_card_zero_neighbors_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    journal = _minimal_journal(tmp_path, monkeypatch, principal=True)
    conn, _ = get_journal_index(str(journal))
    conn.commit()
    conn.close()

    assert build_connections_card() == {"state": "empty"}


def test_connections_card_loader_error_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    import solstone.apps.home.connections as connections

    _minimal_journal(tmp_path, monkeypatch, principal=True)
    monkeypatch.setattr(
        connections,
        "load_entity_network",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    caplog.set_level(logging.WARNING, logger="solstone.apps.home.connections")

    assert build_connections_card() == {"state": "unavailable"}
    assert "home: failed to build connections card" in caplog.text


def test_build_pulse_context_survives_missing_connections_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import solstone.apps.home.routes as home_routes

    _minimal_journal(tmp_path, monkeypatch, principal=True)
    monkeypatch.setattr(
        home_routes,
        "get_capture_health",
        lambda: {"status": "active", "observers": []},
    )
    monkeypatch.setattr(home_routes, "get_cached_state", lambda: {})
    monkeypatch.setattr(home_routes, "get_current", lambda: None)
    monkeypatch.setattr(home_routes, "_resolve_attention", lambda awareness: None)
    monkeypatch.setattr(home_routes, "_today", lambda: "20260713")
    monkeypatch.setattr(home_routes, "_yesterday", lambda: "20260712")
    monkeypatch.setattr(home_routes, "_count_journal_age_days", lambda today: 8)
    monkeypatch.setattr(home_routes, "_load_stats", lambda today: {})
    monkeypatch.setattr(home_routes, "_load_flow_md", lambda today: ("pulse", None))
    monkeypatch.setattr(
        home_routes,
        "_load_pulse_narrative",
        lambda today: ("pulse", None, []),
    )
    monkeypatch.setattr(
        home_routes, "_collect_anticipated_activities", lambda today: []
    )
    monkeypatch.setattr(home_routes, "_collect_activities", lambda today: [])
    monkeypatch.setattr(home_routes, "_load_latest_weekly_reflection", lambda: None)
    monkeypatch.setattr(home_routes, "load_briefing", lambda today: None)
    monkeypatch.setattr(home_routes, "read_steward_health", lambda: None)
    monkeypatch.setattr(
        home_routes, "read_steward_summary", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        home_routes,
        "build_brain_snapshot",
        lambda *_a, **_k: {"state": "ready"},
    )
    monkeypatch.setattr(
        home_routes,
        "_summarize_yesterday_processing",
        lambda yesterday, journal_age_days: None,
    )

    ctx = home_routes._build_pulse_context()

    assert ctx["connections"] == {"state": "unavailable"}
    assert ctx["narrative_content"] == "pulse"


def test_connections_kind_vocabulary_matches_entities_copy_byte_for_byte():
    entities_contract = {
        **entity_copy.ENT_CONN_KIND_WORDS,
        **entity_copy.ENT_CONN_KIND_CHIP_WORDS,
    }

    assert _kind_words() == CONTRACT_KIND_WORDS
    assert {
        key: entities_contract[key] for key in CONTRACT_KIND_WORDS
    } == CONTRACT_KIND_WORDS
    assert sorted(set(entities_contract) - set(CONTRACT_KIND_WORDS)) == [
        "attended-with",
        "co-present",
    ]
    assert sorted(set(CONTRACT_KIND_WORDS) - set(entities_contract)) == []
