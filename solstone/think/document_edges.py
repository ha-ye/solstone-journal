# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Derived edge extraction from talent document summaries."""

from __future__ import annotations

from itertools import combinations
from typing import Any

from solstone.think.edge_sources import EdgeContext, segment_ref
from solstone.think.utils import segment_start_ts_ms


def extract_document_edges(payload: dict[str, Any], ctx: EdgeContext) -> list[dict]:
    """Extract party-of edges from a parsed talents/documents.json payload."""
    if not isinstance(payload, dict):
        raise ValueError(f"documents payload must be a JSON object: {ctx.path}")

    parties = payload.get("parties")
    if not isinstance(parties, list):
        return []

    anchor, segment_key = segment_ref(ctx.path)
    ts = segment_start_ts_ms(ctx.day, segment_key)
    resolved: dict[str, str] = {}
    for party in parties:
        if not isinstance(party, dict):
            continue
        name = party.get("name")
        entity_id = ctx.resolve(name if isinstance(name, str) else "")
        if entity_id is None:
            continue
        resolved.setdefault(entity_id, name.strip())

    rows: list[dict[str, Any]] = []
    for left_id, right_id in combinations(sorted(resolved), 2):
        rows.append(
            {
                "src": left_id,
                "dst": right_id,
                "kind": "party-of",
                "src_name": resolved[left_id],
                "dst_name": resolved[right_id],
                "day": ctx.day,
                "facet": ctx.facet,
                "source": "document",
                "path": ctx.path,
                "anchor": anchor,
                "label": "",
                "ts": ts,
                "weight": 1,
            }
        )

    return rows
