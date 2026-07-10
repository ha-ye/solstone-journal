# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the derived edge index layer."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from solstone.think import edge_sources
from solstone.think.activities import (
    _relation_label,
    extract_activity_edges,
)
from solstone.think.edge_sources import EdgeContext, get_edge_source
from solstone.think.formatters import discover_files
from solstone.think.indexer.edges import (
    EDGES_SCHEMA_PATH,
    _extract_file_edges,
    discover_edge_files,
    insert_edges,
    rebuild_edges,
)
from solstone.think.indexer.journal import get_journal_index, index_file, scan_journal
from tests._sqlite_assertions import edges_content_hash, table_content_hash

EDGE_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "edges_journal"
EDGE_COLUMNS = [
    "src",
    "dst",
    "kind",
    "directed",
    "src_name",
    "dst_name",
    "day",
    "facet",
    "source",
    "path",
    "anchor",
    "label",
    "ts",
    "weight",
]
CHUNK_COLUMNS = [
    "content",
    "path",
    "day",
    "facet",
    "agent",
    "stream",
    "idx",
    "time_bucket",
]
FILE_COLUMNS = ["path", "mtime"]


@pytest.fixture
def edges_journal(tmp_path, monkeypatch):
    journal = tmp_path / "edges_journal"
    shutil.copytree(EDGE_FIXTURE, journal)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal.resolve()))
    return journal


def synthetic_edge_extractor(entries: list[dict], ctx: EdgeContext) -> list[dict]:
    return [
        {
            "src": "edge_ada",
            "dst": "edge_byron",
            "kind": "attended-with",
            "src_name": None,
            "dst_name": None,
            "day": ctx.day,
            "facet": ctx.facet,
            "source": "participation",
            "path": ctx.path,
            "anchor": "synthetic",
            "label": "Synthetic edge-only file",
            "ts": 0,
            "weight": 1,
        }
    ]


def bad_kind_edge_extractor(entries: list[dict], ctx: EdgeContext) -> list[dict]:
    return [
        {
            "src": "edge_ada",
            "dst": "edge_byron",
            "kind": "bad-kind",
            "source": "participation",
            "path": ctx.path,
            "weight": 1,
        }
    ]


def day_rooted_resolving_edge_extractor(
    entries: list[dict],
    ctx: EdgeContext,
) -> list[dict]:
    assert entries == [{"ok": True}]
    src = ctx.resolve("Ada Edge")
    assert src == "edge_ada"
    ctx.drop()
    return [
        {
            "src": src,
            "dst": "edge_byron",
            "kind": "attended-with",
            "source": "participation",
            "path": ctx.path,
            "anchor": "day-rooted",
            "label": "",
            "day": ctx.day,
            "facet": ctx.facet,
            "ts": 0,
            "weight": 1,
        }
    ]


def _conn(journal: Path) -> sqlite3.Connection:
    conn, _ = get_journal_index(str(journal))
    conn.row_factory = sqlite3.Row
    return conn


def _edge_rows(conn: sqlite3.Connection, where: str = "") -> list[dict[str, Any]]:
    sql = f"SELECT {', '.join(EDGE_COLUMNS)} FROM edges"
    if where:
        sql += f" WHERE {where}"
    sql += " ORDER BY source, src, dst, anchor"
    return [dict(row) for row in conn.execute(sql)]


def _source_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        source: count
        for source, count in conn.execute(
            "SELECT source, count(*) FROM edges GROUP BY source"
        )
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _bump_mtime(path: Path) -> None:
    bumped = path.stat().st_mtime + 2
    os.utime(path, (bumped, bumped))


def _edge_ctx(rel: str, *, facet: str = "edges-story") -> EdgeContext:
    return EdgeContext(
        path=rel,
        day="20260430",
        facet=facet,
        resolve=lambda _name: None,
        drop=lambda: None,
    )


def test_relation_label_formats_documented_forms():
    assert _relation_label("Works together", None) == "Works together"
    assert _relation_label("", "quoted text") == '"quoted text"'
    assert (
        _relation_label("Works together", "quoted text")
        == 'Works together — "quoted text"'
    )
    assert _relation_label("  ", None) == ""


