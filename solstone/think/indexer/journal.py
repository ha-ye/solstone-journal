# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Unified journal index for all content types.

This module provides a single FTS5 index over journal content:
- Agent outputs (markdown files)
- Events (facet event JSONL)
- Entities (facet entity JSONL)
- Todos (facet todo JSONL)
- Action logs (facet/journal-level JSONL)

All content is converted to markdown chunks via the formatters framework,
then indexed with metadata fields for filtering (day, facet, agent).
Raw audio/screen transcripts are formattable but not indexed by default.
"""

import calendar
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from solstone.think.entities.core import entity_slug
from solstone.think.formatters import (
    extract_path_metadata,
    find_formattable_files,
    format_file,
    get_formatter,
)
from solstone.think.markdown import format_markdown
from solstone.think.utils import (
    DATE_RE,
    get_journal,
    journal_relative_path,
    resolve_journal_path,
    segment_key,
    segment_parse,
)

logger = logging.getLogger(__name__)


# Database constants
INDEX_DIR = "indexer"
DB_NAME = "journal.sqlite"

# Schema for the unified journal index
SCHEMA = [
    "CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime INTEGER)",
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
        content,
        path UNINDEXED,
        day UNINDEXED,
        facet UNINDEXED,
        agent UNINDEXED,
        stream UNINDEXED,
        idx UNINDEXED,
        time_bucket UNINDEXED
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS entities(
        entity_id TEXT NOT NULL,
        source TEXT NOT NULL,
        facet TEXT,
        day TEXT,
        name TEXT,
        type TEXT,
        description TEXT,
        tags TEXT,
        contact TEXT,
        aka TEXT,
        is_principal INTEGER,
        blocked INTEGER,
        observation_count INTEGER,
        last_observed TEXT,
        attached_at TEXT,
        updated_at TEXT,
        last_seen TEXT,
        created_at TEXT,
        detached INTEGER,
        path TEXT NOT NULL,
        PRIMARY KEY (path, entity_id, source)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_entities_id ON entities(entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_entities_facet ON entities(facet)",
    "CREATE INDEX IF NOT EXISTS idx_entities_source ON entities(source)",
]

ENTITY_COLUMNS = [
    "entity_id",
    "source",
    "facet",
    "day",
    "name",
    "type",
    "description",
    "tags",
    "contact",
    "aka",
    "is_principal",
    "blocked",
    "observation_count",
    "last_observed",
    "attached_at",
    "updated_at",
    "last_seen",
    "created_at",
    "detached",
    "path",
]


def _insert_entity_row(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Insert one entity row into the entities table."""
    columns = ",".join(ENTITY_COLUMNS)
    placeholders = ",".join("?" for _ in ENTITY_COLUMNS)
    values = [row.get(col) for col in ENTITY_COLUMNS]
    conn.execute(f"INSERT INTO entities({columns}) VALUES ({placeholders})", values)


def _entity_source_from_path(rel_path: str) -> str | None:
    """Infer entity source type from a relative path."""
    rel = rel_path.replace("\\", "/")
    if re.fullmatch(r"entities/[^/]+/entity\.json", rel):
        return "identity"
    if re.fullmatch(r"facets/[^/]+/entities/[^/]+/entity\.json", rel):
        return "relationship"
    if re.match(r"\d{8}\.jsonl$", Path(rel).name) and re.fullmatch(
        r"facets/[^/]+/entities/\d{8}\.jsonl", rel
    ):
        return "detected"
    if re.fullmatch(r"facets/[^/]+/entities/[^/]+/observations\.jsonl", rel):
        return "observation"
    return None


def _find_entity_files(journal: str) -> dict[str, tuple[str, str]]:
    """Find all supported entity source files.

    Returns:
        Mapping from relative path to tuple of (absolute_path, source_type).
    """
    journal_path = Path(journal)
    files: dict[str, tuple[str, str]] = {}

    for path in journal_path.glob("entities/*/entity.json"):
        if path.is_file():
            rel = path.relative_to(journal_path).as_posix()
            files[rel] = (str(path), "identity")

    for path in journal_path.glob("facets/*/entities/*/entity.json"):
        if path.is_file():
            rel = path.relative_to(journal_path).as_posix()
            files[rel] = (str(path), "relationship")

    for path in journal_path.glob("facets/*/entities/*.jsonl"):
        filename = path.name
        if re.match(r"\d{8}\.jsonl$", filename):
            rel = path.relative_to(journal_path).as_posix()
            files[rel] = (str(path), "detected")

    for path in journal_path.glob("facets/*/entities/*/observations.jsonl"):
        if path.is_file():
            rel = path.relative_to(journal_path).as_posix()
            files[rel] = (str(path), "observation")

    return files


