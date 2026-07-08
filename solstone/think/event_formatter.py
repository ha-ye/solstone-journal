# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Event formatting for journal event JSONL files."""

import logging
import re
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from solstone.think.edge_sources import EdgeContext


def _event_base_ts(day_str: str | None) -> int:
    if not day_str:
        return 0
    try:
        dt = datetime.strptime(day_str, "%Y%m%d")
        return int(dt.timestamp() * 1000)
    except ValueError:
        return 0


def _event_timestamp(day_str: str | None, start_time: Any) -> int:
    base_ts = _event_base_ts(day_str)
    if not base_ts:
        return 0
    if not isinstance(start_time, str) or not start_time:
        return base_ts
    try:
        time_parts = start_time.split(":")
        hours = int(time_parts[0])
        minutes = int(time_parts[1]) if len(time_parts) > 1 else 0
        seconds = int(time_parts[2]) if len(time_parts) > 2 else 0
        return base_ts + (hours * 3600 + minutes * 60 + seconds) * 1000
    except (ValueError, IndexError):
        return base_ts


def format_events(
    entries: list[dict],
    context: dict | None = None,
) -> tuple[list[dict], dict]:
    """Format event JSONL entries to markdown chunks.

    This is the formatter function used by the formatters registry.

    Args:
        entries: Raw JSONL entries (one event per line)
        context: Optional context with:
            - file_path: Path to JSONL file (for extracting facet name and day)

    Returns:
        Tuple of (chunks, meta) where:
            - chunks: List of dicts with keys:
                - timestamp: int (unix ms)
                - markdown: str
                - source: dict (original event entry)
            - meta: Dict with optional "header" and "error" keys
    """
    ctx = context or {}
    file_path = ctx.get("file_path")
    meta: dict[str, Any] = {}
    chunks: list[dict[str, Any]] = []
    skipped_count = 0

    # Extract facet name and day from path
    facet_name = "unknown"
    day_str: str | None = None

    if file_path:
        file_path = Path(file_path)

        # Extract facet name from path: facets/{facet}/events/YYYYMMDD.jsonl
        path_str = str(file_path)
        facet_match = re.search(r"facets/([^/]+)/events", path_str)
        if facet_match:
            facet_name = facet_match.group(1)

        # Extract day from filename
        if file_path.stem.isdigit() and len(file_path.stem) == 8:
            day_str = file_path.stem

    # Build header
    if day_str:
        formatted_day = f"{day_str[:4]}-{day_str[4:6]}-{day_str[6:8]}"
        meta["header"] = f"# Events for '{facet_name}' facet on {formatted_day}"
    else:
        meta["header"] = f"# Events for '{facet_name}' facet"

    # Format each event as a chunk
    for event in entries:
        # Skip entries without title
        title = event.get("title")
        if not title:
            skipped_count += 1
            continue

        event_type = event.get("type", "event").capitalize()
        occurred = event.get("occurred", True)

        # Calculate timestamp from day + start time
        start_time = event.get("start", "")
        ts = _event_timestamp(day_str, start_time)

        # Build markdown
        type_prefix = "Planned " if not occurred else ""
        lines = [f"### {type_prefix}{event_type}: {title}\n", ""]

        # Time range (24h format, strip seconds for display)
        end_time = event.get("end", "")
        time_label = "Occurred" if occurred else "Scheduled"
        if start_time:
            start_display = start_time[:5] if len(start_time) >= 5 else start_time
            if end_time:
                end_display = end_time[:5] if len(end_time) >= 5 else end_time
                lines.append(f"**Time {time_label}:** {start_display} - {end_display}")
            else:
                lines.append(f"**Time {time_label}:** {start_display}")

        # Participants
        participants = event.get("participants", [])
        if participants and isinstance(participants, list):
            participants_label = (
                "Expected Participants" if not occurred else "Participants"
            )
            lines.append(f"**{participants_label}:** {', '.join(participants)}")

        # For future-dated event rows, show when they were created (from source path)
        if not occurred:
            source = event.get("source", "")
            # Extract YYYYMMDD from source path like "20240101/talents/agent.md"
            source_match = re.match(r"(\d{8})/", source)
            if source_match:
                created_day = source_match.group(1)
                created_formatted = (
                    f"{created_day[:4]}-{created_day[4:6]}-{created_day[6:8]}"
                )
                lines.append(f"**Created on:** {created_formatted}")

        lines.append("")

        # Summary
        summary = event.get("summary", "")
        if summary:
            lines.append(summary)
            lines.append("")

        # Details
        details = event.get("details", "")
        if details:
            lines.append(details)
            lines.append("")

        chunks.append(
            {
                "timestamp": ts,
                "markdown": "\n".join(lines),
                "source": event,
            }
        )

    # Report skipped entries
    if skipped_count > 0:
        error_msg = f"Skipped {skipped_count} entries missing 'title' field"
        if file_path:
            error_msg += f" in {file_path}"
        meta["error"] = error_msg
        logging.info(error_msg)

    # Indexer metadata - agent is always "event" for events
    meta["indexer"] = {"agent": "event"}

    return chunks, meta


def extract_event_edges(entries: list[dict], ctx: EdgeContext) -> list[dict]:
    """Extract attended-with edges from legacy facet events."""
    rows: list[dict[str, Any]] = []

    for event in entries:
        if not isinstance(event, dict):
            continue
        title = event.get("title")
        if not title:
            continue
        participants = event.get("participants")
        if not isinstance(participants, list):
            continue

        resolved_by_id: dict[str, str] = {}
        for name in participants:
            if not isinstance(name, str) or not name.strip():
                continue
            entity_id = ctx.resolve(name)
            if entity_id is None:
                continue
            resolved_by_id.setdefault(entity_id, name.strip())

        resolved = list(resolved_by_id.items())
        for (src_id, src_name), (dst_id, dst_name) in combinations(resolved, 2):
            if src_id == dst_id:
                continue
            rows.append(
                {
                    "src": src_id,
                    "dst": dst_id,
                    "kind": "attended-with",
                    "src_name": src_name,
                    "dst_name": dst_name,
                    "day": ctx.day,
                    "facet": ctx.facet,
                    "source": "event-legacy",
                    "path": ctx.path,
                    "anchor": "",
                    "label": str(title).strip(),
                    "ts": _event_timestamp(ctx.day, event.get("start", "")),
                    "weight": 1,
                }
            )

    return rows