def test_activity_story_generalization_preserves_commitment_and_closure_rows_byte_exact(
    edges_journal,
):
    rel = "facets/edges-story/activities/20260430.jsonl"
    rows = extract_activity_edges(_read_jsonl(edges_journal / rel), _edge_ctx(rel))

    story_rows = [row for row in rows if row["source"] in {"commitment", "closure"}]
    assert story_rows == [
        {
            "src": "edge_mina",
            "dst": "edge_ravi",
            "kind": "committed-to",
            "src_name": None,
            "dst_name": None,
            "day": "20260430",
            "facet": "edges-story",
            "source": "commitment",
            "path": rel,
            "anchor": "story-commitments-1",
            "label": "Send the proposal",
            "ts": 1777554000000,
            "weight": 1,
        },
        {
            "src": "edge_tessa",
            "dst": "edge_mina",
            "kind": "committed-to",
            "src_name": None,
            "dst_name": None,
            "day": "20260430",
            "facet": "edges-story",
            "source": "closure",
            "path": rel,
            "anchor": "story-commitments-1",
            "label": "Confirm the handoff",
            "ts": 1777554000000,
            "weight": 1,
        },
    ]


def test_activity_relations_and_decisions_emit_expected_rows(edges_journal):
    rel = "facets/edges-story/activities/20260430.jsonl"
    record = {
        "id": "story-relations-1",
        "title": "Relation session",
        "created_at": 1777555000000,
        "relations": [
            {
                "from": "Mina Edge",
                "to": "Ravi Edge",
                "from_entity_id": "edge_mina",
                "to_entity_id": "edge_ravi",
                "kind": "works-with",
                "note": "Runs planning together",
                "quote": "Let's pair on this",
            },
            {
                "from": "Tessa Edge",
                "to": "Tessa Edge",
                "from_entity_id": "edge_tessa",
                "to_entity_id": "edge_tessa",
                "kind": "knows",
                "note": "self",
                "quote": None,
            },
        ],
        "decisions": [
            {
                "owner": "Mina Edge",
                "counterparty": "Ravi Edge",
                "owner_entity_id": "edge_mina",
                "counterparty_entity_id": "edge_ravi",
                "action": "Use the stable plan",
            },
            {
                "owner": "Mina Edge",
                "counterparty": "Mina Edge",
                "owner_entity_id": "edge_mina",
                "counterparty_entity_id": "edge_mina",
                "action": "Skip self",
            },
        ],
    }

    rows = extract_activity_edges([record], _edge_ctx(rel))

    assert rows == [
        {
            "src": "edge_mina",
            "dst": "edge_ravi",
            "kind": "decided-with",
            "src_name": None,
            "dst_name": None,
            "day": "20260430",
            "facet": "edges-story",
            "source": "decision",
            "path": rel,
            "anchor": "story-relations-1",
            "label": "Use the stable plan",
            "ts": 1777555000000,
            "weight": 1,
        },
        {
            "src": "edge_mina",
            "dst": "edge_ravi",
            "kind": "works-with",
            "src_name": "Mina Edge",
            "dst_name": "Ravi Edge",
            "day": "20260430",
            "facet": "edges-story",
            "source": "relation",
            "path": rel,
            "anchor": "story-relations-1",
            "label": 'Runs planning together — "Let\'s pair on this"',
            "ts": 1777555000000,
            "weight": 1,
        },
    ]


def test_activity_unknown_relation_kind_raises_at_insert(edges_journal):
    rel = "facets/edges-story/activities/20260430.jsonl"
    rows = extract_activity_edges(
        [
            {
                "id": "story-bad-relation-kind",
                "created_at": 1777555000000,
                "relations": [
                    {
                        "from": "Mina Edge",
                        "to": "Ravi Edge",
                        "from_entity_id": "edge_mina",
                        "to_entity_id": "edge_ravi",
                        "kind": "unknown-relation-kind",
                        "note": "Bad kind",
                        "quote": None,
                    }
                ],
            }
        ],
        _edge_ctx(rel),
    )

    conn = _conn(edges_journal)
    with pytest.raises(ValueError, match="Unknown edge kind"):
        insert_edges(conn, rows)
    conn.close()


