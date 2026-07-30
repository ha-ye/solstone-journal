# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Live capture-health derivation.

Read-time pull from `apps.observer.utils.list_observers()`. No cache, no
write path -- every call returns fresh state. Status is one of active, stale,
offline, degraded, no_observers, unknown. On any exception below the observer
layer, returns ``{"status": "unknown", "observers": []}`` rather than
propagating; callers render a neutral UI instead of crashing.
"""

from __future__ import annotations

import logging

from solstone.think.utils import now_ms

logger = logging.getLogger(__name__)

_CONNECTED_MS = 30_000
_STALE_MS = 120_000
CAPTURE_STATUS_DEGRADED = "degraded"


def get_capture_health() -> dict:
    """Return {"status": ..., "observers": [...]}.

    status ∈ {"active", "stale", "offline", "degraded", "no_observers", "unknown"}.
    Overall rollup: degraded if any observer is degraded, else active if any
    observer is active, else stale if any is stale, else offline.
    "no_observers" when the filtered list is empty.
    """
    from solstone.apps.observer.utils import (
        get_active_ingest_rejection,
        get_health_beacon,
        list_observers,
        observer_device_binding,
    )

    try:
        observers = list_observers()
        # Filter to active (non-revoked, enabled) observers
        active = [
            o
            for o in observers
            if not o.get("revoked", False) and o.get("enabled", True)
        ]

        if not active:
            return {
                "status": "no_observers",
                "observers": [],
            }

        now = now_ms()
        observer_summaries = []
        statuses = []

        for o in active:
            last_seen = o.get("last_seen")
            if last_seen is None:
                obs_status = "offline"
            else:
                elapsed = now - last_seen
                if elapsed < _CONNECTED_MS:
                    obs_status = "active"
                elif elapsed < _STALE_MS:
                    obs_status = "stale"
                else:
                    obs_status = "offline"

            summary = {
                "name": o.get("name", "unknown"),
                "last_seen": last_seen,
                "status": obs_status,
            }

            if observer_device_binding(o) is None:
                obs_status = CAPTURE_STATUS_DEGRADED
                summary["status"] = obs_status
                summary["unbound"] = True

            rejection = get_active_ingest_rejection(o)
            if rejection is not None:
                obs_status = CAPTURE_STATUS_DEGRADED
                summary["status"] = obs_status
                summary["ingest_rejection"] = {
                    "reason_code": rejection.get("reason_code"),
                    "active_count": rejection.get("active_count"),
                    "first_ts": rejection.get("first_ts"),
                    "latest_ts": rejection.get("latest_ts"),
                    "summary": rejection.get("summary"),
                    "stream": rejection.get("stream"),
                    "version": rejection.get("version"),
                }

            beacon = get_health_beacon(o)
            if beacon is not None:
                summary["beacon"] = beacon

            statuses.append(obs_status)
            observer_summaries.append(summary)

        # Overall status is best healthy state across observers.
        if CAPTURE_STATUS_DEGRADED in statuses:
            overall = CAPTURE_STATUS_DEGRADED
        elif "active" in statuses:
            overall = "active"
        elif "stale" in statuses:
            overall = "stale"
        else:
            overall = "offline"

        return {
            "status": overall,
            "observers": observer_summaries,
        }
    except Exception:
        logger.debug("failed to derive capture health", exc_info=True)
        return {"status": "unknown", "observers": []}
