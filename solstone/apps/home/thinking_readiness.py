# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Cached thinking-readiness signal for the home health glance."""

from __future__ import annotations

import logging
import time

from solstone.convey.provider_readiness import _STARTUP_REASON_CODES
from solstone.convey.readiness_snapshot import build_readiness_snapshot
from solstone.think.awareness import get_current, update_state
from solstone.think.journal_io.errors import LockTimeout

logger = logging.getLogger(__name__)

_THINKING_READINESS_CACHE_TTL_S = 300  # seconds


def _generate_path_blocked(generate_view: dict | None) -> bool:
    """True only when the core generate path is genuinely down (not transient).

    Drives off the `generate` interface view alone — never summary.severity
    (which under-fires on a down sole path) and never cogitate/context routes.
    Biases to False on any uncertainty (missing view, unknown/ready status,
    transient local-model startup).
    """
    if not generate_view:
        return False
    if generate_view.get("status") not in {"blocked", "unhealthy"}:
        return False
    return generate_view.get("reason_code") not in _STARTUP_REASON_CODES


def _thinking_blocked() -> bool:
    """Return whether sol has no working way to think, from a short TTL cache.

    Never probes on the hot path: reads the cached bool when fresh, otherwise
    recomputes once from build_readiness_snapshot() and writes the result back
    through the awareness owner (update_state). Biases to False on any error so
    a transient hiccup never false-alarms the home glance.
    """
    now = time.time()
    cached = get_current().get("thinking_readiness", {})
    checked_at = cached.get("checked_at", 0)
    if now - checked_at < _THINKING_READINESS_CACHE_TTL_S and "blocked" in cached:
        return bool(cached["blocked"])

    try:
        snapshot = build_readiness_snapshot()
        generate_view = snapshot.get("interfaces", {}).get("generate")
        blocked = _generate_path_blocked(generate_view)
    except Exception:
        logger.warning(
            "thinking-readiness recompute failed; treating as not blocked",
            exc_info=True,
        )
        return False

    try:
        update_state("thinking_readiness", {"blocked": blocked, "checked_at": now})
    except LockTimeout:
        logger.debug("thinking-readiness cache write skipped (awareness busy)")

    return blocked