def test_day_rooted_context_resolves_journal_entities_and_drop_hook_counts(
    edges_journal,
    monkeypatch,
):
    monkeypatch.setitem(
        edge_sources.EDGE_SOURCES,
        "*/*/*/synthetic.jsonl",
        ("tests.test_index_edges", "day_rooted_resolving_edge_extractor"),
    )
    rel = "20260430/default/090000_300/synthetic.jsonl"
    path = edges_journal / "chronicle" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_jsonl(path, [{"ok": True}])

    conn = _conn(edges_journal)
    result = _extract_file_edges(conn, rel, str(path), {})

    assert result.rows_inserted == 1
    assert result.drops == 1
    assert not result.failed
    assert (
        conn.execute("SELECT src FROM edges WHERE path=?", (rel,)).fetchone()[0]
        == "edge_ada"
    )
    conn.close()


def test_discover_edge_files_keeps_structural_sources_with_chronicle_root(
    edges_journal,
):
    files = discover_edge_files(str(edges_journal))

    assert "facets/edges-activity/activities/20260430.jsonl" in files
    assert "facets/edges-story/activities/20260430.jsonl" in files
    assert "facets/edges-copresence/entities/20260430.jsonl" in files
    assert "facets/edges-events/events/20260430.jsonl" in files
    assert "20260430/default/090000_300/screen.jsonl" in files
    assert "20260430/default/090000_300/talents/documents.json" in files


def test_scan_indexes_edges_and_second_scan_is_zero_delta(edges_journal):
    assert scan_journal(str(edges_journal), full=True) is True

    conn = _conn(edges_journal)
    schema_version = conn.execute(
        "SELECT mtime FROM edge_files WHERE path=?", (EDGES_SCHEMA_PATH,)
    ).fetchone()[0]
    assert schema_version == 1
    assert conn.execute("SELECT count(*) FROM edges").fetchone()[0] == 24
    assert _source_counts(conn) == {
        "calendar": 3,
        "co-presence": 2,
        "closure": 1,
        "commitment": 1,
        "decision": 1,
        "document": 3,
        "event-legacy": 3,
        "messaging": 4,
        "observation": 1,
        "participation": 3,
        "relation": 2,
    }

    participation = _edge_rows(conn, "source='participation'")
    assert len(participation) == 3
    assert {row["anchor"] for row in participation} == {"activity-attendees-1"}
    assert all(
        row["src_name"] is None and row["dst_name"] is None for row in participation
    )

    commitment = _edge_rows(conn, "source='commitment'")
    assert commitment == [
        {
            "src": "edge_mina",
            "dst": "edge_ravi",
            "kind": "committed-to",
            "directed": 1,
            "src_name": None,
            "dst_name": None,
            "day": "20260430",
            "facet": "edges-story",
            "source": "commitment",
            "path": "facets/edges-story/activities/20260430.jsonl",
            "anchor": "story-commitments-1",
            "label": "Send the proposal",
            "ts": 1777554000000,
            "weight": 1,
        }
    ]
    closure = _edge_rows(conn, "source='closure'")
    assert closure[0]["src"] == "edge_tessa"
    assert closure[0]["dst"] == "edge_mina"
    assert closure[0]["directed"] == 1
    assert closure[0]["src_name"] is None
    assert closure[0]["dst_name"] is None

    relation = {row["kind"]: row for row in _edge_rows(conn, "source='relation'")}
    assert set(relation) == {"reports-to", "works-with"}
    assert relation["works-with"]["label"] == (
        'Runs planning together — "Let\'s pair on this"'
    )
    assert relation["works-with"]["directed"] == 0
    assert relation["reports-to"]["directed"] == 1
    decision = _edge_rows(conn, "source='decision'")
    assert len(decision) == 1
    assert decision[0]["kind"] == "decided-with"
    assert decision[0]["label"] == "Use the stable plan together"

    copresence = {
        (row["src"], row["dst"]): row
        for row in _edge_rows(conn, "source='co-presence'")
    }
    assert copresence[("edge_alice", "edge_bob")]["weight"] == 3
    assert (
        copresence[("edge_alice", "edge_bob")]["anchor"]
        == "20260430/default/090000_300"
    )
    assert copresence[("edge_alice", "edge_bob")]["src_name"] == "Alice Edge"
    assert copresence[("edge_alice", "edge_bob")]["dst_name"] == "Bob Edge"
    assert copresence[("edge_alice", "edge_cora")]["weight"] == 1
    assert copresence[("edge_alice", "edge_cora")]["src_name"] == "Alice Edge"

    events = _edge_rows(conn, "source='event-legacy'")
    assert len(events) == 3
    assert all(row["src_name"] and row["dst_name"] for row in events)
    assert {row["label"] for row in events} == {"Edge Legacy Meetup"}
    assert all(row["ts"] > 0 for row in events)

    assert conn.execute(
        "SELECT 1 FROM edges WHERE src='edge_alice' OR dst='edge_alice' LIMIT 1"
    ).fetchone()

    first_hash = edges_content_hash(conn)
    conn.close()

    assert scan_journal(str(edges_journal), full=True) is False
    conn = _conn(edges_journal)
    assert edges_content_hash(conn) == first_hash
    conn.close()