def _extract_entity_identity(journal: str, rel_path: str) -> list[dict[str, Any]]:
    """Read a journal entity file and return row data."""
    abs_path = os.path.join(journal, rel_path)
    rel_parts = rel_path.replace("\\", "/").split("/")
    if len(rel_parts) < 2:
        return []
    entity_id = rel_parts[1]

    try:
        with open(abs_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Skipping %s: %s", rel_path, e)
        return []

    return [
        {
            "entity_id": entity_id,
            "source": "identity",
            "facet": None,
            "day": None,
            "name": data.get("name"),
            "type": data.get("type"),
            "description": None,
            "tags": None,
            "contact": None,
            "aka": json.dumps(data["aka"]) if data.get("aka") else None,
            "is_principal": 1 if data.get("is_principal") else 0,
            "blocked": 1 if data.get("blocked") else 0,
            "observation_count": None,
            "last_observed": None,
            "attached_at": None,
            "updated_at": data.get("updated_at"),
            "last_seen": None,
            "created_at": data.get("created_at"),
            "detached": None,
            "path": rel_path,
        }
    ]


def _extract_entity_relationship(journal: str, rel_path: str) -> list[dict[str, Any]]:
    """Read a relationship entity file and return row data."""
    abs_path = os.path.join(journal, rel_path)
    rel_parts = rel_path.replace("\\", "/").split("/")
    if len(rel_parts) < 4:
        return []
    facet = rel_parts[1]
    entity_id = rel_parts[3]

    try:
        with open(abs_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Skipping %s: %s", rel_path, e)
        return []

    return [
        {
            "entity_id": entity_id,
            "source": "relationship",
            "facet": facet,
            "day": None,
            "name": None,
            "type": None,
            "description": data.get("description"),
            "tags": json.dumps(data["tags"]) if data.get("tags") else None,
            "contact": data.get("contact"),
            "aka": None,
            "is_principal": None,
            "blocked": None,
            "observation_count": None,
            "last_observed": None,
            "attached_at": data.get("attached_at"),
            "updated_at": data.get("updated_at"),
            "last_seen": data.get("last_seen"),
            "created_at": None,
            "detached": 1 if data.get("detached") else 0,
            "path": rel_path,
        }
    ]


def _extract_entity_detected(journal: str, rel_path: str) -> list[dict[str, Any]]:
    """Read an entities JSONL file and return one row per valid entity."""
    abs_path = os.path.join(journal, rel_path)
    rel_parts = rel_path.replace("\\", "/").split("/")
    if len(rel_parts) < 3:
        return []
    facet = rel_parts[1]
    day = Path(rel_path).name.removesuffix(".jsonl")

    rows: list[dict[str, Any]] = []
    try:
        with open(abs_path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    logger.warning("Skipping malformed JSONL in %s: %s", rel_path, e)
                    continue

                name = data.get("name")
                if not name:
                    continue

                entity_id = data.get("id") or entity_slug(name)
                rows.append(
                    {
                        "entity_id": entity_id,
                        "source": "detected",
                        "facet": facet,
                        "day": day,
                        "name": name,
                        "type": data.get("type"),
                        "description": data.get("description"),
                        "tags": None,
                        "contact": None,
                        "aka": None,
                        "is_principal": None,
                        "blocked": None,
                        "observation_count": None,
                        "last_observed": None,
                        "attached_at": None,
                        "updated_at": None,
                        "last_seen": None,
                        "created_at": None,
                        "detached": None,
                        "path": rel_path,
                    }
                )
    except OSError as e:
        logger.warning("Skipping %s: %s", rel_path, e)
        return []

    return rows


def _extract_entity_observations(journal: str, rel_path: str) -> list[dict[str, Any]]:
    """Summarize observations for an entity into a single row."""
    abs_path = os.path.join(journal, rel_path)
    rel_parts = rel_path.replace("\\", "/").split("/")
    if len(rel_parts) < 4:
        return []
    facet = rel_parts[1]
    entity_id = rel_parts[3]

    count = 0
    last_observed = None

    try:
        with open(abs_path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    logger.warning("Skipping malformed JSONL in %s: %s", rel_path, e)
                    continue

                count += 1
                observed_at = data.get("observed_at")
                if observed_at and (
                    last_observed is None or observed_at > last_observed
                ):
                    last_observed = observed_at
    except OSError as e:
        logger.warning("Skipping %s: %s", rel_path, e)
        return []

    if count == 0:
        return []

    return [
        {
            "entity_id": entity_id,
            "source": "observation",
            "facet": facet,
            "day": None,
            "name": None,
            "type": None,
            "description": None,
            "tags": None,
            "contact": None,
            "aka": None,
            "is_principal": None,
            "blocked": None,
            "observation_count": count,
            "last_observed": last_observed,
            "attached_at": None,
            "updated_at": None,
            "last_seen": None,
            "created_at": None,
            "detached": None,
            "path": rel_path,
        }
    ]


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create required tables if they don't exist."""
    conn.execute("DROP TABLE IF EXISTS entity_signals")
    for statement in SCHEMA:
        conn.execute(statement)

    # Detect old schema missing time_bucket — FTS5 cannot ALTER, must rebuild
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks'"
    ).fetchone()
    if row and "time_bucket" not in row[0]:
        logger.warning(
            "Schema migration: rebuilding chunks table to add time_bucket column"
        )
        conn.execute("DROP TABLE IF EXISTS chunks")
        conn.execute("DROP TABLE IF EXISTS files")
        for statement in SCHEMA:
            conn.execute(statement)


def _time_bucket(rel: str) -> str:
    """Derive time bucket from a journal-relative path.

    Returns 'morning' (06-11), 'afternoon' (12-16), 'evening' (17-20),
    'night' (21-05), or '' for non-segment content.
    """
    start_time, _ = segment_parse(rel)
    if start_time is None:
        return ""
    hour = start_time.hour
    if 6 <= hour <= 11:
        return "morning"
    elif 12 <= hour <= 16:
        return "afternoon"
    elif 17 <= hour <= 20:
        return "evening"
    else:
        return "night"


def get_journal_index(journal: str | None = None) -> tuple[sqlite3.Connection, str]:
    """Return SQLite connection for the journal index.

    Args:
        journal: Path to journal root. Uses SOLSTONE_JOURNAL env var if not provided.

    Returns:
        Tuple of (connection, db_path)
    """
    journal = journal or get_journal()

    db_dir = os.path.join(journal, INDEX_DIR)
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, DB_NAME)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_schema(conn)

    return conn, db_path


def reset_journal_index(journal: str) -> None:
    """Remove the journal index database file."""
    db_path = os.path.join(journal, INDEX_DIR, DB_NAME)
    try:
        os.unlink(db_path)
    except FileNotFoundError:
        pass


def index_file(journal: str, file_path: str, verbose: bool = False) -> bool:
    """Index a single file into the journal index.

    Validates that the file exists, is under the journal directory, and has
    a registered formatter. Then indexes it (replacing any existing chunks).

    Args:
        journal: Path to journal root directory
        file_path: Absolute or journal-relative path to file
        verbose: If True, log detailed progress

    Returns:
        True if file was indexed successfully

    Raises:
        ValueError: If file is outside journal or has no formatter
        FileNotFoundError: If file doesn't exist
    """
    journal_path = Path(journal).resolve()

    # Resolve file path (handle both absolute and relative)
    if os.path.isabs(file_path):
        abs_path = Path(file_path).resolve()
    else:
        abs_path = resolve_journal_path(journal_path, file_path).resolve()

    # Validate file exists
    if not abs_path.is_file():
        raise FileNotFoundError(f"File not found: {abs_path}")

    # Validate file is under journal
    try:
        rel_path = journal_relative_path(journal_path, abs_path)
    except ValueError:
        raise ValueError(f"File is outside journal directory: {abs_path}") from None

    # Validate formatter exists
    if get_formatter(rel_path) is None:
        raise ValueError(f"No formatter found for: {rel_path}")

    # Get file mtime
    mtime = int(os.path.getmtime(abs_path))

    # Index the file
    conn, _ = get_journal_index(journal)

    # Delete existing chunks for this file
    conn.execute("DELETE FROM chunks WHERE path=?", (rel_path,))

    if verbose:
        logger.info("Indexing %s", rel_path)

    stream = _extract_stream(journal, rel_path)
    _index_file(conn, rel_path, str(abs_path), verbose, stream=stream)

    # Update file mtime
    conn.execute("REPLACE INTO files(path, mtime) VALUES (?, ?)", (rel_path, mtime))

    # Regenerate segment chunk if file is in a segment
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) >= 4 and segment_key(parts[2]):
        rel_segment = "/".join(parts[:3])
        seg_dir = str(resolve_journal_path(journal, rel_segment))
        conn.execute("DELETE FROM chunks WHERE path=?", (rel_segment,))
        if os.path.isdir(seg_dir):
            seg_stream = _extract_stream(journal, rel_segment + "/dummy")
            _index_segment_chunks(conn, seg_dir, rel_segment, seg_stream, verbose)

    conn.commit()
    conn.close()

    return True


def _extract_stream(journal: str, rel: str) -> str | None:
    """Extract stream name from a journal-relative path's segment directory.

    Reads stream.json from the segment dir if the path is inside a segment
    (e.g., "20240101/142500_300/talents/facet/flow.md").

    Returns stream name string or None for non-segment paths or pre-stream segments.
    """
    from solstone.think.streams import read_segment_stream

    parts = rel.replace("\\", "/").split("/")
    # Segment paths: parts[0]=day, parts[1]=stream, parts[2]=segment, parts[3+]=file
    if len(parts) >= 3 and segment_key(parts[2]):
        seg_dir = str(resolve_journal_path(journal, "/".join(parts[:3])))
        marker = read_segment_stream(seg_dir)
        if marker:
            return marker.get("stream")
    return None


def _index_file(
    conn: sqlite3.Connection,
    rel: str,
    path: str,
    verbose: bool,
    stream: str | None = None,
) -> None:
    """Index a single file into the chunks table.

    Uses format_file() to convert content to markdown chunks,
    then inserts each chunk with metadata.

    Metadata is sourced from two places:
    - Path-derived: day and facet from extract_path_metadata()
    - Formatter-provided: agent from meta["indexer"]["agent"]
    For markdown files, agent is also path-derived.
    """
    try:
        chunks, meta = format_file(path)
    except (ValueError, FileNotFoundError) as e:
        logger.warning("Skipping %s: %s", rel, e)
        return

    # Get path-derived metadata (day, facet, agent for .md files)
    path_meta = extract_path_metadata(rel)

    # Get formatter-provided metadata (agent for JSONL files)
    formatter_indexer = meta.get("indexer", {})

    # Merge: formatter values override path values, normalize to lowercase
    day = formatter_indexer.get("day") or path_meta["day"]
    facet = (formatter_indexer.get("facet") or path_meta["facet"]).lower()
    agent = (formatter_indexer.get("agent") or path_meta["agent"]).lower()

    if verbose:
        logger.info(
            "  %s chunks, day=%s, facet=%s, agent=%s, stream=%s",
            len(chunks),
            day,
            facet,
            agent,
            stream,
        )

    for idx, chunk in enumerate(chunks):
        content = chunk.get("markdown", "")
        if not content:
            continue

        conn.execute(
            "INSERT INTO chunks(content, path, day, facet, agent, stream, idx, time_bucket) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (content, rel, day, facet, agent, stream, idx, _time_bucket(rel)),
        )


def _index_segment_chunks(
    conn: sqlite3.Connection,
    segment_dir: str,
    rel_segment: str,
    stream: str | None,
    verbose: bool,
) -> int:
    """Index concatenated markdown content for one segment."""
    segment_path = Path(segment_dir)
    talent_files = sorted(
        [
            *segment_path.glob("talents/*.md"),
            *segment_path.glob("talents/*/*.md"),
        ],
        key=lambda path: str(path),
    )
    if not talent_files:
        return 0

    content = "\n\n---\n\n".join(
        path.read_text(encoding="utf-8") for path in talent_files
    )
    chunks, _meta = format_markdown(content)
    day = rel_segment.replace("\\", "/").split("/")[0]

    inserted = 0
    for idx, chunk in enumerate(chunks):
        chunk_content = chunk.get("markdown", "")
        if not chunk_content:
            continue
        conn.execute(
            "INSERT INTO chunks(content, path, day, facet, agent, stream, idx, time_bucket) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chunk_content,
                rel_segment,
                day,
                "",
                "segment",
                stream,
                idx,
                _time_bucket(rel_segment),
            ),
        )
        inserted += 1

    if verbose:
        logger.info(
            "  %s segment chunks, path=%s, stream=%s", inserted, rel_segment, stream
        )

    return inserted


