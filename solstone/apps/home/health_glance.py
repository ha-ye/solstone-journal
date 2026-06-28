# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
from typing import Any

from solstone.apps.home.needs_you import format_degraded_capture_line

logger = logging.getLogger(__name__)

_HEALTH_DETAIL_HREF = "/app/health#focus=recent-errors&day=today"


def build_health_glance(
    capture_health: Any,
    pipeline_status: Any,
    last_observe_relative: str | None,
) -> dict:
    issues = []

    capture_issue = _issue_safely(
        "capture health", capture_health, _build_capture_issue
    )
    if capture_issue is not None:
        issues.append(capture_issue)

    pipeline_issue = _issue_safely(
        "pipeline status", pipeline_status, _build_pipeline_issue
    )
    if pipeline_issue is not None:
        issues.append(pipeline_issue)

    if issues:
        severity = (
            "red" if any(issue["severity"] == "red" for issue in issues) else "amber"
        )
        count = len(issues)
        headline = (
            "1 thing needs your attention"
            if count == 1
            else f"{count} things need your attention"
        )
        return {
            "verdict": "attention",
            "severity": severity,
            "headline": headline,
            "last_observation": None,
            "cta": None,
            "issues": issues,
        }

    status = _observer_state(capture_health)
    if status == "active":
        return {
            "verdict": "ok",
            "severity": "green",
            "headline": "everything's working",
            "last_observation": last_observe_relative,
            "cta": None,
            "issues": [],
        }
    if status == "no_observers":
        return {
            "verdict": "ok",
            "severity": "green",
            "headline": "no observers yet",
            "last_observation": None,
            "cta": {"text": "set one up →", "href": "/app/observer/"},
            "issues": [],
        }
    return {
        "verdict": "unavailable",
        "severity": "amber",
        "headline": "observer status unavailable",
        "last_observation": None,
        "cta": None,
        "issues": [],
    }


def _issue_safely(label: str, value: Any, builder: Any) -> dict | None:
    try:
        return builder(value)
    except Exception:
        logger.warning("omitting malformed %s signal", label, exc_info=True)
        return None


def _build_capture_issue(capture_health: Any) -> dict | None:
    if not isinstance(capture_health, dict):
        return None
    status = capture_health.get("status")
    if status == "degraded":
        text = format_degraded_capture_line(capture_health)
        if not text:
            text = "an observer isn't reaching your journal"
        return {"text": text, "severity": "red", "href": "/app/health"}
    if status == "offline":
        return {
            "text": "no observer is reaching your journal",
            "severity": "red",
            "href": "/app/health",
        }
    if status == "stale":
        name = _first_stale_observer_name(capture_health.get("observers"))
        text = (
            f"{name} hasn't reported recently"
            if name
            else "an observer hasn't reported recently"
        )
        return {"text": text, "severity": "amber", "href": "/app/health"}
    return None


def _build_pipeline_issue(pipeline_status: Any) -> dict | None:
    if not isinstance(pipeline_status, dict) or not pipeline_status:
        return None
    headline = pipeline_status.get("headline")
    text = headline.strip() if isinstance(headline, str) else ""
    if not text:
        text = "processing is behind"
    return {
        "text": text,
        "severity": "amber",
        "href": _pipeline_href(pipeline_status.get("suggested_action")),
    }


def _pipeline_href(suggested_action: Any) -> str:
    if suggested_action == "open_support":
        return "/app/support"
    return _HEALTH_DETAIL_HREF


def _first_stale_observer_name(observers: Any) -> str | None:
    if not isinstance(observers, list):
        return None
    for observer in observers:
        if not isinstance(observer, dict) or observer.get("status") != "stale":
            continue
        name = observer.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None
    return None


def _observer_state(capture_health: Any) -> str:
    if not isinstance(capture_health, dict):
        return "unknown"
    status = capture_health.get("status")
    if status in {"active", "no_observers"}:
        return status
    return "unknown"