def test_touching_one_edge_file_replaces_only_that_path(edges_journal):
    scan_journal(str(edges_journal), full=True)
    conn = _conn(edges_journal)
    before = _source_counts(conn)
    conn.close()

    path = edges_journal / "facets" / "edges-activity" / "activities" / "20260430.jsonl"
    records = _read_jsonl(path)
    records[0]["participation"][4]["role"] = "attendee"
    _write_jsonl(path, records)
    _bump_mtime(path)

    assert scan_journal(str(edges_journal), full=True) is True
    conn = _conn(edges_journal)
    after = _source_counts(conn)
    assert after["participation"] == 6
    assert {k: v for k, v in after.items() if k != "participation"} == {
        k: v for k, v in before.items() if k != "participation"
    }
    assert conn.execute(
        "SELECT 1 FROM edge_files WHERE path=?",
        ("facets/edges-activity/activities/20260430.jsonl",),
    ).fetchone()
    conn.close()


def test_deleted_edge_source_removes_rows_and_ledger(edges_journal):
    scan_journal(str(edges_journal), full=True)
    path = edges_journal / "facets" / "edges-events" / "events" / "20260430.jsonl"
    path.unlink()

    assert scan_journal(str(edges_journal), full=True) is True
    conn = _conn(edges_journal)
    assert (
        conn.execute(
            "SELECT count(*) FROM edges WHERE source='event-legacy'"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT 1 FROM edge_files WHERE path=?",
            ("facets/edges-events/events/20260430.jsonl",),
        ).fetchone()
        is None
    )
    conn.close()


def test_observation_relations_emit_edges_and_non_relations_still_index(
    edges_journal,
):
    rel = "facets/edges-observations/entities/edge_mina/observations.jsonl"
    extractor = get_edge_source("facets/work/entities/acme/observations.jsonl")
    assert extractor is not None
    assert extractor.__name__ == "extract_observation_edges"

    conn = _conn(edges_journal)
    result = _extract_file_edges(conn, rel, str(edges_journal / rel), {})
    assert result.rows_inserted == 1
    assert result.drops == 1
    row = conn.execute(
        """
        SELECT src, dst, kind, day, facet, source, anchor, label
        FROM edges
        WHERE path=?
        """,
        (rel,),
    ).fetchone()
    assert dict(row) == {
        "src": "edge_mina",
        "dst": "edge_ravi",
        "kind": "works-with",
        "day": "20260430",
        "facet": "edges-observations",
        "source": "observation",
        "anchor": "1777556000000",
        "label": "Plans edge enrichment together",
    }
    conn.close()

    assert index_file(str(edges_journal), rel) is True

    conn = _conn(edges_journal)
    assert (
        conn.execute("SELECT count(*) FROM chunks WHERE path=?", (rel,)).fetchone()[0]
        > 0
    )
    assert (
        conn.execute("SELECT count(*) FROM edges WHERE path=?", (rel,)).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM chunks WHERE path=? AND chunks MATCH ?",
            (rel, '"non relation observation"'),
        ).fetchone()[0]
        > 0
    )
    conn.close()