def _is_historical_day(rel_path: str) -> bool:
    """Check if path is in a historical YYYYMMDD directory (before today).

    Returns True for paths like "20240101/..." where the date is before today.
    Returns False for non-day paths (facets/, imports/, apps/) or today/future.
    """
    from datetime import datetime

    if not rel_path or "/" not in rel_path:
        return False

    first_part = rel_path.split("/")[0]
    if not DATE_RE.fullmatch(first_part):
        return False  # Not a day directory

    today = datetime.now().strftime("%Y%m%d")
    return first_part < today


def _is_historical_entity_file(rel_path: str, source_type: str | None) -> bool:
    """Check if a detected entity file is historical based on filename day."""
    if source_type != "detected":
        return False

    from datetime import datetime

    filename = Path(rel_path).name
    match = re.match(r"\d{8}\.jsonl$", filename)
    if not match:
        return False

    today = datetime.now().strftime("%Y%m%d")
    day = filename[:-6]
    return day < today


def scan_entities(
    journal: str,
    conn: sqlite3.Connection,
    verbose: bool = False,
    full: bool = False,
) -> bool:
    """Scan and index entity source files."""
    entity_files = _find_entity_files(journal)

    in_scope: dict[str, tuple[str, str]] = entity_files
    if not full:
        in_scope = {
            rel: (path, source_type)
            for rel, (path, source_type) in entity_files.items()
            if not _is_historical_entity_file(rel, source_type)
        }

    logger.info("Scanning %s entity files...", len(in_scope))

    # Get current file mtimes from database.
    # Entity paths are stored with an "entity:" prefix to avoid collisions
    # with the chunk indexer, which also tracks some of the same files.
    ENTITY_PREFIX = "entity:"
    db_mtimes = {
        path: mtime
        for path, mtime in conn.execute(
            "SELECT path, mtime FROM files WHERE path LIKE 'entity:%'"
        )
    }
    to_index: list[tuple[str, str, int, str]] = []
    for rel, (path, source_type) in in_scope.items():
        try:
            mtime = int(os.path.getmtime(path))
        except OSError as e:
            logger.warning("Unable to stat %s: %s", rel, e)
            continue
        if db_mtimes.get(ENTITY_PREFIX + rel) != mtime:
            to_index.append((rel, path, mtime, source_type))

    cached = len(in_scope) - len(to_index)
    logger.info(
        "%s total entity files, %s cached, %s to index",
        len(in_scope),
        cached,
        len(to_index),
    )

    start = time.time()

    for i, (rel, path, mtime, source_type) in enumerate(to_index, 1):
        if verbose:
            logger.info("[%s/%s] %s", i, len(to_index), rel)

        conn.execute("DELETE FROM entities WHERE path=?", (rel,))

        if source_type == "identity":
            rows = _extract_entity_identity(journal, rel)
        elif source_type == "relationship":
            rows = _extract_entity_relationship(journal, rel)
        elif source_type == "detected":
            rows = _extract_entity_detected(journal, rel)
        elif source_type == "observation":
            rows = _extract_entity_observations(journal, rel)
        else:
            rows = []

        for row in rows:
            _insert_entity_row(conn, row)

        conn.execute(
            "REPLACE INTO files(path, mtime) VALUES (?, ?)",
            (ENTITY_PREFIX + rel, mtime),
        )

    # Entity files removed from scope (full vs light)
    removed: set[str] = set()
    db_entity_paths = {
        row[0] for row in conn.execute("SELECT DISTINCT path FROM entities").fetchall()
    }

    if full:
        removed = db_entity_paths - set(entity_files)
    else:
        in_scope_db = {
            path
            for path in db_entity_paths
            if not _is_historical_entity_file(path, _entity_source_from_path(path))
        }
        removed = in_scope_db - set(in_scope)

    for rel in removed:
        conn.execute("DELETE FROM entities WHERE path=?", (rel,))
        conn.execute("DELETE FROM files WHERE path=?", (ENTITY_PREFIX + rel,))

    if to_index or removed:
        conn.commit()

    elapsed = time.time() - start
    logger.info(
        "%s entity files indexed, %s entity rows removed in %.2f seconds",
        len(to_index),
        len(removed),
        elapsed,
    )

    return bool(to_index or removed)


