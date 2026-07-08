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
from solstone.think.edge_sources import EdgeContext, get_edge_source
from solstone.think.formatters import discover_files
from solstone.think.indexer.edges import (
    EDGES_SCHEMA_PATH,
    _extract_file_edges,
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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _bump_mtime(path: Path) -> None:
    bumped = path.stat().st_mtime + 2
    os.utime(path, (bumped, bumped))


def test_scan_indexes_edges_and_second_scan_is_zero_delta(edges_journal):
    assert scan_journal(str(edges_journal), full=True) is True

    conn = _conn(edges_journal)
    schema_version = conn.execute(
        "SELECT mtime FROM edge_files WHERE path=?", (EDGES_SCHEMA_PATH,)
    ).fetchone()[0]
    assert schema_version == 1
    assert conn.execute("SELECT count(*) FROM edges").fetchone()[0] == 10
    assert _source_counts(conn) == {
        "co-presence": 2,
        "closure": 1,
        "commitment": 1,
        "event-legacy": 3,
        "participation": 3,
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


def test_observations_file_has_no_copresence_edge_source(edges_journal):
    rel = "facets/edges-copresence/entities/edge_alice/observations.jsonl"
    assert get_edge_source("facets/work/entities/acme/observations.jsonl") is None

    path = edges_journal / rel
    _write_jsonl(path, [{"content": "Observation-only content", "observed_at": 1}])
    assert index_file(str(edges_journal), rel) is True

    conn = _conn(edges_journal)
    assert (
        conn.execute("SELECT count(*) FROM chunks WHERE path=?", (rel,)).fetchone()[0]
        > 0
    )
    assert (
        conn.execute("SELECT count(*) FROM edges WHERE path=?", (rel,)).fetchone()[0]
        == 0
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

    assert first["rows"] == 10
    assert second["rows"] == 10
    assert first["drops"] == 1
    assert second["drops"] == 1


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
    assert conn.execute("SELECT count(*) FROM edges").fetchone()[0] == 10
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
    assert conn.execute("SELECT count(*) FROM edges").fetchone()[0] == 10
    conn.close()


def test_discover_files_day_rooted_paths_are_chronicle_free(tmp_path):
    journal = tmp_path / "journal"
    target = journal / "chronicle" / "20260430" / "synthetic" / "input.jsonl"
    target.parent.mkdir(parents=True)
    _write_jsonl(target, [{"ok": True}])

    files = discover_files(str(journal), [], ["*/synthetic/*.jsonl"])

    assert files == {"20260430/synthetic/input.jsonl": str(target)}
