# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Entity detection edge extraction."""

from __future__ import annotations

from itertools import combinations
from typing import Any

from solstone.think.edge_sources import EdgeContext


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