def test_screen_sources_emit_messaging_calendar_and_event_day(edges_journal):
    rel = "20260430/default/090000_300/screen.jsonl"
    named_rel = "20260430/default/090000_300/left_screen.jsonl"

    assert index_file(str(edges_journal), rel) is True
    assert index_file(str(edges_journal), named_rel) is True

    conn = _conn(edges_journal)
    screen_rows = _edge_rows(conn, f"path='{rel}'")
    named_rows = _edge_rows(conn, f"path='{named_rel}'")
    conn.close()

    messaging = [row for row in screen_rows if row["source"] == "messaging"]
    calendar = [row for row in screen_rows if row["source"] == "calendar"]
    assert len(messaging) == 3
    assert {row["kind"] for row in messaging} == {"messaged-with"}
    assert {row["label"] for row in messaging} == {"Edge Planning"}
    assert {row["weight"] for row in messaging} == {2}

    assert len(calendar) == 3
    assert {row["kind"] for row in calendar} == {"scheduled-with"}
    assert {row["day"] for row in calendar} == {"20260501"}
    assert {row["label"] for row in calendar} == {"Future Edge Review"}

    assert len(named_rows) == 1
    assert named_rows[0]["kind"] == "messaged-with"
    assert named_rows[0]["source"] == "messaging"
    assert named_rows[0]["label"] == "Doc Review"
    assert named_rows[0]["weight"] == 2


def test_pretty_documents_json_edges_via_index_file_and_scan_journal(edges_journal):
    rel = "20260430/default/090000_300/talents/documents.json"

    conn = _conn(edges_journal)
    result = _extract_file_edges(conn, rel, str(edges_journal / "chronicle" / rel), {})
    assert result.rows_inserted == 3
    assert result.drops == 1
    conn.close()

    assert index_file(str(edges_journal), rel) is True
    conn = _conn(edges_journal)
    assert (
        conn.execute(
            "SELECT count(*) FROM edges WHERE path=? AND source='document'",
            (rel,),
        ).fetchone()[0]
        == 3
    )
    assert (
        conn.execute("SELECT count(*) FROM chunks WHERE path=?", (rel,)).fetchone()[0]
        > 0
    )
    conn.close()

    assert scan_journal(str(edges_journal), full=True) is True
    conn = _conn(edges_journal)
    assert (
        conn.execute(
            "SELECT count(*) FROM edges WHERE path=? AND source='document'",
            (rel,),
        ).fetchone()[0]
        == 3
    )
    conn.close()


