# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Derived entity-to-entity edge index maintenance."""

from __future__ import annotations

import logging
import math
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
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
# Shared directional storage contract; the parallel speaker/mention edge-source
# lode relies on these kinds being stored without endpoint normalization.
DIRECTED_KINDS = frozenset({"committed-to", "mentioned"})
KIND_WEIGHTS = {
    "committed-to": 5,
    "spoke-with": 4,
    "mentioned": 3,
    "attended-with": 3,
    "co-present": 1,
}
"""Relationship-strength weights; commitments outweigh passive co-presence."""
HALF_LIFE_DAYS = 90.0
"""Edge score half-life in days; 90-day-old evidence contributes half weight."""

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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, kind, day)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, kind, day)")

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


def _open_edges_reader() -> sqlite3.Connection:
    """Open the edge index read-only at the SQLite driver layer."""
    # Local imports avoid adding cycles while indexer.journal imports this module.
    from solstone.think.indexer.journal import DB_NAME, INDEX_DIR
    from solstone.think.utils import get_journal

    journal_path = get_journal()
    db_path = os.path.join(journal_path, INDEX_DIR, DB_NAME)
    if not os.path.exists(db_path):
        raise FileNotFoundError(
            f"Edge index database not found at {db_path}; run "
            "`journal indexer --rescan` to build it."
        )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _normalize_kinds(kinds: Sequence[str] | None) -> list[str] | None:
    if kinds is None:
        return None
    normalized = [kinds] if isinstance(kinds, str) else list(kinds)
    for kind in normalized:
        if kind not in KINDS:
            raise ValueError(f"Unknown edge kind: {kind!r}")
    return normalized


def _normalize_facet(facet: str | None) -> str | None:
    return facet.lower() if isinstance(facet, str) and facet else facet