def _ts_to_day(ts_value: str | int | None) -> str:
    """Convert a millisecond timestamp to YYYYMMDD string.

    Returns empty string if the value is missing or unparseable.
    """
    if ts_value is None:
        return ""
    try:
        ms = int(ts_value)
        if ms <= 0:
            return ""
        return date.fromtimestamp(ms / 1000).strftime("%Y%m%d")
    except (ValueError, TypeError, OSError):
        return ""


def _index_entity_search_chunks(conn: sqlite3.Connection) -> int:
    """Generate FTS5 search chunks from the entities table.

    Combines identity records (name, type, aka) with relationship records
    (description, tags, facet) to create searchable chunks for each entity.
    One chunk per entity-facet relationship, plus one for identity-only entities.

    Returns the number of entity chunks indexed.
    """
    # Clean up: remove previous entity search chunks and legacy formatter chunks
    conn.execute("DELETE FROM chunks WHERE path LIKE 'entity_search:%'")
    conn.execute("DELETE FROM chunks WHERE path LIKE 'entities/%/entity.json'")

    # Load all non-blocked identity records
    identities: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT entity_id, name, type, aka, created_at, updated_at "
        "FROM entities WHERE source='identity' AND (blocked IS NULL OR blocked=0)"
    ).fetchall():
        entity_id, name, etype, aka, created_at, updated_at = row
        identities[entity_id] = {
            "name": name,
            "type": etype,
            "aka": aka,
            "created_at": created_at,
            "updated_at": updated_at,
        }

    # Load all non-detached relationship records, grouped by entity_id
    relationships: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        "SELECT entity_id, facet, description, tags, last_seen, updated_at, attached_at "
        "FROM entities "
        "WHERE source='relationship' AND (detached IS NULL OR detached=0)"
    ).fetchall():
        entity_id, facet, description, tags, last_seen, updated_at, attached_at = row
        relationships.setdefault(entity_id, []).append(
            {
                "facet": facet,
                "description": description,
                "tags": tags,
                "last_seen": last_seen,
                "updated_at": updated_at,
                "attached_at": attached_at,
            }
        )

    count = 0
    for entity_id, identity in identities.items():
        name = identity["name"] or entity_id.replace("_", " ").title()
        etype = identity["type"] or "Unknown"
        aka_raw = identity["aka"]

        # Build common identity lines (included in every chunk for this entity)
        identity_lines = [f"{name} ({etype})"]
        if aka_raw:
            try:
                aka_list = json.loads(aka_raw)
                if aka_list:
                    identity_lines.append(f"Also known as: {', '.join(aka_list)}")
            except (json.JSONDecodeError, TypeError):
                pass

        path = f"entity_search:{entity_id}"
        rels = relationships.get(entity_id, [])

        if rels:
            # One chunk per facet relationship, enriched with identity data
            for idx, rel in enumerate(rels):
                lines = list(identity_lines)
                if rel["description"]:
                    lines.append(rel["description"])
                if rel["tags"]:
                    try:
                        tags_list = json.loads(rel["tags"])
                        if tags_list:
                            lines.append(f"Tags: {', '.join(tags_list)}")
                    except (json.JSONDecodeError, TypeError):
                        pass

                content = "\n".join(lines)
                facet = (rel["facet"] or "").lower()

                # Best available day: last_seen > updated_at > attached_at
                day = ""
                if rel["last_seen"] and len(rel["last_seen"]) == 8:
                    day = rel["last_seen"]
                else:
                    day = _ts_to_day(rel["updated_at"]) or _ts_to_day(
                        rel["attached_at"]
                    )

                conn.execute(
                    "INSERT INTO chunks(content, path, day, facet, agent, stream, idx, time_bucket) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (content, path, day, facet, "entity", "", idx, ""),
                )
                count += 1
        else:
            # Identity-only entity — one chunk with no facet
            content = "\n".join(identity_lines)
            day = _ts_to_day(identity["updated_at"]) or _ts_to_day(
                identity["created_at"]
            )
            conn.execute(
                "INSERT INTO chunks(content, path, day, facet, agent, stream, idx, time_bucket) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (content, path, day, "", "entity", "", 0, ""),
            )
            count += 1

    conn.commit()
    logger.info("%s entity search chunks indexed", count)
    return count