def _replace_new_source_fixture(shape: str, path: Path) -> None:
    if shape == "observations":
        _write_jsonl(
            path,
            [
                {
                    "content": "Mina works with Ravi after replacement.",
                    "observed_at": 1777557000000,
                    "source_day": "20260430",
                    "relation": {
                        "kind": "works-with",
                        "target_entity_id": "edge_ravi",
                        "target_name": "Ravi Edge",
                        "note": "Replacement relation one",
                    },
                },
                {
                    "content": "Mina knows Tessa after replacement.",
                    "observed_at": 1777557100000,
                    "source_day": "20260430",
                    "relation": {
                        "kind": "knows",
                        "target_entity_id": "edge_tessa",
                        "target_name": "Tessa Edge",
                        "note": "Replacement relation two",
                    },
                },
            ],
        )
        return

    if shape == "screen":
        _write_jsonl(
            path,
            [
                {"raw": "screen.png", "model": "fixture"},
                {
                    "timestamp": 0,
                    "content": {
                        "messaging": {
                            "view": "conversation",
                            "app": "Signal",
                            "thread": "Replacement Thread",
                            "messages": [
                                {
                                    "sender": "Alice Edge",
                                    "timestamp": "2026-04-30T09:10:00Z",
                                    "subject": "",
                                    "text": "Replacement message",
                                },
                                {
                                    "sender": "Bob Edge",
                                    "timestamp": "2026-04-30T09:10:30Z",
                                    "subject": "",
                                    "text": "Replacement reply",
                                },
                            ],
                        }
                    },
                },
            ],
        )
        return

    if shape == "named_screen":
        _write_jsonl(
            path,
            [
                {"raw": "left_screen.png", "model": "fixture"},
                {
                    "timestamp": 0,
                    "content": {
                        "calendar": {
                            "view": "week",
                            "app": "Calendar",
                            "events": [
                                {
                                    "title": "Replacement Calendar",
                                    "start": "20260430T093000",
                                    "end": "20260430T100000",
                                    "calendar": "Work",
                                    "guests": [
                                        "Mina Edge",
                                        "Ravi Edge",
                                        "Tessa Edge",
                                    ],
                                }
                            ],
                        }
                    },
                },
            ],
        )
        return

    if shape == "documents":
        _write_json(
            path,
            {
                "overview": "Replacement document.",
                "parties": [
                    {"name": "Mina Edge", "role": "author"},
                    {"name": "Ravi Edge", "role": "reviewer"},
                ],
                "key_provisions": [],
                "assets": [],
                "conditions": [],
                "important_dates": [],
                "summary": "Replacement parties.",
            },
        )
        return

    raise AssertionError(f"unknown source fixture shape: {shape}")


@pytest.mark.parametrize(
    ("shape", "rel", "initial_rows", "replacement_rows"),
    [
        (
            "observations",
            "facets/edges-observations/entities/edge_mina/observations.jsonl",
            1,
            2,
        ),
        ("screen", "20260430/default/090000_300/screen.jsonl", 6, 1),
        ("named_screen", "20260430/default/090000_300/left_screen.jsonl", 1, 3),
        (
            "documents",
            "20260430/default/090000_300/talents/documents.json",
            3,
            1,
        ),
    ],
)
def test_new_source_shapes_support_index_file_replacement_and_deletion(
    edges_journal,
    shape,
    rel,
    initial_rows,
    replacement_rows,
):
    assert index_file(str(edges_journal), rel) is True
    conn = _conn(edges_journal)
    assert (
        conn.execute("SELECT count(*) FROM edges WHERE path=?", (rel,)).fetchone()[0]
        == initial_rows
    )
    conn.close()

    path = edges_journal / ("chronicle" if rel.startswith("202") else "") / rel
    _replace_new_source_fixture(shape, path)
    _bump_mtime(path)

    assert scan_journal(str(edges_journal), full=True) is True
    conn = _conn(edges_journal)
    assert (
        conn.execute("SELECT count(*) FROM edges WHERE path=?", (rel,)).fetchone()[0]
        == replacement_rows
    )
    conn.close()

    path.unlink()
    assert scan_journal(str(edges_journal), full=True) is True
    conn = _conn(edges_journal)
    assert (
        conn.execute("SELECT count(*) FROM edges WHERE path=?", (rel,)).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT 1 FROM edge_files WHERE path=?", (rel,)).fetchone() is None
    )
    conn.close()


