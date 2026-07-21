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
    brain: dict[str, Any] | None = None,
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

    brain_issue = _build_brain_issue(brain)
    if brain_issue is not None:
        issues.append(brain_issue)

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
    if status not in {"active", "no_observers"}:
        return {
            "verdict": "unavailable",
            "severity": "amber",
            "headline": "i don't know the status of your devices right now.",
            "last_observation": None,
            "cta": None,
            "issues": [],
        }

    brain_verdict = _brain_inflight_verdict(brain)
    if brain_verdict is not None:
        assert isinstance(brain, dict)
        return {
            "verdict": brain_verdict,
            "severity": "amber",
            "headline": brain["headline"],
            "last_observation": None,
            "cta": None,
            "issues": [],
        }

    if status == "active":
        return {
            "verdict": "ok",
            "severity": "green",
            "headline": "everything's working",
            "last_observation": last_observe_relative,
            "cta": None,
            "issues": [],
        }
    return {
        "verdict": "ok",
        "severity": "green",
        "headline": "no devices are running sol yet. set one up to start your journal.",
        "last_observation": None,
        "cta": {"text": "set one up →", "href": "/app/observer/"},
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
            text = "one of your devices isn't reaching your journal."
        return {"text": text, "severity": "red", "href": "/app/health"}
    if status == "offline":
        return {
            "text": "nothing is reaching your journal.",
            "severity": "red",
            "href": "/app/health",
        }
    if status == "stale":
        return {
            "text": "one of your devices hasn't reached your journal recently.",
            "severity": "amber",
            "href": "/app/health",
        }
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


def _build_brain_issue(brain: Any) -> dict | None:
    if not isinstance(brain, dict):
        return None
    if brain.get("state") == "ready":
        return None
    if _brain_inflight_verdict(brain) is not None:
        return None
    action = brain.get("action")
    if not isinstance(action, dict) and brain.get("state") not in {
        "blocked",
        "unhealthy",
        "unknown",
    }:
        return None
    text = brain.get("headline")
    if not isinstance(text, str) or not text.strip():
        return None
    href = "/app/health/#brain"
    if isinstance(action, dict) and isinstance(action.get("href"), str):
        href = action["href"]
    return {"text": text.strip(), "severity": "amber", "href": href}


def _brain_inflight_verdict(brain: Any) -> str | None:
    """Return the status-only verdict token for an in-flight brain state."""
    if not isinstance(brain, dict):
        return None
    if brain.get("state") == "checking":
        return "checking"
    if brain.get("state") == "blocked" and brain.get("progressing"):
        return "progressing"
    return None


def _pipeline_href(suggested_action: Any) -> str:
    if suggested_action == "open_support":
        return "/app/support"
    return _HEALTH_DETAIL_HREF


def _observer_state(capture_health: Any) -> str:
    if not isinstance(capture_health, dict):
        return "unknown"
    status = capture_health.get("status")
    if status in {"active", "no_observers"}:
        return status
    return "unknown"
