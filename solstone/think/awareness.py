# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Awareness system — solstone's self-awareness about the user.

Tracks the system's evolving understanding: capture state, identity
persistence, imports, and awareness signals. Two-layer storage:

- ``awareness/current.json`` — materialized current state for fast reads
- ``awareness/YYYYMMDD.jsonl`` — append-only daily log of everything noticed

Designed to extend to cogitate (proactive agents),
learned preferences, and cross-session agent memory.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from solstone.think.journal_io import (
    MalformedPolicy,
    append_jsonl,
    hold_lock,
    read_json,
    write_json,
)

logger = logging.getLogger(__name__)
_LEGACY_AGENT_FIELD = "talent"


def _awareness_dir() -> Path:
    """Return path to the awareness directory, creating it if needed."""
    from solstone.think.utils import get_journal

    d = Path(get_journal()) / "awareness"
    d.mkdir(exist_ok=True)
    return d


def _current_path() -> Path:
    """Return path to the materialized current awareness state."""
    return _awareness_dir() / "current.json"


def _now_ts() -> int:
    """Return current time in milliseconds."""
    return int(time.time() * 1000)


def _today() -> str:
    """Return today's date as YYYYMMDD."""
    return datetime.now().strftime("%Y%m%d")


def _now_iso() -> str:
    """Return current time as compact ISO string."""
    return datetime.now().strftime("%Y%m%dT%H:%M:%S")


def get_current() -> dict[str, Any]:
    """Read the current awareness state from ``awareness/current.json``.

    Returns an empty dict if no state exists yet.
    """
    return read_json(
        _current_path(),
        on_error=MalformedPolicy.WARN_AND_SKIP,
        default={},
    )