def _filter_payload(
    *,
    kinds: Sequence[str] | None,
    facet: str | None,
    day_from: str | None,
    day_to: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    normalized_kinds = _normalize_kinds(kinds)
    normalized_facet = _normalize_facet(facet)
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if normalized_kinds is not None:
        if not normalized_kinds:
            clauses.append("0 = 1")
        else:
            placeholders = []
            for idx, kind in enumerate(normalized_kinds):
                key = f"kind{idx}"
                placeholders.append(f":{key}")
                params[key] = kind
            clauses.append(f"kind IN ({', '.join(placeholders)})")
    if normalized_facet is not None:
        clauses.append("facet = :facet")
        params["facet"] = normalized_facet
    if day_from is not None:
        clauses.append("day >= :day_from")
        params["day_from"] = day_from
    if day_to is not None:
        clauses.append("day <= :day_to")
        params["day_to"] = day_to

    sql = "".join(f"\n  AND {clause}" for clause in clauses)
    filters = {
        "kinds": normalized_kinds,
        "facet": normalized_facet,
        "day_from": day_from,
        "day_to": day_to,
    }
    return sql, params, filters


def _validate_limit(name: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{name} must be >= 0")


def _reference_day(reference_day: str | None) -> str:
    return reference_day or datetime.now().strftime("%Y%m%d")


def _parse_day(day: str) -> date:
    return datetime.strptime(day, "%Y%m%d").date()


def _decay_factor(day: str | None, reference: date) -> float:
    if day is None:
        return 1.0
    age_days = max(0, (reference - _parse_day(day)).days)
    return math.exp(-age_days * math.log(2) / HALF_LIFE_DAYS)


def _empty_kind() -> dict[str, Any]:
    return {"count": 0, "weighted": 0.0}


def _evidence_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "src": row["src"],
        "dst": row["dst"],
        "kind": row["kind"],
        "directed": bool(row["directed"]),
        "src_name": row["src_name"],
        "dst_name": row["dst_name"],
        "day": row["day"],
        "facet": row["facet"],
        "source": row["source"],
        "path": row["path"],
        "anchor": row["anchor"],
        "label": row["label"],
        "ts": row["ts"],
        "weight": row["weight"],
    }


def _pair_where() -> str:
    return """
WHERE ((src = :entity_id AND dst = :peer_id)
   OR  (src = :peer_id AND dst = :entity_id))
"""


def _evidence_order() -> str:
    return """
ORDER BY day IS NULL ASC, day DESC,
         ts IS NULL ASC, ts DESC,
         path ASC, anchor IS NULL ASC, anchor ASC, rowid ASC
"""


def _load_evidence_rows(
    conn: sqlite3.Connection,
    entity_id: str,
    peer_id: str,
    *,
    filters_sql: str,
    filter_params: dict[str, Any],
    limit: int,
    offset: int = 0,
) -> list[dict[str, Any]]:
    params = {
        "entity_id": entity_id,
        "peer_id": peer_id,
        "limit": limit,
        "offset": offset,
        **filter_params,
    }
    rows = conn.execute(
        f"""
SELECT src, dst, kind, directed, src_name, dst_name, day, facet,
       source, path, anchor, label, ts, weight
FROM edges
{_pair_where()}
  {filters_sql}
{_evidence_order()}
LIMIT :limit OFFSET :offset
        """,
        params,
    ).fetchall()
    return [_evidence_dict(row) for row in rows]


def _load_peer_name(
    conn: sqlite3.Connection,
    entity_id: str,
    peer_id: str,
    *,
    filters_sql: str,
    filter_params: dict[str, Any],
) -> str | None:
    params = {"entity_id": entity_id, "peer_id": peer_id, **filter_params}
    row = conn.execute(
        f"""
SELECT CASE WHEN src = :entity_id THEN dst_name ELSE src_name END AS peer_name
FROM edges
{_pair_where()}
  {filters_sql}
  AND (CASE WHEN src = :entity_id THEN dst_name ELSE src_name END) IS NOT NULL
{_evidence_order()}
LIMIT 1
        """,
        params,
    ).fetchone()
    return str(row["peer_name"]) if row and row["peer_name"] is not None else None


def _load_peer_names(
    conn: sqlite3.Connection,
    entity_id: str,
    *,
    filters_sql: str,
    filter_params: dict[str, Any],
) -> dict[str, str]:
    rows = conn.execute(
        f"""
SELECT
  CASE WHEN src = :entity_id THEN dst ELSE src END AS peer,
  CASE WHEN src = :entity_id THEN dst_name ELSE src_name END AS peer_name,
  day,
  ts,
  path,
  anchor,
  rowid
FROM edges
WHERE (src = :entity_id OR dst = :entity_id)
  AND (CASE WHEN src = :entity_id THEN dst ELSE src END) != :entity_id
  {filters_sql}
  AND (CASE WHEN src = :entity_id THEN dst_name ELSE src_name END) IS NOT NULL
ORDER BY peer ASC, day IS NULL ASC, day DESC,
         ts IS NULL ASC, ts DESC,
         path ASC, anchor IS NULL ASC, anchor ASC, rowid ASC
        """,
        {"entity_id": entity_id, **filter_params},
    ).fetchall()
    names: dict[str, str] = {}
    for row in rows:
        peer = str(row["peer"])
        if peer not in names:
            names[peer] = str(row["peer_name"])
    return names


def _load_endpoint_names(
    conn: sqlite3.Connection,
    *,
    filters_sql: str,
    filter_params: dict[str, Any],
) -> dict[str, str]:
    rows = conn.execute(
        f"""
WITH endpoint_edges AS (
  SELECT src AS entity_id, src_name AS entity_name, day, ts, path, anchor,
         rowid AS edge_rowid
  FROM edges
  WHERE 1 = 1
  {filters_sql}
  UNION ALL
  SELECT dst AS entity_id, dst_name AS entity_name, day, ts, path, anchor,
         rowid AS edge_rowid
  FROM edges
  WHERE 1 = 1
    AND dst != src
  {filters_sql}
)
SELECT entity_id, entity_name
FROM endpoint_edges
WHERE entity_name IS NOT NULL
ORDER BY entity_id ASC, day IS NULL ASC, day DESC,
         ts IS NULL ASC, ts DESC,
         path ASC, anchor IS NULL ASC, anchor ASC, edge_rowid ASC
        """,
        filter_params,
    ).fetchall()
    names: dict[str, str] = {}
    for row in rows:
        entity_id = str(row["entity_id"])
        if entity_id not in names:
            names[entity_id] = str(row["entity_name"])
    return names


def _kind_weight(
    kind: str, weight_sum: int | float, day: str | None, reference: date
) -> float:
    return KIND_WEIGHTS[kind] * float(weight_sum) * _decay_factor(day, reference)


def load_entity_network(
    entity_id: str,
    *,
    kinds: Sequence[str] | None = None,
    facet: str | None = None,
    day_from: str | None = None,
    day_to: str | None = None,
    include_principal: bool = False,
    limit: int = 25,
    evidence_limit: int = 5,
    reference_day: str | None = None,
) -> dict[str, Any]:
    """Load one-hop edge neighbors for an entity without mutating the index."""
    _validate_limit("limit", limit)
    _validate_limit("evidence_limit", evidence_limit)
    filters_sql, filter_params, filters = _filter_payload(
        kinds=kinds,
        facet=facet,
        day_from=day_from,
        day_to=day_to,
    )
    ref_day = _reference_day(reference_day)
    reference = _parse_day(ref_day)

    # Local import avoids adding an entities.journal dependency to indexer import.
    from solstone.think.entities.journal import get_journal_principal

    principal = get_journal_principal()
    principal_id = principal.get("id") if principal else None

    conn = _open_edges_reader()
    try:
        rows = conn.execute(
            f"""
SELECT
  CASE WHEN src = :entity_id THEN dst ELSE src END AS peer,
  kind,
  day,
  COUNT(*) AS count,
  SUM(weight) AS weight_sum,
  SUM(CASE WHEN directed = 1 AND src = :entity_id THEN 1 ELSE 0 END) AS directed_out,
  SUM(CASE WHEN directed = 1 AND dst = :entity_id THEN 1 ELSE 0 END) AS directed_in
FROM edges
WHERE (src = :entity_id OR dst = :entity_id)
  AND (CASE WHEN src = :entity_id THEN dst ELSE src END) != :entity_id
  {filters_sql}
GROUP BY peer, kind, day
            """,
            {"entity_id": entity_id, **filter_params},
        ).fetchall()
        names = _load_peer_names(
            conn,
            entity_id,
            filters_sql=filters_sql,
            filter_params=filter_params,
        )

        neighbors: dict[str, dict[str, Any]] = {}
        for row in rows:
            peer = str(row["peer"])
            if (
                not include_principal
                and principal_id
                and entity_id != principal_id
                and peer == principal_id
            ):
                continue

            neighbor = neighbors.setdefault(
                peer,
                {
                    "entity_id": peer,
                    "name": names.get(peer),
                    "score": 0.0,
                    "count": 0,
                    "first_seen": None,
                    "last_seen": None,
                    "directed": {"out": 0, "in": 0},
                    "kinds": {},
                    "evidence": [],
                },
            )
            kind = str(row["kind"])
            kind_info = neighbor["kinds"].setdefault(kind, _empty_kind())
            count = int(row["count"] or 0)
            weighted = _kind_weight(kind, row["weight_sum"] or 0, row["day"], reference)
            kind_info["count"] += count
            kind_info["weighted"] += weighted
            neighbor["count"] += count
            neighbor["score"] += weighted
            neighbor["directed"]["out"] += int(row["directed_out"] or 0)
            neighbor["directed"]["in"] += int(row["directed_in"] or 0)

            day = row["day"]
            if day is not None:
                if neighbor["first_seen"] is None or day < neighbor["first_seen"]:
                    neighbor["first_seen"] = day
                if neighbor["last_seen"] is None or day > neighbor["last_seen"]:
                    neighbor["last_seen"] = day

        ordered = sorted(
            neighbors.values(),
            key=lambda item: (-item["score"], item["entity_id"]),
        )
        limited = ordered[:limit]
        for neighbor in limited:
            neighbor["evidence"] = _load_evidence_rows(
                conn,
                entity_id,
                neighbor["entity_id"],
                filters_sql=filters_sql,
                filter_params=filter_params,
                limit=evidence_limit,
            )

        return {
            "entity_id": entity_id,
            "reference_day": ref_day,
            "filters": {**filters, "include_principal": include_principal},
            "limit": limit,
            "evidence_limit": evidence_limit,
            "total_neighbors": len(ordered),
            "neighbors": limited,
        }
    finally:
        conn.close()


def load_edge_evidence(
    entity_id: str,
    peer_id: str,
    *,
    kinds: Sequence[str] | None = None,
    facet: str | None = None,
    day_from: str | None = None,
    day_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Load stable newest-first evidence rows for one entity pair."""
    _validate_limit("limit", limit)
    _validate_limit("offset", offset)
    filters_sql, filter_params, filters = _filter_payload(
        kinds=kinds,
        facet=facet,
        day_from=day_from,
        day_to=day_to,
    )
    conn = _open_edges_reader()
    try:
        params = {"entity_id": entity_id, "peer_id": peer_id, **filter_params}
        total = int(
            conn.execute(
                f"""
SELECT COUNT(*) AS total
FROM edges
{_pair_where()}
  {filters_sql}
                """,
                params,
            ).fetchone()["total"]
        )
        return {
            "entity_id": entity_id,
            "peer_id": peer_id,
            "peer_name": _load_peer_name(
                conn,
                entity_id,
                peer_id,
                filters_sql=filters_sql,
                filter_params=filter_params,
            ),
            "filters": filters,
            "total": total,
            "limit": limit,
            "offset": offset,
            "evidence": _load_evidence_rows(
                conn,
                entity_id,
                peer_id,
                filters_sql=filters_sql,
                filter_params=filter_params,
                limit=limit,
                offset=offset,
            ),
        }
    finally:
        conn.close()


def load_network_overview(
    *,
    kinds: Sequence[str] | None = None,
    facet: str | None = None,
    day_from: str | None = None,
    day_to: str | None = None,
    limit: int = 25,
    reference_day: str | None = None,
) -> dict[str, Any]:
    """Load a modest global one-hop edge snapshot."""
    _validate_limit("limit", limit)
    filters_sql, filter_params, filters = _filter_payload(
        kinds=kinds,
        facet=facet,
        day_from=day_from,
        day_to=day_to,
    )
    ref_day = _reference_day(reference_day)
    reference = _parse_day(ref_day)
    conn = _open_edges_reader()
    try:
        total_edges = int(
            conn.execute(
                f"""
SELECT COUNT(*) AS total
FROM edges
WHERE 1 = 1
  {filters_sql}
                """,
                filter_params,
            ).fetchone()["total"]
        )
        kind_rows = conn.execute(
            f"""
SELECT kind, day, COUNT(*) AS count, SUM(weight) AS weight_sum
FROM edges
WHERE 1 = 1
  {filters_sql}
GROUP BY kind, day
            """,
            filter_params,
        ).fetchall()
        global_kinds: dict[str, dict[str, Any]] = {}
        for row in kind_rows:
            kind = str(row["kind"])
            kind_info = global_kinds.setdefault(kind, _empty_kind())
            kind_info["count"] += int(row["count"] or 0)
            kind_info["weighted"] += _kind_weight(
                kind,
                row["weight_sum"] or 0,
                row["day"],
                reference,
            )

        entity_rows = conn.execute(
            f"""
WITH endpoint_edges AS (
  SELECT src AS entity_id, kind, day, weight
  FROM edges
  WHERE 1 = 1
  {filters_sql}
  UNION ALL
  SELECT dst AS entity_id, kind, day, weight
  FROM edges
  WHERE 1 = 1
    AND dst != src
  {filters_sql}
)
SELECT entity_id, kind, day, COUNT(*) AS count, SUM(weight) AS weight_sum
FROM endpoint_edges
GROUP BY entity_id, kind, day
            """,
            filter_params,
        ).fetchall()
        names = _load_endpoint_names(
            conn,
            filters_sql=filters_sql,
            filter_params=filter_params,
        )
        entities: dict[str, dict[str, Any]] = {}
        for row in entity_rows:
            entity_id = str(row["entity_id"])
            entity = entities.setdefault(
                entity_id,
                {
                    "entity_id": entity_id,
                    "name": names.get(entity_id),
                    "score": 0.0,
                    "count": 0,
                    "first_seen": None,
                    "last_seen": None,
                    "kinds": {},
                },
            )
            kind = str(row["kind"])
            kind_info = entity["kinds"].setdefault(kind, _empty_kind())
            count = int(row["count"] or 0)
            weighted = _kind_weight(kind, row["weight_sum"] or 0, row["day"], reference)
            kind_info["count"] += count
            kind_info["weighted"] += weighted
            entity["count"] += count
            entity["score"] += weighted
            day = row["day"]
            if day is not None:
                if entity["first_seen"] is None or day < entity["first_seen"]:
                    entity["first_seen"] = day
                if entity["last_seen"] is None or day > entity["last_seen"]:
                    entity["last_seen"] = day

        ordered_entities = sorted(
            entities.values(),
            key=lambda item: (-item["score"], item["entity_id"]),
        )
        return {
            "reference_day": ref_day,
            "filters": filters,
            "limit": limit,
            "totals": {
                "edges": total_edges,
                "entities": len(ordered_entities),
            },
            "kinds": global_kinds,
            "entities": ordered_entities[:limit],
        }
    finally:
        conn.close()


def count_entity_edges(entity_id: str) -> int:
    """Count edge rows touching one entity without mutating the index."""
    conn = _open_edges_reader()
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM edges WHERE src = ? OR dst = ?",
                (entity_id, entity_id),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def fold_entity_edges(
    source_id: str,
    target_id: str,
) -> dict[str, int]:
    """Fold derived edge endpoints from a merged source entity to its target."""
    from solstone.think.indexer.journal import get_journal_index

    conn, _ = get_journal_index(None)
    try:
        rows_folded = int(
            conn.execute(
                "SELECT COUNT(*) FROM edges WHERE src = ? OR dst = ?",
                (source_id, source_id),
            ).fetchone()[0]
        )
        conn.execute("UPDATE edges SET src = ? WHERE src = ?", (target_id, source_id))
        conn.execute("UPDATE edges SET dst = ? WHERE dst = ?", (target_id, source_id))
        self_edges_dropped = int(
            conn.execute(
                "SELECT COUNT(*) FROM edges WHERE src = dst AND src = ?",
                (target_id,),
            ).fetchone()[0]
        )
        conn.execute("DELETE FROM edges WHERE src = dst AND src = ?", (target_id,))
        # Names are historical evidence labels. Keep them as captured; only swap
        # during re-normalization so each label stays aligned with its id column.
        conn.execute(
            """
            UPDATE edges
            SET src = dst, dst = src, src_name = dst_name, dst_name = src_name
            WHERE directed = 0
              AND src > dst
              AND (src = ? OR dst = ?)
            """,
            (target_id, target_id),
        )
        conn.commit()
        return {
            "rows_folded": rows_folded,
            "self_edges_dropped": self_edges_dropped,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