def test_malformed_new_source_fails_without_suppressing_sibling(
    edges_journal,
    caplog,
):
    bad_rel = "20260430/default/090000_300/talents/documents.json"
    bad_path = edges_journal / "chronicle" / bad_rel
    bad_path.write_text("{\n", encoding="utf-8")

    caplog.set_level(logging.ERROR, logger="solstone.think.indexer.edges")
    conn = _conn(edges_journal)
    result = _extract_file_edges(conn, bad_rel, str(bad_path), {})
    assert result.failed
    assert result.rows_inserted == 0
    conn.close()
    assert f"Skipping edge extraction for {bad_rel}" in caplog.text

    caplog.clear()
    caplog.set_level(logging.ERROR, logger="solstone.think.indexer.edges")
    assert scan_journal(str(edges_journal), full=True) is True
    assert f"Skipping edge extraction for {bad_rel}" in caplog.text

    conn = _conn(edges_journal)
    assert (
        conn.execute("SELECT count(*) FROM edges WHERE path=?", (bad_rel,)).fetchone()[
            0
        ]
        == 0
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM edges WHERE path=?",
            ("20260430/default/090000_300/screen.jsonl",),
        ).fetchone()[0]
        == 6
    )
    assert (
        conn.execute("SELECT 1 FROM edge_files WHERE path=?", (bad_rel,)).fetchone()
        is not None
    )
    conn.close()


def test_index_file_supports_edge_only_file(edges_journal, monkeypatch):
    monkeypatch.setitem(
        edge_sources.EDGE_SOURCES,
        "edge-only/*.jsonl",
        ("tests.test_index_edges", "synthetic_edge_extractor"),
    )
    rel = "edge-only/sample.jsonl"
    path = edges_journal / rel
    path.parent.mkdir(parents=True)
    _write_jsonl(path, [{"ok": True}])

    assert index_file(str(edges_journal), rel) is True

    conn = _conn(edges_journal)
    assert (
        conn.execute("SELECT count(*) FROM edges WHERE path=?", (rel,)).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT count(*) FROM chunks WHERE path=?", (rel,)).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT count(*) FROM files WHERE path=?", (rel,)).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT count(*) FROM edge_files WHERE path=?", (rel,)).fetchone()[
            0
        ]
        == 1
    )
    conn.close()


def test_edge_driver_catches_file_boundary_failures(
    edges_journal,
    monkeypatch,
    caplog,
):
    monkeypatch.setitem(
        edge_sources.EDGE_SOURCES,
        "bad-edge/*.jsonl",
        ("tests.test_index_edges", "bad_kind_edge_extractor"),
    )
    rel = "bad-edge/source.jsonl"
    path = edges_journal / rel
    path.parent.mkdir(parents=True)
    _write_jsonl(path, [{"ok": True}])

    caplog.set_level(logging.ERROR, logger="solstone.think.indexer.edges")
    assert index_file(str(edges_journal), rel) is True

    conn = _conn(edges_journal)
    assert (
        conn.execute("SELECT count(*) FROM edges WHERE path=?", (rel,)).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT count(*) FROM edge_files WHERE path=?", (rel,)).fetchone()[
            0
        ]
        == 1
    )
    conn.close()
    assert f"Skipping edge extraction for {rel}" in caplog.text


def test_rescan_file_indexes_activity_edges(edges_journal):
    rel = "facets/edges-activity/activities/20260430.jsonl"
    assert index_file(str(edges_journal), rel) is True

    conn = _conn(edges_journal)
    assert (
        conn.execute(
            "SELECT count(*) FROM edges WHERE path=? AND source='participation'",
            (rel,),
        ).fetchone()[0]
        == 3
    )
    assert (
        conn.execute("SELECT count(*) FROM chunks WHERE path=?", (rel,)).fetchone()[0]
        > 0
    )
    conn.close()


def test_rebuild_edges_is_idempotent_and_preserves_chunks_files(edges_journal):
    scan_journal(str(edges_journal), full=True)
    conn = _conn(edges_journal)
    chunks_hash = table_content_hash(conn, "chunks", CHUNK_COLUMNS)
    files_hash = table_content_hash(conn, "files", FILE_COLUMNS)
    assert (
        conn.execute(
            "SELECT count(*) FROM chunks WHERE path=?",
            ("20260430/default/090000_300/talents/documents.json",),
        ).fetchone()[0]
        > 0
    )
    conn.close()

    first = rebuild_edges(str(edges_journal))
    conn = _conn(edges_journal)
    first_edges_hash = edges_content_hash(conn)
    assert table_content_hash(conn, "chunks", CHUNK_COLUMNS) == chunks_hash
    assert table_content_hash(conn, "files", FILE_COLUMNS) == files_hash
    conn.close()

    second = rebuild_edges(str(edges_journal))
    conn = _conn(edges_journal)
    assert edges_content_hash(conn) == first_edges_hash
    assert table_content_hash(conn, "chunks", CHUNK_COLUMNS) == chunks_hash
    assert table_content_hash(conn, "files", FILE_COLUMNS) == files_hash
    conn.close()

    assert first["rows"] == 24
    assert second["rows"] == 24
    assert first["drops"] == 3
    assert second["drops"] == 3