def scan_journal(journal: str, verbose: bool = False, full: bool = False) -> bool:
    """Scan and index journal content.

    Args:
        journal: Path to journal root directory
        verbose: If True, log detailed progress
        full: If True, scan all files. If False (default), exclude historical
            YYYYMMDD directories (before today) for lighter incremental scans.

    Returns:
        True if any files were indexed or removed
    """
    conn, db_path = get_journal_index(journal)
    files = find_formattable_files(journal)

    # Light mode: exclude historical day directories
    if not full:
        files = {
            rel: path for rel, path in files.items() if not _is_historical_day(rel)
        }

    logger.info("Scanning %s files...", len(files))

    # Get current file mtimes from database
    db_mtimes = {
        path: mtime
        for path, mtime in conn.execute("SELECT path, mtime FROM files")
        if not (path.startswith("entity:") or path.startswith("signal:"))
    }

    to_index = []
    for rel, path in files.items():
        try:
            mtime = int(os.path.getmtime(path))
        except OSError:
            continue
        if db_mtimes.get(rel) != mtime:
            to_index.append((rel, path, mtime))

    cached = len(files) - len(to_index)
    logger.info(
        "%s total files, %s cached, %s to index", len(files), cached, len(to_index)
    )

    start = time.time()

    for i, (rel, path, mtime) in enumerate(to_index, 1):
        if verbose:
            logger.info("[%s/%s] %s", i, len(to_index), rel)

        # Delete existing chunks for this file
        conn.execute("DELETE FROM chunks WHERE path=?", (rel,))

        # Index the file
        stream = _extract_stream(journal, rel)
        _index_file(conn, rel, path, verbose, stream=stream)

        # Update file mtime
        conn.execute("REPLACE INTO files(path, mtime) VALUES (?, ?)", (rel, mtime))

    # Remove files that no longer exist
    # In full mode: remove all missing entries
    # In light mode: only remove entries that would have been scanned (non-historical)
    removed: set[str] = set()
    if full:
        removed = set(db_mtimes) - set(files)
    else:
        # Filter db entries to those in light mode's scan scope, then find missing
        in_scope_db = {rel for rel in db_mtimes if not _is_historical_day(rel)}
        removed = in_scope_db - set(files)

    for rel in removed:
        conn.execute("DELETE FROM chunks WHERE path=?", (rel,))
        conn.execute("DELETE FROM files WHERE path=?", (rel,))

    if to_index or removed:
        conn.commit()

    elapsed = time.time() - start
    logger.info(
        "%s indexed, %s removed in %.2f seconds", len(to_index), len(removed), elapsed
    )

    # Index segment-level concatenated chunks
    affected_segments: set[str] = set()
    for rel, _path, _mtime in to_index:
        parts = rel.replace("\\", "/").split("/")
        if len(parts) >= 4 and segment_key(parts[2]):
            affected_segments.add("/".join(parts[:3]))
    for rel in removed:
        parts = rel.replace("\\", "/").split("/")
        if len(parts) >= 4 and segment_key(parts[2]):
            affected_segments.add("/".join(parts[:3]))

    seg_count = 0
    for rel_segment in sorted(affected_segments):
        segment_dir = str(resolve_journal_path(journal, rel_segment))
        conn.execute("DELETE FROM chunks WHERE path=?", (rel_segment,))
        if os.path.isdir(segment_dir):
            stream = _extract_stream(journal, rel_segment + "/dummy")
            seg_count += _index_segment_chunks(
                conn, segment_dir, rel_segment, stream, verbose
            )

    if affected_segments:
        conn.commit()
        logger.info(
            "%s segment chunks indexed for %s segments",
            seg_count,
            len(affected_segments),
        )

    entity_changed = scan_entities(journal, conn, verbose=verbose, full=full)

    # Regenerate entity search chunks when entity data changes or chunks are missing
    if (
        entity_changed
        or not conn.execute(
            "SELECT 1 FROM chunks WHERE agent='entity' LIMIT 1"
        ).fetchone()
    ):
        _index_entity_search_chunks(conn)

    conn.close()
    return bool(to_index or removed or entity_changed)


