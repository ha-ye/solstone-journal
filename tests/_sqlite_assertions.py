# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""SQLite assertion helpers for content-stable table comparisons."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from typing import Any

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
EDGE_FILE_COLUMNS = ["path", "mtime"]
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


def rows_content_hash(rows: Sequence[Sequence[Any]]) -> str:
    payload = json.dumps(
        rows,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def table_content_hash(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
) -> str:
    """Hash explicit-column table contents without relying on rowid order."""
    column_sql = ", ".join(columns)
    rows: list[list[Any]] = [
        list(row)
        for row in conn.execute(
            f"SELECT {column_sql} FROM {table} ORDER BY {column_sql}"
        )
    ]
    return rows_content_hash(rows)


def edges_content_hash(conn: sqlite3.Connection) -> str:
    """Hash the edge table using its explicit L1 contract columns."""
    return table_content_hash(conn, "edges", EDGE_COLUMNS)
