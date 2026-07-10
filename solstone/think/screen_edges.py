# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Derived edge extraction from screen understanding frames.

For ``messaged-with``, weight is the count of deduped messages in that
``(app, thread)`` group authored by either endpoint of the pair.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from typing import Any

from solstone.think.edge_sources import EdgeContext, segment_ref
from solstone.think.utils import segment_start_ts_ms


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _event_day(start: Any) -> str | None:
    if not isinstance(start, str):
        return None
    text = start.strip()
    day: str | None = None
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        day = text[:10].replace("-", "")
    else:
        match = re.match(r"^(\d{8})(?!\d)", text)
        if match:
            day = match.group(1)

    if day is None:
        return None
    try:
        datetime.strptime(day, "%Y%m%d")
    except ValueError:
        return None
    return day


def extract_screen_edges(entries: list[dict], ctx: EdgeContext) -> list[dict]:
    """Extract messaging and calendar edges from screen.jsonl frames."""
    anchor, segment_key = segment_ref(ctx.path)
    ts = segment_start_ts_ms(ctx.day, segment_key)
    messaging_rows = _messaging_rows(entries, ctx, anchor, ts)
    calendar_rows = _calendar_rows(entries, ctx, anchor, ts)
    return messaging_rows + calendar_rows


def _messaging_rows(
    entries: list[dict],
    ctx: EdgeContext,
    anchor: str,
    ts: int,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[tuple[Any, ...], dict[str, str]]] = defaultdict(
        dict
    )
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content")
        if not isinstance(content, dict):
            continue
        messaging = content.get("messaging")
        if not isinstance(messaging, dict) or messaging.get("view") != "conversation":
            continue

        app = _string(messaging.get("app"))
        thread = _string(messaging.get("thread"))
        messages = messaging.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict):
                continue
            sender = _string(message.get("sender"))
            key = (
                app,
                thread,
                sender,
                message.get("timestamp"),
                _string(message.get("subject")),
                _string(message.get("text")),
            )
            groups[(app, thread)][key] = {"sender": sender}

    rows: list[dict[str, Any]] = []
    for (_app, thread), messages_by_key in sorted(groups.items()):
        messages = list(messages_by_key.values())
        sender_ids: dict[str, str] = {}
        for sender in sorted({message["sender"] for message in messages}):
            entity_id = ctx.resolve(sender)
            if entity_id is not None:
                sender_ids[sender] = entity_id

        author_ids = [sender_ids.get(message["sender"]) for message in messages]
        resolved_ids = sorted({entity_id for entity_id in author_ids if entity_id})
        for left_id, right_id in combinations(resolved_ids, 2):
            endpoints = {left_id, right_id}
            weight = sum(1 for entity_id in author_ids if entity_id in endpoints)
            rows.append(
                {
                    "src": left_id,
                    "dst": right_id,
                    "kind": "messaged-with",
                    "src_name": None,
                    "dst_name": None,
                    "day": ctx.day,
                    "facet": ctx.facet,
                    "source": "messaging",
                    "path": ctx.path,
                    "anchor": anchor,
                    "label": thread,
                    "ts": ts,
                    "weight": weight,
                }
            )
    return rows


def _calendar_rows(
    entries: list[dict],
    ctx: EdgeContext,
    anchor: str,
    ts: int,
) -> list[dict[str, Any]]:
    events_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content")
        if not isinstance(content, dict):
            continue
        calendar_block = content.get("calendar")
        if not isinstance(calendar_block, dict):
            continue

        app = _string(calendar_block.get("app"))
        events = calendar_block.get("events")
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            key = (
                app,
                _string(event.get("title")),
                _string(event.get("start")),
                _string(event.get("end")),
                _string(event.get("calendar")),
            )
            events_by_key[key] = event

    rows: list[dict[str, Any]] = []
    for event in events_by_key.values():
        guests = event.get("guests")
        if not isinstance(guests, list):
            continue

        resolved_ids: set[str] = set()
        for guest in guests:
            entity_id = ctx.resolve(_string(guest))
            if entity_id is not None:
                resolved_ids.add(entity_id)
        if len(resolved_ids) < 2:
            continue

        day = _event_day(event.get("start")) or ctx.day
        # Edge days must always be parseable; malformed segment days reach here.
        datetime.strptime(day, "%Y%m%d")
        for left_id, right_id in combinations(sorted(resolved_ids), 2):
            rows.append(
                {
                    "src": left_id,
                    "dst": right_id,
                    "kind": "scheduled-with",
                    "src_name": None,
                    "dst_name": None,
                    "day": day,
                    "facet": ctx.facet,
                    "source": "calendar",
                    "path": ctx.path,
                    "anchor": anchor,
                    "label": _string(event.get("title")),
                    "ts": ts,
                    "weight": 1,
                }
            )
    return rows