# Compiled patterns for temporal extraction (checked against unquoted text only)
_TEMPORAL_PATTERNS: list[tuple[re.Pattern, str]] = []


def _build_temporal_patterns():
    """Build compiled regex patterns for temporal date references.

    Each entry is (compiled_regex, handler_name). Longer patterns are listed
    first so "last monday" is tried before "last week".
    """
    days = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    patterns = [
        # "over the weekend" / "on the weekend"
        (r"\b(?:over|on)\s+the\s+weekend\b", "weekend"),
        # "last monday" through "last sunday"
        (rf"\blast\s+({days})\b", "last_day"),
        # Multi-word phrases
        (r"\blast\s+week\b", "last_week"),
        (r"\bthis\s+week\b", "this_week"),
        (r"\blast\s+month\b", "last_month"),
        (r"\bthis\s+month\b", "this_month"),
        # Single words
        (r"\byesterday\b", "yesterday"),
        (r"\btoday\b", "today"),
    ]
    return [(re.compile(p, re.IGNORECASE), name) for p, name in patterns]


_TEMPORAL_PATTERNS = _build_temporal_patterns()


def extract_temporal_references(
    query: str, reference_date: datetime | None = None
) -> tuple[str, str | None, str | None]:
    """Extract temporal date references from a query string.

    Scans unquoted portions of the query for temporal phrases like "yesterday",
    "last week", "last Monday", etc. Returns the query with the temporal phrase
    removed, plus day_from/day_to as YYYYMMDD strings.

    Only the first temporal match is used. Content inside double quotes is
    never matched.

    Args:
        query: Raw query string.
        reference_date: Pin "today" for testability. Defaults to datetime.now().

    Returns:
        Tuple of (cleaned_query, day_from, day_to). day_from/day_to are None
        if no temporal reference was found.
    """
    if not query:
        return query, None, None

    ref = reference_date or datetime.now()

    # Split into quoted and unquoted segments to protect quoted content.
    # re.split with a capturing group keeps the delimiters in the list.
    segments = re.split(r'("(?:[^"]*)")', query)

    best_match: tuple[int, int, str, re.Match] | None = None

    # Scan unquoted segments only and keep the earliest match in query order.
    for i, seg in enumerate(segments):
        if i % 2 == 1:  # odd indices are quoted segments
            continue
        for pattern, handler in _TEMPORAL_PATTERNS:
            m = pattern.search(seg)
            if not m:
                continue
            candidate = (i, m.start(), handler, m)
            if best_match is None:
                best_match = candidate
                continue
            best_i, best_start, _, _ = best_match
            if i < best_i or (i == best_i and m.start() < best_start):
                best_match = candidate

    if best_match:
        seg_idx, _, handler, match = best_match
        seg = segments[seg_idx]
        # Remove the matched text from this segment
        segments[seg_idx] = seg[: match.start()] + seg[match.end() :]
        cleaned = "".join(segments).strip()
        # Collapse multiple spaces
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        day_from, day_to = _resolve_temporal(handler, match, ref)
        return cleaned, day_from, day_to

    return query, None, None


def _resolve_temporal(
    handler: str, match: re.Match, ref: datetime
) -> tuple[str | None, str | None]:
    """Resolve a temporal handler + match into (day_from, day_to) YYYYMMDD strings."""
    fmt = "%Y%m%d"

    if handler == "yesterday":
        d = ref - timedelta(days=1)
        s = d.strftime(fmt)
        return s, s

    if handler == "today":
        s = ref.strftime(fmt)
        return s, s

    if handler == "last_week":
        # Monday of this week, then back 7 days
        mon_this = ref - timedelta(days=ref.weekday())
        mon_last = mon_this - timedelta(days=7)
        sun_last = mon_last + timedelta(days=6)
        return mon_last.strftime(fmt), sun_last.strftime(fmt)

    if handler == "this_week":
        mon = ref - timedelta(days=ref.weekday())
        sun = mon + timedelta(days=6)
        return mon.strftime(fmt), sun.strftime(fmt)

    if handler == "last_month":
        first_this = ref.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return first_prev.strftime(fmt), last_prev.strftime(fmt)

    if handler == "this_month":
        first = ref.replace(day=1)
        last_day = calendar.monthrange(ref.year, ref.month)[1]
        last = ref.replace(day=last_day)
        return first.strftime(fmt), last.strftime(fmt)

    if handler == "last_day":
        # "last monday" etc. — match group 1 is the day name
        day_name = match.group(1).lower()
        day_map = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }
        target = day_map[day_name]
        days_back = (ref.weekday() - target) % 7
        if days_back == 0:
            days_back = 7  # "last Monday" on a Monday = a week ago
        d = ref - timedelta(days=days_back)
        s = d.strftime(fmt)
        return s, s

    if handler == "weekend":
        # Most recent Saturday-Sunday
        weekday = ref.weekday()
        if weekday >= 5:  # Sat=5, Sun=6
            sat = ref - timedelta(days=(weekday - 5))
        else:
            sat = ref - timedelta(days=(weekday + 2))
        sun = sat + timedelta(days=1)
        return sat.strftime(fmt), sun.strftime(fmt)

    return None, None  # unreachable


