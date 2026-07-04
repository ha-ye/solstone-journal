# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Importer-owned health dedupe state."""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEDUPE_DB_RELATIVE_PATH = Path("imports") / "health-dedupe.sqlite"


@dataclass(frozen=True, slots=True)
class HealthDedupeRecord:
    """One row in the health dedupe database."""

    dedupe_key: str
    source_family: str
    record_type: str
    start_time: str
    end_time: str | None = None
    source_record_id: str | None = None
    value_hash: str | None = None
    first_import_id: str | None = None
    last_seen_import_id: str | None = None
    normalized_ref: str | None = None
    raw_ref: str | None = None

def health_dedupe_db_path(journal_root: Path) -> Path:
    """Return the journal-local health dedupe database path."""

    return Path(journal_root) / DEDUPE_DB_RELATIVE_PATH


def ensure_health_dedupe_db(journal_root: Path) -> Path:
    """Create the health dedupe database and return its path."""

    db_path = health_dedupe_db_path(journal_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS health_dedupe (
                dedupe_key TEXT PRIMARY KEY,
                source_family TEXT NOT NULL,
                source_record_id TEXT,
                record_type TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                value_hash TEXT,
                first_import_id TEXT,
                last_seen_import_id TEXT,
                normalized_ref TEXT,
                raw_ref TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_health_dedupe_source_record
            ON health_dedupe (source_family, source_record_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_health_dedupe_record_time
            ON health_dedupe (record_type, start_time, end_time)
            """
        )
    return db_path


def get_health_dedupe_record(
    journal_root: Path,
    dedupe_key: str,
) -> dict[str, Any] | None:
    """Fetch one dedupe record as a plain dict."""

    db_path = health_dedupe_db_path(journal_root)
    if not db_path.exists():
        return None

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM health_dedupe WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
    return dict(row) if row is not None else None


def upsert_health_dedupe_record(
    journal_root: Path,
    record: HealthDedupeRecord,
) -> bool:
    """Insert or update a dedupe row.

    Returns True when the row was newly inserted, False when it already existed.
    """

    db_path = ensure_health_dedupe_db(journal_root)
    now = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    first_import_id = record.first_import_id or record.last_seen_import_id
    last_seen_import_id = record.last_seen_import_id or record.first_import_id

    with sqlite3.connect(db_path) as conn:
        insert_result = conn.execute(
            """
            INSERT INTO health_dedupe (
                dedupe_key,
                source_family,
                source_record_id,
                record_type,
                start_time,
                end_time,
                value_hash,
                first_import_id,
                last_seen_import_id,
                normalized_ref,
                raw_ref,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dedupe_key) DO NOTHING
            """,
            (
                record.dedupe_key,
                record.source_family,
                record.source_record_id,
                record.record_type,
                record.start_time,
                record.end_time,
                record.value_hash,
                first_import_id,
                last_seen_import_id,
                record.normalized_ref,
                record.raw_ref,
                now,
                now,
            ),
        )
        if insert_result.rowcount == 1:
            return True

        conn.execute(
            """
            UPDATE health_dedupe
            SET
                last_seen_import_id = ?,
                normalized_ref = COALESCE(?, normalized_ref),
                raw_ref = COALESCE(?, raw_ref),
                updated_at = ?
            WHERE dedupe_key = ?
            """,
            (
                last_seen_import_id,
                record.normalized_ref,
                record.raw_ref,
                now,
                record.dedupe_key,
            ),
        )
    return False
