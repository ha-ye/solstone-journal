# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only provider-readiness snapshot aggregation for convey surfaces."""

from __future__ import annotations

import logging
from typing import Any

from solstone.convey.provider_readiness import present_readiness, view_to_dict
from solstone.think import models
from solstone.think.providers import state as provider_state

logger = logging.getLogger(__name__)

_READINESS_SEVERITY_RANK = {
    "ok": 0,
    "neutral": 1,
    "attention": 2,
    "blocker": 3,
}


def unavailable_snapshot() -> dict[str, Any]:
    return {
        "summary": {
            "status": "unknown",
            "severity": "neutral",
            "active_groups": 0,
            "blocked_count": 0,
        },
        "interfaces": {},
        "groups": [],
        "unavailable": True,
    }


def build_readiness_snapshot(
    *,
    local_model_id: str | None = None,
    include_local: bool = False,
) -> dict[str, Any]:
    try:
        snapshot = _build_interface_snapshot()
    except Exception:
        logger.exception("error building provider readiness snapshot")
        return unavailable_snapshot()

    if include_local and local_model_id:
        try:
            local_readiness = provider_state.readiness_for_provider(
                "local", "generate", local_model_id
            )
            snapshot["local"] = view_to_dict(present_readiness(local_readiness))
        except Exception:
            logger.exception("error loading local provider readiness")
            snapshot["local"] = None

    return snapshot


def _build_interface_snapshot() -> dict[str, Any]:
    interface_views = {}
    views = []
    for interface in ("generate", "cogitate"):
        provider, model = models.resolve_provider("", interface)
        readiness = provider_state.readiness_for_provider(provider, interface, model)
        view = present_readiness(readiness)
        interface_views[interface] = view_to_dict(view)
        views.append(view)

    groups_by_key = {}
    for view in views:
        if view.severity not in {"blocker", "attention"}:
            continue
        groups_by_key.setdefault(view.semantic_key, view)

    groups = [view_to_dict(view) for view in groups_by_key.values()]
    worst = max(
        views,
        key=lambda view: _READINESS_SEVERITY_RANK.get(view.severity, -1),
    )

    return {
        "summary": {
            "status": worst.status,
            "severity": worst.severity,
            "active_groups": len(groups),
            "blocked_count": sum(
                1 for view in groups_by_key.values() if view.severity == "blocker"
            ),
        },
        "interfaces": interface_views,
        "groups": groups,
    }


def highest_severity_group(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Return the snapshot group with the highest presenter severity, or None.

    Reuses ``_READINESS_SEVERITY_RANK`` so the ordering matches every other
    readiness surface. ``groups`` only ever contains blocker/attention views;
    ties resolve by ``semantic_key`` for deterministic output.
    """
    groups = snapshot.get("groups") or []
    if not groups:
        return None
    return max(
        groups,
        key=lambda g: (
            _READINESS_SEVERITY_RANK.get(g.get("severity", ""), -1),
            g.get("semantic_key", ""),
        ),
    )
