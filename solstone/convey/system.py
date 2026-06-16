# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""System health API endpoint."""

from __future__ import annotations

import logging
import time
from typing import Any

from flask import Blueprint, jsonify
from packaging.version import InvalidVersion, Version

from solstone.convey.reasons import AWARENESS_BUSY
from solstone.convey.utils import error_response
from solstone.think.capture_health import get_capture_health
from solstone.think.journal_io.errors import LockTimeout

logger = logging.getLogger(__name__)

bp = Blueprint("system", __name__, url_prefix="/api/system")

# Version check cache TTL: 24 hours
_VERSION_CACHE_TTL_S = 86400


def collect_version() -> str | None:
    """Return the installed solstone version string."""
    from solstone.apps.support.diagnostics import collect_version as _collect_version

    return _collect_version()


def _check_latest_version() -> dict[str, Any] | None:
    """Fetch latest release from GitHub. Returns None on any failure."""
    import json as _json
    import urllib.request

    url = "https://api.github.com/repos/solpbc/solstone-journal/releases/latest"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github.v3+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read())
        tag = data.get("tag_name", "")
        # Strip leading 'v' if present
        return {"latest": tag.lstrip("v")}
    except Exception:
        logger.debug("GitHub version check failed", exc_info=True)
        return None


def _is_newer(latest: str, current: str) -> bool:
    """Return True only if `latest` is a strictly newer version than `current`.

    Uses PEP 440 ordering. Returns False conservatively when either string is
    not a parseable version (including the `"unknown"` sentinel for current).
    """
    try:
        return Version(latest) > Version(current)
    except InvalidVersion:
        return False


def _get_version_info() -> dict[str, Any]:
    """Get version info with cached GitHub check."""
    from solstone.think.awareness import get_current, update_state

    current = collect_version() or "unknown"
    result: dict[str, Any] = {"current": current}

    # Check cache
    awareness = get_current()
    cached = awareness.get("version", {})
    checked_at = cached.get("checked_at", 0)
    now = time.time()

    if now - checked_at < _VERSION_CACHE_TTL_S and "latest" in cached:
        result["latest"] = cached["latest"]
    else:
        # Fetch fresh
        fresh = _check_latest_version()
        if fresh:
            result["latest"] = fresh["latest"]
            update_state(
                "version",
                {
                    "latest": fresh["latest"],
                    "checked_at": now,
                },
            )
        elif "latest" in cached:
            # Use stale cache on failure
            result["latest"] = cached["latest"]

    if "latest" in result:
        result["update_available"] = _is_newer(result["latest"], current)

    return result


@bp.route("/status")
def system_status():
    """Return system health: version, capture status, overall ok."""
    try:
        version = _get_version_info()
    except LockTimeout as exc:
        logger.warning("awareness state busy for %s", exc.path)
        return error_response(
            AWARENESS_BUSY,
            detail="awareness state is busy; try again",
        )
    capture = get_capture_health()

    ok = capture["status"] in ("active", "no_observers")

    return jsonify(
        {
            "version": version,
            "capture": capture,
            "ok": ok,
        }
    )