def _update_current(
    transform: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Apply a locked read-modify-write transform to current awareness state."""
    path = _current_path()
    with hold_lock(path):
        state = read_json(
            path,
            on_error=MalformedPolicy.WARN_AND_SKIP,
            default={},
        )
        new_state = transform(state)
        write_json(path, new_state)
        return new_state


def update_state(section: str, data: dict[str, Any]) -> dict[str, Any]:
    """Update a section of the current awareness state.

    Merges ``data`` into the named section (creates if missing).
    Returns the updated section.
    """

    def _transform(state: dict[str, Any]) -> dict[str, Any]:
        existing = state.get(section, {})
        existing.update(data)
        state[section] = existing
        return state

    return _update_current(_transform)[section]


def append_log(
    kind: str,
    *,
    key: str | None = None,
    message: str | None = None,
    data: dict[str, Any] | None = None,
    day: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Append an entry to the daily awareness log.

    Parameters
    ----------
    kind : str
        Entry type: "state", "observation", "nudge", "interaction", "preference"
    key : str, optional
        Dotted key for state entries (e.g., "onboarding.started")
    message : str, optional
        Human-readable message
    data : dict, optional
        Structured data payload
    day : str, optional
        Override day (defaults to today)
    **extra
        Additional fields merged into the entry

    Returns
    -------
    dict
        The entry that was written
    """
    entry: dict[str, Any] = {"ts": _now_ts(), "kind": kind}
    if key:
        entry["key"] = key
    if message:
        entry["message"] = message
    if data:
        entry["data"] = data
    entry.update(extra)

    log_day = day or _today()
    log_path = _awareness_dir() / f"{log_day}.jsonl"
    append_jsonl(log_path, entry)

    return entry


def read_log(day: str | None = None) -> list[dict[str, Any]]:
    """Read all entries from a daily awareness log.

    Parameters
    ----------
    day : str, optional
        Day in YYYYMMDD format (defaults to today)

    Returns
    -------
    list[dict]
        Entries in chronological order, empty list if no log exists
    """
    log_day = day or _today()
    log_path = _awareness_dir() / f"{log_day}.jsonl"
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping malformed awareness log entry")
    return entries


# --- Import tracking convenience functions ---


def _default_imports() -> dict[str, Any]:
    """Return a fresh default import tracking state."""
    return {
        "has_imported": False,
        "import_count": 0,
        "sources_used": [],
        "offer_declined": None,
        "last_nudge": None,
    }


def get_imports() -> dict[str, Any]:
    """Return the current import tracking state, or defaults if none."""
    state = get_current()
    return state.get("imports", _default_imports())


def _recent_chat_exchanges(limit: int = 10000) -> list[dict[str, Any]]:
    """Return owner-visible chat responses from chat stream history."""
    from solstone.think.utils import day_dirs

    try:
        days = day_dirs()
    except Exception:
        return []

    exchanges: list[dict[str, Any]] = []
    for day_name in sorted(days):
        day_path = Path(days[day_name])
        chat_root = day_path / "chat"
        if not chat_root.exists():
            continue
        for segment_dir in sorted(chat_root.iterdir()):
            if not segment_dir.is_dir():
                continue
            chat_path = segment_dir / "chat.jsonl"
            if not chat_path.exists():
                continue
            try:
                for line in chat_path.read_text().splitlines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get("kind") != "sol_message":
                        continue
                    exchanges.append(
                        {
                            "talent": "chat",
                            "agent_response": event.get("text", ""),
                        }
                    )
            except (OSError, json.JSONDecodeError):
                logger.warning("Skipping malformed chat stream file: %s", chat_path)
    if limit <= 0:
        return []
    return exchanges[-limit:]


def owner_detection_ready() -> dict[str, Any]:
    """Check if owner voice detection should be surfaced to the user.

    Returns a dict with a ``ready`` boolean and contextual fields.

    Checks in order:
    1. Owner centroid already exists → not ready
    2. Recent rejection within 14 days → not ready (cooldown)
    3. Calls ``detect_owner_candidate()`` → ready if positive recommendation
    """
    from solstone.apps.speakers.owner import detect_owner_candidate, load_owner_centroid

    if load_owner_centroid() is not None:
        return {"ready": False, "reason": "centroid_exists"}

    voiceprint = get_current().get("voiceprint", {})
    rejected_at = voiceprint.get("rejected_at")
    if rejected_at:
        try:
            rejection_time = datetime.fromisoformat(rejected_at)
            now = datetime.now(rejection_time.tzinfo)
            days_since = (now - rejection_time).days
            if days_since < 14:
                return {
                    "ready": False,
                    "reason": "cooldown",
                    "days_remaining": 14 - days_since,
                }
        except (ValueError, TypeError):
            pass

    result = detect_owner_candidate()
    if result.get("recommendation") == "ready":
        return {
            "ready": True,
            "reason": "candidate_found",
            "cluster_size": result.get("cluster_size"),
            "streams_represented": result.get("streams_represented"),
            "samples": result.get("samples", []),
        }

    return {
        "ready": False,
        "reason": result.get("recommendation", result.get("status", "unknown")),
    }


def record_import(
    source_type: str,
    source_display: str | None = None,
    entries_written: int = 0,
) -> dict[str, Any]:
    """Record a completed import.

    Parameters
    ----------
    source_type : str
        Import source type (e.g., "chatgpt", "ics", "claude")
    source_display : str, optional
        Human-readable source display name
    entries_written : int
        Number of entries imported

    Returns
    -------
    dict
        The updated imports state
    """

    def _transform(state: dict[str, Any]) -> dict[str, Any]:
        imports = state.get("imports") or _default_imports()
        sources = imports.get("sources_used", [])
        if source_type not in sources:
            sources.append(source_type)
        imports["has_imported"] = True
        imports["import_count"] = imports.get("import_count", 0) + 1
        imports["sources_used"] = sources
        if source_display is not None:
            summary = (
                f"{entries_written} {source_display}"
                if entries_written
                else source_display
            )
            imports["last_completed"] = _now_iso()
            imports["last_result_summary"] = summary
        state["imports"] = imports
        return state

    new_state = _update_current(_transform)
    append_log("state", key="imports.completed", data={"source_type": source_type})
    return new_state["imports"]


def record_import_offer_declined() -> dict[str, Any]:
    """Record that the user declined an import offer.

    Returns
    -------
    dict
        The updated imports state
    """

    def _transform(state: dict[str, Any]) -> dict[str, Any]:
        imports = state.get("imports") or _default_imports()
        imports["offer_declined"] = _now_iso()
        state["imports"] = imports
        return state

    new_state = _update_current(_transform)
    append_log("state", key="imports.offer_declined")
    return new_state["imports"]


def record_import_nudge() -> dict[str, Any]:
    """Record that triage nudged the user about imports.

    Returns
    -------
    dict
        The updated imports state
    """

    def _transform(state: dict[str, Any]) -> dict[str, Any]:
        imports = state.get("imports") or _default_imports()
        imports["last_nudge"] = _now_iso()
        state["imports"] = imports
        return state

    new_state = _update_current(_transform)
    append_log("state", key="imports.nudge_sent")
    return new_state["imports"]
