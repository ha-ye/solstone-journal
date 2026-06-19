# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Push trigger handlers."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from solstone.convey.chat_stream import day_for_ts, read_chat_events
from solstone.convey.sol_initiated.copy import (
    KIND_OWNER_CHAT_DISMISSED,
    KIND_OWNER_CHAT_OPEN,
    KIND_SOL_CHAT_REQUEST,
)
from solstone.think.push.devices import load_devices
from solstone.think.push.portal_dispatch import (
    dispatch_dedup_via_portal,
    dispatch_via_portal,
)
from solstone.think.utils import get_journal

logger = logging.getLogger("solstone.push.triggers")

FOLD_PUSH_ACTION = "chat_answer_ready"
_VIEWING_STALENESS_MS = 15 * 60 * 1000


def _nudge_log_path() -> Path:
    return Path(get_journal()) / "push" / "nudge_log.jsonl"


def _append_nudge_log(line: dict[str, Any]) -> None:
    path = _nudge_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def _owner_viewing_chat(fold_ts_ms: int) -> bool:
    open_ts: dict[str, int] = {}
    for event in read_chat_events(day_for_ts(fold_ts_ms)):
        kind = event.get("kind")
        request_id = str(event.get("request_id") or "").strip()
        if not request_id:
            continue
        if kind == KIND_OWNER_CHAT_OPEN:
            open_ts[request_id] = int(event.get("ts", 0) or 0)
            continue
        if kind == KIND_OWNER_CHAT_DISMISSED:
            open_ts.pop(request_id, None)

    return any(
        fold_ts_ms - opened_ts <= _VIEWING_STALENESS_MS
        for opened_ts in open_ts.values()
    )


def handle_sol_chat_request(message: dict[str, Any]) -> None:
    if message.get("tract") != "chat" or message.get("event") != KIND_SOL_CHAT_REQUEST:
        return
    request_id = str(message.get("request_id") or "").strip()
    if not request_id:
        return
    summary = str(message.get("summary") or "")
    category = str(message.get("category") or "")
    kind = f"{KIND_SOL_CHAT_REQUEST}_push"

    if not load_devices():
        _append_nudge_log(
            {
                "ts": int(time.time()),
                "kind": kind,
                "dedupe_key": request_id,
                "category": category,
                "outcome": "skipped",
                "reason": "no_devices",
            }
        )
        return

    portal_result = dispatch_via_portal(
        request_id=request_id,
        summary=summary,
        category=category,
    )
    if portal_result is not None:
        _append_nudge_log(
            {
                "ts": int(time.time()),
                "kind": kind,
                "dedupe_key": request_id,
                "category": category,
                "outcome": "dispatched",
                "via": "portal",
            }
        )
        return

    logger.warning(
        "sol chat request push skipped: relay dispatch failed request_id=%s",
        request_id,
    )
    _append_nudge_log(
        {
            "ts": int(time.time()),
            "kind": kind,
            "dedupe_key": request_id,
            "category": category,
            "outcome": "skipped",
            "reason": "portal_unavailable",
        }
    )


def handle_chat_fold(message: dict[str, Any]) -> None:
    if message.get("tract") != "chat" or message.get("event") != "sol_message":
        return
    origin = message.get("origin")
    if not isinstance(origin, dict) or not origin or message.get("requested_target"):
        return
    route_id = str(origin.get("logical_use_id") or "").strip()
    if not route_id:
        return
    fold_ts_ms = int(message["ts"])
    kind = "chat_fold_push"

    if _owner_viewing_chat(fold_ts_ms):
        _append_nudge_log(
            {
                "ts": int(time.time()),
                "kind": kind,
                "dedupe_key": route_id,
                "category": FOLD_PUSH_ACTION,
                "outcome": "skipped",
                "reason": "owner_viewing_chat",
            }
        )
        return

    if not load_devices():
        _append_nudge_log(
            {
                "ts": int(time.time()),
                "kind": kind,
                "dedupe_key": route_id,
                "category": FOLD_PUSH_ACTION,
                "outcome": "skipped",
                "reason": "no_devices",
            }
        )
        return

    portal_result = dispatch_dedup_via_portal(
        request_id=route_id,
        action=FOLD_PUSH_ACTION,
    )
    if portal_result is not None:
        _append_nudge_log(
            {
                "ts": int(time.time()),
                "kind": kind,
                "dedupe_key": route_id,
                "category": FOLD_PUSH_ACTION,
                "outcome": "dispatched",
                "via": "portal",
            }
        )
        return

    logger.warning(
        "chat fold push skipped: relay dispatch failed request_id=%s",
        route_id,
    )
    _append_nudge_log(
        {
            "ts": int(time.time()),
            "kind": kind,
            "dedupe_key": route_id,
            "category": FOLD_PUSH_ACTION,
            "outcome": "skipped",
            "reason": "portal_unavailable",
        }
    )


def handle_chat_lifecycle(message: dict[str, Any]) -> None:
    if message.get("tract") != "chat":
        return
    event = message.get("event")
    if event not in {KIND_OWNER_CHAT_OPEN, KIND_OWNER_CHAT_DISMISSED}:
        return
    raw_request_id = message.get("request_id")
    request_id = raw_request_id.strip() if isinstance(raw_request_id, str) else ""
    if not request_id:
        return
    kind = "sol_chat_lifecycle_push"

    if not load_devices():
        _append_nudge_log(
            {
                "ts": int(time.time()),
                "kind": kind,
                "dedupe_key": request_id,
                "category": event,
                "outcome": "skipped",
                "reason": "no_devices",
            }
        )
        return

    portal_result = dispatch_dedup_via_portal(request_id=request_id, action=event)
    if portal_result is not None:
        _append_nudge_log(
            {
                "ts": int(time.time()),
                "kind": kind,
                "dedupe_key": request_id,
                "category": event,
                "outcome": "dispatched",
                "via": "portal",
            }
        )
        return

    logger.warning(
        "sol chat lifecycle push skipped: relay dispatch failed request_id=%s event=%s",
        request_id,
        event,
    )
    _append_nudge_log(
        {
            "ts": int(time.time()),
            "kind": kind,
            "dedupe_key": request_id,
            "category": event,
            "outcome": "skipped",
            "reason": "portal_unavailable",
        }
    )


__all__ = [
    "handle_chat_fold",
    "handle_chat_lifecycle",
    "handle_sol_chat_" + "request",
]