def sanitize_fts_query(
    query: str, reference_date: datetime | None = None
) -> tuple[str, str | None, str | None]:
    """Sanitize query for FTS5 and extract temporal date references.

    Extracts temporal phrases (yesterday, last week, etc.) from the query,
    then sanitizes the remaining text for FTS5: keeps alphanumeric, spaces,
    quotes, apostrophes, and *.

    For plain multi-word queries (no explicit operators or quotes), produces
    a NEAR-proximity formulation with AND fallback:
        NEAR(term1 term2, 10) OR (term1 AND term2)

    Returns:
        Tuple of (sanitized_query, day_from, day_to) where day_from/day_to
        are YYYYMMDD strings or None.
    """
    # Extract temporal references before sanitization
    query, day_from, day_to = extract_temporal_references(query, reference_date)

    result = re.sub(r"[^a-zA-Z0-9\s\"'*]", " ", query)
    # Remove all quotes if unbalanced
    if result.count('"') % 2:
        result = result.replace('"', "")
    # NEAR formulation for plain multi-word queries
    words = result.split()
    has_operators = any(word in ("AND", "OR", "NOT") for word in words)
    has_quotes = '"' in result
    if len(words) > 1 and not has_operators and not has_quotes:
        near_terms = " ".join(words)
        and_terms = " AND ".join(words)
        result = f"NEAR({near_terms}, 10) OR ({and_terms})"
    return result, day_from, day_to


def _build_where_clause(
    query: str,
    day: str | None = None,
    day_from: str | None = None,
    day_to: str | None = None,
    facet: str | None = None,
    agent: str | None = None,
    stream: str | None = None,
    time_bucket: str | None = None,
) -> tuple[str, list[Any]]:
    """Build WHERE clause and params for FTS5 search.

    Args:
        query: FTS5 search query
        day: Filter by exact day (YYYYMMDD) - mutually exclusive with day_from/day_to
        day_from: Filter by date range start (YYYYMMDD, inclusive)
        day_to: Filter by date range end (YYYYMMDD, inclusive)
        facet: Filter by facet name
        agent: Filter by agent
        stream: Filter by stream name
        time_bucket: Filter by time of day bucket (morning, afternoon, evening, night)

    Returns:
        Tuple of (where_clause, params)
    """
    params: list[Any] = []

    extracted_from: str | None = None
    extracted_to: str | None = None

    if query:
        sanitized, extracted_from, extracted_to = sanitize_fts_query(query)
        if sanitized:
            where_clause = f"chunks MATCH '{sanitized}'"
        else:
            where_clause = "1=1"
    else:
        where_clause = "1=1"

    if day:
        where_clause += " AND day=?"
        params.append(day)
    elif day_from or day_to:
        if day_from:
            where_clause += " AND day>=?"
            params.append(day_from)
        if day_to:
            where_clause += " AND day<=?"
            params.append(day_to)
    elif extracted_from or extracted_to:
        if extracted_from:
            where_clause += " AND day>=?"
            params.append(extracted_from)
        if extracted_to:
            where_clause += " AND day<=?"
            params.append(extracted_to)
    if facet:
        where_clause += " AND facet=?"
        params.append(facet.lower())
    if agent:
        where_clause += " AND agent=?"
        params.append(agent.lower())
    if stream:
        where_clause += " AND stream=?"
        params.append(stream)
    if time_bucket:
        where_clause += " AND time_bucket=?"
        params.append(time_bucket)

    return where_clause, params


def search_journal(
    query: str,
    limit: int = 10,
    offset: int = 0,
    *,
    day: str | None = None,
    day_from: str | None = None,
    day_to: str | None = None,
    facet: str | None = None,
    agent: str | None = None,
    stream: str | None = None,
    time_bucket: str | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Search the journal index.

    Args:
        query: FTS5 search query. Words are AND'd by default; use OR to match any,
            quotes for exact phrases, * for prefix match. Empty string returns all.
        limit: Maximum results to return
        offset: Number of results to skip for pagination
        day: Filter by exact day (YYYYMMDD) - mutually exclusive with day_from/day_to
        day_from: Filter by date range start (YYYYMMDD, inclusive)
        day_to: Filter by date range end (YYYYMMDD, inclusive)
        facet: Filter by facet name
        agent: Filter by agent (e.g., "flow", "event", "news")
        stream: Filter by stream name
        time_bucket: Filter by time of day (morning, afternoon, evening, night)

    Returns:
        Tuple of (total_count, results) where each result has:
            - id: "{path}:{idx}"
            - text: The matched markdown chunk
            - metadata: {day, facet, agent, stream, path, idx}
            - score: BM25 relevance score
    """
    conn, _ = get_journal_index()
    where_clause, params = _build_where_clause(
        query, day, day_from, day_to, facet, agent, stream, time_bucket
    )

    # Get total count
    total = conn.execute(
        f"SELECT count(*) FROM chunks WHERE {where_clause}", params
    ).fetchone()[0]

    # Get results
    cursor = conn.execute(
        f"""
        SELECT content, path, day, facet, agent, stream, idx, bm25(chunks) as rank
        FROM chunks WHERE {where_clause}
        ORDER BY rank LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    )

    results = []
    for (
        content,
        path,
        day_val,
        facet_val,
        agent_val,
        stream_val,
        idx,
        rank,
    ) in cursor.fetchall():
        results.append(
            {
                "id": f"{path}:{idx}",
                "text": content,
                "metadata": {
                    "day": day_val,
                    "facet": facet_val,
                    "agent": agent_val,
                    "stream": stream_val,
                    "path": path,
                    "idx": idx,
                },
                "score": rank,
            }
        )

    conn.close()
    return total, results


def search_counts(
    query: str,
    *,
    day: str | None = None,
    day_from: str | None = None,
    day_to: str | None = None,
    facet: str | None = None,
    agent: str | None = None,
    stream: str | None = None,
    time_bucket: str | None = None,
) -> dict[str, Any]:
    """Get aggregated counts for a search query.

    Uses single query + Python aggregation for efficiency.

    Args:
        query: FTS5 search query (empty string for all)
        day: Filter by exact day (YYYYMMDD) - mutually exclusive with day_from/day_to
        day_from: Filter by date range start (YYYYMMDD, inclusive)
        day_to: Filter by date range end (YYYYMMDD, inclusive)
        facet: Filter by facet name
        agent: Filter by agent
        stream: Filter by stream name
        time_bucket: Filter by time of day (morning, afternoon, evening, night)

    Returns:
        Dict with:
            - total: Total matching chunks
            - facets: Counter of facet_name -> count
            - agents: Counter of agent_name -> count
            - days: Counter of day -> count
            - streams: Counter of stream_name -> count
    """
    from collections import Counter

    conn, _ = get_journal_index()
    where_clause, params = _build_where_clause(
        query, day, day_from, day_to, facet, agent, stream, time_bucket
    )

    rows = conn.execute(
        f"SELECT facet, agent, day, stream FROM chunks WHERE {where_clause}", params
    ).fetchall()

    conn.close()

    return {
        "total": len(rows),
        "facets": Counter(r[0] for r in rows if r[0]),
        "agents": Counter(r[1] for r in rows if r[1]),
        "days": Counter(r[2] for r in rows if r[2]),
        "streams": Counter(r[3] for r in rows if r[3]),
    }


