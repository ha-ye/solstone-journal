# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Derived entity-to-entity edge index maintenance."""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from solstone.think.edge_sources import (
    EdgeContext,
    edge_source_patterns,
    get_edge_source,
)
from solstone.think.entities.loading import load_entities
from solstone.think.entities.matching import find_matching_entity
from solstone.think.formatters import discover_files, extract_path_metadata, load_jsonl

logger = logging.getLogger(__name__)

EDGES_SCHEMA_VERSION = 1
EDGES_SCHEMA_PATH = "edges:__schema__"
KINDS = frozenset(
    {"attended-with", "co-present", "spoke-with", "mentioned", "committed-to"}
)
DIRECTED_KINDS = frozenset({"committed-to"})

EDGE_COLUMNS = (
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
)


@dataclass
class EdgeFileResult:
    """Outcome from one edge source file extraction attempt."""

    rows_inserted: int = 0
    drops: int = 0
    failed: bool = False


def _ensure_edges_schema(conn: sqlite3.Connection) -> None:
    """Create or rebuild edge tables without touching chunk/index tables."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS edge_files(path TEXT PRIMARY KEY, mtime INTEGER)"
    )
    row = conn.execute(
        "SELECT mtime FROM edge_files WHERE path=?", (EDGES_SCHEMA_PATH,)
    ).fetchone()
    try:
        version = int(row[0]) if row else None
    except (TypeError, ValueError):
        version = None

    version_mismatch = version != EDGES_SCHEMA_VERSION
    if version_mismatch:
        conn.execute("DROP TABLE IF EXISTS edges")
        conn.execute("DELETE FROM edge_files")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edges(
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
    conn.execute("CREATE INDEX IF NOT EXISTS edges_path ON edges(path)")

    if version_mismatch:
        conn.execute(
            "REPLACE INTO edge_files(path, mtime) VALUES (?, ?)",
            (EDGES_SCHEMA_PATH, EDGES_SCHEMA_VERSION),
        )
        conn.commit()


def insert_edges(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    """Validate, normalize, and insert edge rows."""
    prepared: list[tuple[Any, ...]] = []
    for row in rows:
        kind = row.get("kind")
        if kind not in KINDS:
            raise ValueError(f"Unknown edge kind: {kind!r}")

        src = row.get("src")
        dst = row.get("dst")
        if not isinstance(src, str) or not src:
            raise ValueError("edge row requires non-empty string src")
        if not isinstance(dst, str) or not dst:
            raise ValueError("edge row requires non-empty string dst")

        directed = 1 if kind in DIRECTED_KINDS else 0
        src_name = row.get("src_name")
        dst_name = row.get("dst_name")
        if not directed and src > dst:
            src, dst = dst, src
            src_name, dst_name = dst_name, src_name

        facet = row.get("facet")
        if isinstance(facet, str) and facet:
            facet = facet.lower()

        values = {
            "src": src,
            "dst": dst,
            "kind": kind,
            "directed": directed,
            "src_name": src_name,
            "dst_name": dst_name,
            "day": row.get("day"),
            "facet": facet,
            "source": row.get("source"),
            "path": row.get("path"),
            "anchor": row.get("anchor"),
            "label": row.get("label"),
            "ts": row.get("ts"),
            "weight": row.get("weight"),
        }
        prepared.append(tuple(values[column] for column in EDGE_COLUMNS))

    conn.executemany(
        """
        INSERT INTO edges(
            src, dst, kind, directed, src_name, dst_name, day, facet,
            source, path, anchor, label, ts, weight
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        prepared,
    )

    return len(prepared)


def delete_edges_for_path(conn: sqlite3.Connection, path: str) -> int:
    """Delete edge rows and ledger entry for one source path."""
    deleted = conn.execute("DELETE FROM edges WHERE path=?", (path,)).rowcount
    if path != EDGES_SCHEMA_PATH:
        conn.execute("DELETE FROM edge_files WHERE path=?", (path,))
    return int(deleted or 0)


