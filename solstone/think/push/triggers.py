# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Push trigger handlers."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

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
from solstone.think.push.relay_auth import push_relay_token
from solstone.think.utils import get_journal

logger = logging.getLogger("solstone.push.triggers")


def _nudge_log_path() -> Path:
    return Path(get_journal()) / "push" / "nudge_log.jsonl"


def _append_nudge_log(line: dict[str, Any]) -> None:
    path = _nudge_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def handle_sol_chat_request(message: dict[str, Any]) -> None:
    if message.get("tract") != "chat" or message.get("event") != KIND_SOL_CHAT_REQUEST:
        return
    request_id = str(message.get("request_id") or "").strip()
    if not request_id:
        return
    summary = str(message.get("summary") or "")
    category = str(message.get("category") or "")
    kind = f"{KIND_SOL_CHAT_REQUEST}_push"

    if not push_relay_token():
        _append_nudge_log(
            {
                "ts": int(time.time()),
                "kind": kind,
                "dedupe_key": request_id,
                "category": category,
                "outcome": "skipped",
                "reason": "no_relay_token",
            }
        )
        return

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

    if not push_relay_token():
        _append_nudge_log(
            {
                "ts": int(time.time()),
                "kind": kind,
                "dedupe_key": request_id,
                "category": event,
                "outcome": "skipped",
                "reason": "no_relay_token",
            }
        )
        return

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
    "handle_chat_lifecycle",
    "handle_sol_chat_" + "request",
]
