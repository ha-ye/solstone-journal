# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Entity detection and observation-relation edge extraction."""

from __future__ import annotations

from datetime import datetime
from itertools import combinations
from typing import Any

from solstone.think.edge_sources import EdgeContext


def _observation_day(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        return None
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return None
    return value


def extract_observation_edges(entries: list[dict], ctx: EdgeContext) -> list[dict]:
    """Extract explicit relation edges from entity observations."""
    parts = ctx.path.replace("\\", "/").split("/")
    if len(parts) < 5:
        raise ValueError(f"invalid observations path: {ctx.path}")
    source_id = parts[3]

    rows: list[dict[str, Any]] = []
    for observation in entries:
        if not isinstance(observation, dict):
            continue
        relation = observation.get("relation")
        if not isinstance(relation, dict):
            continue

        target_id = relation.get("target_entity_id")
        if not target_id:
            ctx.drop()
            continue
        if target_id == source_id:
            continue

        observed_at = observation.get("observed_at")
        anchor = str(observed_at) if observed_at is not None else None
        rows.append(
            {
                "src": source_id,
                "dst": target_id,
                "kind": relation["kind"],
                "src_name": None,
                "dst_name": relation.get("target_name"),
                "day": _observation_day(observation.get("source_day")),
                "facet": ctx.facet,
                "source": "observation",
                "path": ctx.path,
                "anchor": anchor,
                "label": relation.get("note"),
                "ts": observed_at,
                "weight": 1,
            }
        )

    return rows


def extract_copresence_edges(entries: list[dict], ctx: EdgeContext) -> list[dict]:
    """Extract co-present edges from detected entity segment overlap."""
    resolved: list[dict[str, Any]] = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        segments = entry.get("segments")
        if not isinstance(segments, list):
            continue
        segment_ids = {
            segment
            for segment in segments
            if isinstance(segment, str) and segment.strip()
        }
        if not segment_ids:
            continue

        entity_id = ctx.resolve(name)
        if entity_id is None:
            continue

        resolved.append(
            {"entity_id": entity_id, "name": name.strip(), "segments": segment_ids}
        )

    rows: list[dict[str, Any]] = []
    for left, right in combinations(resolved, 2):
        left_id = left["entity_id"]
        right_id = right["entity_id"]
        if left_id == right_id:
            continue

        shared = sorted(left["segments"] & right["segments"])
        if not shared:
            continue

        rows.append(
            {
                "src": left_id,
                "dst": right_id,
                "kind": "co-present",
                "src_name": left["name"],
                "dst_name": right["name"],
                "day": ctx.day,
                "facet": ctx.facet,
                "source": "co-presence",
                "path": ctx.path,
                "anchor": shared[0],
                "label": "",
                "ts": 0,
                "weight": len(shared),
            }
        )

    return rows
