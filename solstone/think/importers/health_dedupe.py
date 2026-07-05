# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Importer-owned health dedupe state."""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterable
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


@dataclass(frozen=True, slots=True)
class HealthDedupeBatchResult:
    """Summary of a batched dedupe upsert."""

    inserted: int
    updated: int


def health_dedupe_db_path(journal_root: Path) -> Path:
    """Return the journal-local health dedupe database path."""

    return Path(journal_root) / DEDUPE_DB_RELATIVE_PATH


def _ensure_health_dedupe_schema(conn: sqlite3.Connection) -> None:
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


def ensure_health_dedupe_db(journal_root: Path) -> Path:
    """Create the health dedupe database and return its path."""

    db_path = health_dedupe_db_path(journal_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        _ensure_health_dedupe_schema(conn)
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
                value_hash = COALESCE(?, value_hash),
                normalized_ref = COALESCE(?, normalized_ref),
                raw_ref = COALESCE(?, raw_ref),
                updated_at = ?
            WHERE dedupe_key = ?
            """,
            (
                last_seen_import_id,
                record.value_hash,
                record.normalized_ref,
                record.raw_ref,
                now,
                record.dedupe_key,
            ),
        )
    return False


def upsert_health_dedupe_records(
    journal_root: Path,
    records: Iterable[HealthDedupeRecord],
) -> HealthDedupeBatchResult:
    """Insert or update dedupe rows in one SQLite batch.

    Uses one connection, WAL mode, one transaction, and one executemany upsert
    for the batch. ``inserted`` counts dedupe keys that were absent before this
    batch began; duplicate keys later in the same batch count as updates.
    """

    db_path = health_dedupe_db_path(journal_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    batch = tuple(records)
    now = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN")
        try:
            _ensure_health_dedupe_schema(conn)
            existing_keys = _existing_dedupe_keys(conn, [r.dedupe_key for r in batch])
            conn.executemany(
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
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    last_seen_import_id = excluded.last_seen_import_id,
                    value_hash = COALESCE(
                        excluded.value_hash,
                        health_dedupe.value_hash
                    ),
                    normalized_ref = COALESCE(
                        excluded.normalized_ref,
                        health_dedupe.normalized_ref
                    ),
                    raw_ref = COALESCE(excluded.raw_ref, health_dedupe.raw_ref),
                    updated_at = excluded.updated_at
                """,
                [_record_insert_values(record, now) for record in batch],
            )
        except Exception:
            conn.rollback()
            raise
        conn.commit()

    batch_keys = [record.dedupe_key for record in batch]
    inserted = len(set(batch_keys) - existing_keys)
    return HealthDedupeBatchResult(inserted=inserted, updated=len(batch) - inserted)


def _existing_dedupe_keys(conn: sqlite3.Connection, dedupe_keys: list[str]) -> set[str]:
    existing: set[str] = set()
    for offset in range(0, len(dedupe_keys), 900):
        chunk = dedupe_keys[offset : offset + 900]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT dedupe_key FROM health_dedupe WHERE dedupe_key IN ({placeholders})",
            chunk,
        )
        existing.update(row[0] for row in rows)
    return existing


def _record_insert_values(record: HealthDedupeRecord, now: str) -> tuple[Any, ...]:
    first_import_id = record.first_import_id or record.last_seen_import_id
    last_seen_import_id = record.last_seen_import_id or record.first_import_id
    return (
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
    )
