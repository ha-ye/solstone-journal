# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Formatter for calendar category content."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def format(content: Any, context: dict) -> str:
    """Format calendar analysis to markdown."""
    if not isinstance(content, dict):
        return ""

    app = _text(content.get("app")) or "unknown"
    view = _text(content.get("view")) or "unknown"
    lines = [f"**Calendar** ({app} - {view})", ""]

    date_range = _text(content.get("range"))
    if date_range:
        lines.append(f"*{date_range}*")
        lines.append("")

    events = content.get("events", [])
    if not isinstance(events, list):
        events = []

    for event in events:
        if not isinstance(event, dict):
            logger.warning("calendar formatter: skipping non-dict event: %r", event)
            continue

        title = _text(event.get("title")) or "Untitled event"
        start = _text(event.get("start"))
        end = _text(event.get("end"))
        time_label = ""
        if start and end:
            time_label = f"{start} - {end}"
        elif start:
            time_label = start
        elif end:
            time_label = end

        status = _text(event.get("status"))
        event_line = f"- **{title}**"
        if time_label:
            event_line += f" ({time_label})"
        if status and status != "unknown":
            event_line += f" [{status}]"
        lines.append(event_line)

        location = _text(event.get("location"))
        if location:
            lines.append(f"  - Location: {location}")
        conferencing = _text(event.get("conferencing"))
        if conferencing:
            lines.append(f"  - Conferencing: {conferencing}")
        guests = event.get("guests", [])
        if isinstance(guests, list):
            guest_text = ", ".join(_text(guest) for guest in guests if _text(guest))
            if guest_text:
                lines.append(f"  - Guests: {guest_text}")
        recurrence = _text(event.get("recurrence"))
        if recurrence:
            lines.append(f"  - Recurrence: {recurrence}")
        calendar_name = _text(event.get("calendar"))
        if calendar_name:
            lines.append(f"  - Calendar: {calendar_name}")
        description = _text(event.get("description"))
        if description:
            lines.append(f"  - Description: {description}")

    availability = content.get("availability", [])
    if isinstance(availability, list):
        availability_text = ", ".join(
            _text(slot) for slot in availability if _text(slot)
        )
        if availability_text:
            if lines[-1] != "":
                lines.append("")
            lines.append(f"**Availability:** {availability_text}")

    notes = _text(content.get("notes"))
    if notes:
        if lines[-1] != "":
            lines.append("")
        lines.append(notes)

    return "\n".join(lines)
