# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pure rendering helpers for serialized processing backlog views."""

from __future__ import annotations

import math
from typing import Any

from solstone.convey import backlog_copy, provider_readiness

__all__ = [
    "stuck_rows",
    "verdict",
]


def _count(value: Any) -> int | float:
    if value is None:
        return 0
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(num) or num <= 0:
        return 0
    return int(num) if num.is_integer() else num


def _fmt(
    template: str, stuck: int | float | None = None, pending: int | float | None = None
) -> str:
    return (
        str(template or "")
        .replace("{stuck_n}", "" if stuck is None else str(stuck))
        .replace("{pending_n}", "" if pending is None else str(pending))
    )


def verdict(backlog: dict | None) -> str:
    p = _count(backlog and backlog.get("pending_days"))
    s = _count(backlog and backlog.get("stuck_days"))

    if backlog is None or backlog.get("degraded") is True:
        return backlog_copy.BACKLOG_VERDICT_CANT_TELL
    if p == 0 and s == 0:
        return backlog_copy.BACKLOG_VERDICT_CAUGHT_UP
    if s > 0 and p == 0:
        return _fmt(
            backlog_copy.BACKLOG_VERDICT_STUCK_ONLY_SINGULAR
            if s == 1
            else backlog_copy.BACKLOG_VERDICT_STUCK_ONLY_PLURAL,
            stuck=s,
        )
    if s == 0 and p > 0:
        return _fmt(
            backlog_copy.BACKLOG_VERDICT_PENDING_ONLY_SINGULAR
            if p == 1
            else backlog_copy.BACKLOG_VERDICT_PENDING_ONLY_PLURAL,
            pending=p,
        )

    stuck_arm = _fmt(
        backlog_copy.BACKLOG_VERDICT_MIXED_STUCK_SINGULAR
        if s == 1
        else backlog_copy.BACKLOG_VERDICT_MIXED_STUCK_PLURAL,
        stuck=s,
    )
    pending_arm = _fmt(
        backlog_copy.BACKLOG_VERDICT_MIXED_PENDING_SINGULAR
        if p == 1
        else backlog_copy.BACKLOG_VERDICT_MIXED_PENDING_PLURAL,
        pending=p,
    )
    return f"{stuck_arm}. {pending_arm}."


def _error_for_day(day: dict, backlog: dict) -> object | None:
    if day.get("error"):
        return day.get("error")
    errors = backlog.get("errors") or []
    return next(
        (error for error in errors if error.get("day") == day.get("day")),
        None,
    )


def _needs_hand(day: dict, backlog: dict) -> bool:
    return day.get("state") == "stuck" or _error_for_day(day, backlog) is not None


def _reason_copy(day: dict) -> str:
    reason = day.get("reason")
    reason_code = day.get("reason_code")
    marker = reason_code if isinstance(reason_code, str) and reason_code else reason
    if marker == "corrupt_raw":
        return backlog_copy.BACKLOG_REASON_CORRUPT_RAW
    category = provider_readiness.backlog_reason_category(marker)
    if category == "setup":
        return backlog_copy.BACKLOG_REASON_MISSING_CONFIG
    if category in {"provider", "startup"}:
        # startup (transient local-model states) has no dedicated sentence;
        # PROVIDER_DOWN ("...try again") is the closest honest existing copy.
        # Follow-up: a future copy pass could add a "still starting up - try
        # again shortly" sentence and split startup off from provider here.
        return backlog_copy.BACKLOG_REASON_PROVIDER_DOWN
    if category == "request":
        return backlog_copy.BACKLOG_REASON_PROVIDER_REFUSED
    return backlog_copy.BACKLOG_REASON_FAILING_STEP


def stuck_rows(backlog: dict | None) -> list[dict]:
    if backlog is None or backlog.get("degraded") is True:
        return []

    rows = []
    for day in backlog.get("days") or []:
        if not _needs_hand(day, backlog):
            continue
        depth = _count(day.get("segments")) + _count(day.get("units"))
        row = {
            "day": day.get("day"),
            "reason": _reason_copy(day),
            "depth": depth if depth > 0 else None,
        }
        for field in ("reason_code", "provider", "model"):
            if day.get(field):
                row[field] = day.get(field)
        rows.append(row)
    return rows
