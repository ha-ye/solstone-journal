# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Connections card context for the Pulse home dashboard."""

from __future__ import annotations

import logging
from typing import Any

from solstone.apps.entities.copy import ENT_CONN_KIND_CHIP_WORDS, ENT_CONN_KIND_WORDS
from solstone.think.entities.journal import get_journal_principal
from solstone.think.indexer.edges import ATTENDANCE_KINDS, load_entity_network

logger = logging.getLogger(__name__)

# Connections-card chip vocabulary contract: narrower than the entities kind map
# because it omits attendance-only words; values are projected from entities copy.
CONNECTION_KIND_KEYS: tuple[str, ...] = (
    "works-with",
    "works-at",
    "reports-to",
    "family-of",
    "knows",
    "uses",
    "created",
    "decided-with",
    "committed-to",
    "spoke-with",
    "mentioned",
    "messaged-with",
    "scheduled-with",
    "party-of",
    "other",
)


def _kind_words() -> dict[str, str]:
    composed = {**ENT_CONN_KIND_WORDS, **ENT_CONN_KIND_CHIP_WORDS}
    return {key: composed[key] for key in CONNECTION_KIND_KEYS}


def _trim_kinds(kinds: dict[str, Any]) -> list[dict[str, Any]]:
    trimmed = []
    for kind, value in kinds.items():
        if not isinstance(value, dict):
            continue
        count = int(value.get("count") or 0)
        weighted = float(value.get("weighted") or 0.0)
        if count <= 0 and weighted <= 0:
            continue
        trimmed.append({"kind": kind, "count": count, "weighted": weighted})
    return sorted(trimmed, key=lambda item: (-item["weighted"], item["kind"]))


def _latest_evidence(neighbor: dict[str, Any]) -> dict[str, Any]:
    evidence = neighbor.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return {"latest_label": None, "latest_kind": None, "latest_day": None}
    row = evidence[0] if isinstance(evidence[0], dict) else {}
    return {
        "latest_label": row.get("label"),
        "latest_kind": row.get("kind"),
        "latest_day": row.get("day"),
    }


def _trim_neighbor(neighbor: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_id": neighbor.get("entity_id"),
        "name": neighbor.get("name"),
        "evidence_class": neighbor.get("evidence_class"),
        "count": int(neighbor.get("count") or 0),
        "last_seen": neighbor.get("last_seen"),
        "kinds": _trim_kinds(neighbor.get("kinds") or {}),
        **_latest_evidence(neighbor),
    }


def build_connections_card() -> dict[str, Any]:
    """Build the Pulse home connections card payload from the edge index."""
    try:
        principal = get_journal_principal()
        principal_id = str(principal.get("id") or "") if principal else ""
        if not principal_id:
            return {"state": "empty"}

        network = load_entity_network(principal_id, limit=12, evidence_limit=1)
        neighbors = network.get("neighbors")
        if not isinstance(neighbors, list) or not neighbors:
            return {"state": "empty"}

        return {
            "state": "ok",
            "neighbors": [
                _trim_neighbor(neighbor)
                for neighbor in neighbors
                if isinstance(neighbor, dict)
            ],
            "total": int(network.get("total_neighbors") or 0),
            "kind_words": _kind_words(),
            "attendance_kinds": sorted(ATTENDANCE_KINDS),
        }
    except Exception:
        logger.warning("home: failed to build connections card", exc_info=True)
        return {"state": "unavailable"}
