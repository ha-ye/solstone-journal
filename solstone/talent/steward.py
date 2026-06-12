# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Hooks for the steward cadence health talent.

The 4-section health.md body is rendered **deterministically** in the pre-hook
(no LLM in that write path). The talent itself is a tiny ``lite`` generate that
writes only the human-friendly summaries the home widget surfaces, appending
them to the day-jsonl accumulator. Repair is not steward's job — it runs in the
deterministic overnight ``journal heartbeat`` (``heartbeat.py``); the pre-hook
only *reads* the latest pass event.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from solstone.think.day_accumulator import append_record
from solstone.think.steward import (
    acquire_steward_lock,
    default_summary_from_body,
    gather_health_facts,
    load_latest_pass_event,
    load_previous_summary,
    normalize_summary,
    release_steward_lock,
    render_health_body,
    write_health_md,
)
from solstone.think.utils import now_ms

logger = logging.getLogger(__name__)


def _today_from_config(config: dict) -> str:
    day = config.get("day")
    if isinstance(day, str) and day:
        return day
    return datetime.now().strftime("%Y%m%d")


def _generated_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def pre_process(config: dict) -> dict | None:
    """Render + write health.md deterministically; feed the summary talent.

    Holds the single-flight lock only for the deterministic render+write (the
    LLM summary step that follows needs no steward lock). Returns the template
    vars the lite generate consumes: the new health state and the previous run's
    summary (for run-to-run continuity).
    """
    today = _today_from_config(config)
    dry_run = bool(config.get("dry_run"))

    fd = acquire_steward_lock()
    if fd is None:
        return {"skip_reason": "steward already in flight"}
    try:
        pass_event = load_latest_pass_event()
        if pass_event is None:
            escalated_targets: list = []
            pass_errors: list = []
        else:
            escalated_targets = list(pass_event.get("escalated_targets", []))
            pass_errors = list(pass_event.get("data_source_errors", []))

        facts = gather_health_facts(today)
        data_source_errors = list(facts.get("data_source_errors") or []) + pass_errors

        body = render_health_body(
            generated_at=facts["generated_at"],
            pipeline_day=facts.get("pipeline_day"),
            recipe_outcomes_7d=facts.get("recipe_outcomes_7d") or [],
            escalated_targets=escalated_targets,
            data_source_errors=data_source_errors,
        )

        if not dry_run:
            reason = write_health_md(body)
            if reason is not None:
                logger.error("steward deterministic render rejected: %s", reason)

        # Stash a deterministic fallback so post_process can recover if the model
        # output is missing or malformed.
        config["_steward_default_summary"] = default_summary_from_body(body)

        previous = load_previous_summary(today)
        return {
            "template_vars": {
                "health_state": body,
                "previous_summary": (
                    json.dumps(previous, indent=2, sort_keys=True)
                    if previous is not None
                    else "(none — first run)"
                ),
            }
        }
    except Exception as exc:
        logger.exception("steward pre-hook failed")
        return {"skip_reason": f"steward pre-hook failed: {exc}"}
    finally:
        release_steward_lock(fd)


def post_process(result: str, config: dict) -> str:
    """Normalize the model's summary and append it to the day accumulator."""
    default = config.get("_steward_default_summary") or {
        "headline": "Health summary unavailable",
        "summary_sentence": "Sol could not produce a health summary this run.",
        "suggested_action": "open_health_detail",
    }
    summary = normalize_summary(result, default)
    day = _today_from_config(config)
    record = {
        **summary,
        "model": config.get("model"),
        "generated_at": _generated_at(),
        "ts": now_ms(),
    }
    append_record(day, "steward", record)
    return json.dumps(summary, indent=2, sort_keys=True)