def _load_index_entity_dicts(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Load identity entities from the journal index as entity dicts.

    Returns dicts with "id", "name", and "aka" suitable for
    build_name_resolution_map().
    """
    rows = conn.execute(
        "SELECT entity_id, name, aka FROM entities WHERE source='identity'"
    ).fetchall()
    entity_dicts: list[dict[str, Any]] = []
    for entity_id, name, aka_str in rows:
        d: dict[str, Any] = {"id": entity_id, "name": name, "aka": []}
        if aka_str:
            try:
                aka_list = json.loads(aka_str)
                if isinstance(aka_list, list):
                    d["aka"] = aka_list
            except (ValueError, TypeError):
                pass
        entity_dicts.append(d)
    return entity_dicts


def _build_entity_name_map(
    conn: sqlite3.Connection,
    names: list[str] | set[str],
) -> dict[str, str]:
    """Map entity names to entity_ids via shared name resolution.

    Returns dict mapping entity_name -> entity_id. Uses the same tiered
    matching as all other name resolution call sites.
    """
    from solstone.think.entities.matching import build_name_resolution_map

    entity_dicts = _load_index_entity_dicts(conn)
    return build_name_resolution_map(
        sorted({name for name in names if name}), entity_dicts
    )


def _extract_match_candidates(fts_results: list[dict[str, Any]]) -> set[str]:
    """Extract candidate entity names from FTS result text."""
    names: set[str] = set()
    for result in fts_results:
        text = result.get("text", "")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("### "):
                name = stripped[4:].strip()
                if name.startswith("Project: "):
                    names.add(name[len("Project: ") :].strip())
                elif name.startswith("Person: "):
                    names.add(name[len("Person: ") :].strip())
                elif name:
                    names.add(name)
    return names


def search_entities(
    query: str | None = None,
    entity_type: str | None = None,
    facet: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search entities by text query, type, facet, and/or detected activity."""
    conn, _ = get_journal_index()
    try:
        id_where_parts = ["source='identity'"]
        id_params: list[Any] = []
        if entity_type:
            id_where_parts.append("LOWER(type)=LOWER(?)")
            id_params.append(entity_type)

        id_rows = conn.execute(
            f"SELECT entity_id, name, type, description FROM entities WHERE {' AND '.join(id_where_parts)}",
            id_params,
        ).fetchall()

        entities: dict[str, dict[str, Any]] = {
            r[0]: {
                "entity_id": r[0],
                "name": r[1],
                "type": r[2],
                "description": r[3] or "",
            }
            for r in id_rows
        }

        relationship_ids: set[str] | None = None
        if facet:
            rel_rows = conn.execute(
                "SELECT DISTINCT entity_id FROM entities WHERE source='relationship' AND facet=?",
                [facet.lower()],
            ).fetchall()
            relationship_ids = {eid for (eid,) in rel_rows}

        if since:
            from solstone.think.entities.activity import (
                iter_detected_entity_names_since,
            )

            detected_names = [
                name for name, _facet, _day in iter_detected_entity_names_since(since)
            ]
            name_map = _build_entity_name_map(conn, detected_names)
            active_ids = set(name_map.values())
            if relationship_ids is not None:
                active_ids &= relationship_ids
            entities = {eid: e for eid, e in entities.items() if eid in active_ids}
        elif relationship_ids is not None:
            entities = {
                eid: e for eid, e in entities.items() if eid in relationship_ids
            }

        if query:
            _, fts_results = search_journal(query, limit=100, agent="entity:detected")
            fts_ids = set()
            for r in fts_results:
                path = r.get("metadata", {}).get("path", "")
                parts = path.split("/")
                if "entities" in parts:
                    idx = parts.index("entities")
                    if idx + 1 < len(parts):
                        candidate = parts[idx + 1]
                        if candidate and "." not in candidate:
                            fts_ids.add(candidate)

            if not fts_ids:
                match_names = _extract_match_candidates(fts_results)
                name_map = _build_entity_name_map(conn, match_names)
                fts_ids.update(name_map.values())

            if not fts_ids:
                like_term = f"%{query.lower()}%"
                identity_rows = conn.execute(
                    """
                    SELECT DISTINCT entity_id
                    FROM entities
                    WHERE source='identity'
                      AND (
                        LOWER(name) LIKE ?
                        OR LOWER(type) LIKE ?
                        OR LOWER(description) LIKE ?
                      )
                    """,
                    [like_term, like_term, like_term],
                ).fetchall()
                for (eid,) in identity_rows:
                    fts_ids.add(eid)

            entities = {eid: e for eid, e in entities.items() if eid in fts_ids}

        result_list = []
        for eid, e in entities.items():
            e["facets"] = []

            rel_facets = conn.execute(
                "SELECT DISTINCT facet FROM entities WHERE entity_id=? AND source='relationship' AND facet IS NOT NULL AND facet != ''",
                [eid],
            ).fetchall()
            for (rf,) in rel_facets:
                if rf and rf not in e["facets"]:
                    e["facets"].append(rf)

            result_list.append(e)

        result_list.sort(key=lambda x: (str(x["name"]).lower(), str(x["name"])))
        return result_list[:limit]
    finally:
        conn.close()
