# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from solstone.apps.home.needs_you import format_degraded_capture_line
from solstone.convey.backlog_source import BacklogSource
from solstone.convey.backlog_view import stuck_day_rows
from solstone.convey.provider_readiness import DISPLAY_NAMES

logger = logging.getLogger(__name__)

_HEALTH_DETAIL_HREF = "/app/health#focus=recent-errors&day=today"
BACKLOG_FRESHNESS_MAX_AGE_HOURS = 36
_BACKLOG_HREF = "/app/health"


def build_health_glance(
    capture_health: Any,
    pipeline_status: Any,
    last_observe_relative: str | None,
    *,
    backlog: BacklogSource,
    brain: dict[str, Any] | None = None,
) -> dict:
    issues = []

    issues.extend(_build_backlog_issues(backlog))

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

    if isinstance(brain, dict):
        brain_verdict = _brain_inflight_verdict(brain)
        if brain_verdict is not None:
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


def _build_backlog_issues(source: BacklogSource) -> list[dict]:
    issues: list[dict] = []
    if source.validity != "valid":
        issues.append(_backlog_unknown_issue())
        return issues

    backlog = source.backlog
    if not isinstance(backlog, dict):
        issues.append(_backlog_unknown_issue())
        return issues

    if backlog.get("degraded") is True:
        issues.append(_backlog_unknown_issue())

    freshness_issue = _build_backlog_freshness_issue(source.generated_at)
    if freshness_issue is not None:
        issues.append(freshness_issue)

    if _backlog_count(backlog.get("stuck_days")) > 0:
        issues.append(_build_backlog_stuck_issue(backlog))

    return issues


def _backlog_unknown_issue() -> dict:
    return {
        "text": "i can't tell if your journal is caught up right now.",
        "severity": "amber",
        "href": _BACKLOG_HREF,
    }


def _build_backlog_freshness_issue(generated_at: str | None) -> dict | None:
    generated_at_dt = _parse_generated_at(generated_at)
    if generated_at_dt is None:
        return {
            "text": (
                "i can't tell if your journal is caught up; "
                "the last update age is unknown."
            ),
            "severity": "amber",
            "href": _BACKLOG_HREF,
        }

    age = _now_utc() - generated_at_dt
    if age.total_seconds() <= BACKLOG_FRESHNESS_MAX_AGE_HOURS * 3600:
        return None
    return {
        "text": (
            "i can't tell if your journal is caught up; "
            f"the last update was {_format_age(age)} ago."
        ),
        "severity": "amber",
        "href": _BACKLOG_HREF,
    }


def _build_backlog_stuck_issue(backlog: dict) -> dict:
    rows = stuck_day_rows(backlog)
    if rows:
        text = _backlog_stuck_issue_text(rows[0])
    else:
        text = "a journal day needs a hand."
    return {"text": text, "severity": "red", "href": _BACKLOG_HREF}


def _backlog_stuck_issue_text(row: dict) -> str:
    reason = row.get("reason")
    text = reason.strip() if isinstance(reason, str) and reason.strip() else ""
    if not text:
        text = "a journal day needs a hand."

    provider = row.get("provider")
    display_name = DISPLAY_NAMES.get(provider) if isinstance(provider, str) else None
    if display_name:
        return f"{display_name}: {text}"
    return text


def _backlog_count(value: Any) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, count)


def _parse_generated_at(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_age(delta: Any) -> str:
    seconds = max(0, int(delta.total_seconds()))
    hours = seconds // 3600
    if hours >= 1:
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit}"
    minutes = max(1, seconds // 60)
    unit = "minute" if minutes == 1 else "minutes"
    return f"{minutes} {unit}"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


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