def test_extract_file_edges_counts_only_resolution_drops(edges_journal):
    conn = _conn(edges_journal)
    rel = "facets/edges-copresence/entities/20260430.jsonl"
    result = _extract_file_edges(conn, rel, str(edges_journal / rel), {})
    assert result.rows_inserted == 2
    assert result.drops == 1
    assert not result.failed
    conn.close()


def test_insert_bad_kind_raises_directly(edges_journal):
    conn = _conn(edges_journal)
    with pytest.raises(ValueError, match="Unknown edge kind"):
        insert_edges(
            conn,
            [
                {
                    "src": "a",
                    "dst": "b",
                    "kind": "not-a-kind",
                    "source": "participation",
                    "path": "synthetic",
                    "weight": 1,
                }
            ],
        )
    conn.close()


def test_insert_edges_validates_whole_batch_before_insert(edges_journal):
    conn = _conn(edges_journal)
    before = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
    with pytest.raises(ValueError, match="Unknown edge kind"):
        insert_edges(
            conn,
            [
                {
                    "src": "edge_ada",
                    "dst": "edge_byron",
                    "kind": "attended-with",
                    "src_name": None,
                    "dst_name": None,
                    "day": "20260430",
                    "facet": "edges-activity",
                    "source": "participation",
                    "path": "synthetic",
                    "anchor": "ok",
                    "label": "valid row",
                    "ts": 0,
                    "weight": 1,
                },
                {
                    "src": "edge_ada",
                    "dst": "edge_byron",
                    "kind": "not-a-kind",
                    "source": "participation",
                    "path": "synthetic",
                    "weight": 1,
                },
            ],
        )
    after = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
    assert after == before
    conn.close()


def test_schema_version_migration_preserves_chunks_and_files(
    edges_journal,
    monkeypatch,
):
    import solstone.think.indexer.edges as edge_index

    scan_journal(str(edges_journal), full=True)
    conn = _conn(edges_journal)
    chunks_hash = table_content_hash(conn, "chunks", CHUNK_COLUMNS)
    files_hash = table_content_hash(conn, "files", FILE_COLUMNS)
    assert conn.execute("SELECT count(*) FROM edges").fetchone()[0] == 24
    conn.close()

    monkeypatch.setattr(edge_index, "EDGES_SCHEMA_VERSION", 2)
    conn = _conn(edges_journal)
    assert (
        conn.execute(
            "SELECT mtime FROM edge_files WHERE path=?", (EDGES_SCHEMA_PATH,)
        ).fetchone()[0]
        == 2
    )
    assert conn.execute("SELECT count(*) FROM edges").fetchone()[0] == 0
    assert table_content_hash(conn, "chunks", CHUNK_COLUMNS) == chunks_hash
    assert table_content_hash(conn, "files", FILE_COLUMNS) == files_hash
    conn.close()

    assert scan_journal(str(edges_journal), full=True) is True
    conn = _conn(edges_journal)
    assert conn.execute("SELECT count(*) FROM edges").fetchone()[0] == 24
    conn.close()


def test_discover_files_day_rooted_paths_are_chronicle_free(tmp_path):
    journal = tmp_path / "journal"
    target = journal / "chronicle" / "20260430" / "synthetic" / "input.jsonl"
    target.parent.mkdir(parents=True)
    _write_jsonl(target, [{"ok": True}])

    files = discover_files(str(journal), [], ["*/synthetic/*.jsonl"])

    assert files == {"20260430/synthetic/input.jsonl": str(target)}