def edge_file_mtimes(conn: sqlite3.Connection) -> dict[str, int]:
    """Return edge source file mtimes, excluding the schema pseudo-row."""
    return {
        path: int(mtime)
        for path, mtime in conn.execute(
            "SELECT path, mtime FROM edge_files WHERE path != ?",
            (EDGES_SCHEMA_PATH,),
        )
    }


def discover_edge_files(journal: str) -> dict[str, str]:
    """Discover edge source files using the shared formatter glob helper."""
    structural, day_rooted = edge_source_patterns()
    return discover_files(journal, structural, day_rooted)


def make_edge_context(
    rel: str,
    entity_cache: dict[str, list[dict[str, Any]]],
    drop_counter: dict[str, int],
) -> EdgeContext:
    """Build a context whose resolver targets facet-attached entities."""
    path_meta = extract_path_metadata(rel)
    facet = path_meta["facet"]

    def resolve(name: str) -> str | None:
        if not isinstance(name, str) or not name.strip():
            drop_counter["drops"] += 1
            return None
        if facet not in entity_cache:
            entity_cache[facet] = load_entities(facet, day=None)
        match = find_matching_entity(name, entity_cache[facet])
        entity_id = match.get("id") if match else None
        if not entity_id:
            drop_counter["drops"] += 1
            return None
        return str(entity_id)

    return EdgeContext(
        path=rel,
        day=path_meta["day"],
        facet=facet,
        resolve=resolve,
    )


def _extract_file_edges(
    conn: sqlite3.Connection,
    rel: str,
    abs_path: str,
    entity_cache: dict[str, list[dict[str, Any]]],
) -> EdgeFileResult:
    """Load, extract, and insert edges for one source file boundary."""
    extractor = get_edge_source(rel)
    if extractor is None:
        return EdgeFileResult()

    drop_counter = {"drops": 0}
    ctx = make_edge_context(rel, entity_cache, drop_counter)
    try:
        entries = load_jsonl(abs_path)
        rows = extractor(entries, ctx)
        inserted = insert_edges(conn, rows)
    except Exception:
        logger.exception("Skipping edge extraction for %s", rel)
        return EdgeFileResult(drops=drop_counter["drops"], failed=True)

    return EdgeFileResult(rows_inserted=inserted, drops=drop_counter["drops"])


def replace_edge_file_mtime(conn: sqlite3.Connection, rel: str, mtime: int) -> None:
    """Record the last processed mtime for one edge source file."""
    conn.execute("REPLACE INTO edge_files(path, mtime) VALUES (?, ?)", (rel, mtime))


def rebuild_edges(journal: str) -> dict[str, int]:
    """Rebuild only edge tables from discovered edge source files."""
    from solstone.think.indexer.journal import get_journal_index

    conn, _ = get_journal_index(journal)
    conn.execute("DELETE FROM edges")
    conn.execute("DELETE FROM edge_files")
    conn.execute(
        "REPLACE INTO edge_files(path, mtime) VALUES (?, ?)",
        (EDGES_SCHEMA_PATH, EDGES_SCHEMA_VERSION),
    )

    files = discover_edge_files(journal)
    entity_cache: dict[str, list[dict[str, Any]]] = {}
    rows_inserted = 0
    drops = 0
    failed = 0
    processed = 0

    for rel, abs_path in sorted(files.items()):
        try:
            mtime = int(os.path.getmtime(abs_path))
        except OSError:
            continue
        result = _extract_file_edges(conn, rel, abs_path, entity_cache)
        rows_inserted += result.rows_inserted
        drops += result.drops
        failed += int(result.failed)
        replace_edge_file_mtime(conn, rel, mtime)
        processed += 1

    conn.commit()
    conn.close()
    return {
        "files": processed,
        "rows": rows_inserted,
        "drops": drops,
        "failed": failed,
    }
