# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Formatter for messaging category content."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _label(app: Any, thread: Any) -> str:
    app_text = str(app or "").strip()
    thread_text = str(thread or "").strip()
    if app_text and thread_text:
        return f"{app_text} - {thread_text}"
    if app_text:
        return app_text
    if thread_text:
        return thread_text
    return "unknown"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def format(content: Any, context: dict) -> str:
    """Format messaging analysis to markdown."""
    if not isinstance(content, dict):
        return ""

    lines = []
    lines.append(f"**Messaging** ({_label(content.get('app'), content.get('thread'))})")
    lines.append("")

    messages = content.get("messages", [])
    if not isinstance(messages, list):
        messages = []

    for message in messages:
        if not isinstance(message, dict):
            logger.warning(
                "messaging formatter: skipping non-dict message: %r", message
            )
            continue

        sender = _text(message.get("sender") or "Unknown")
        timestamp = _text(message.get("timestamp")).strip()
        subject = _text(message.get("subject")).strip()
        text = _text(message.get("text"))

        body = f"{subject} - {text}" if subject and text else subject or text
        if timestamp:
            lines.append(f"**{sender}** ({timestamp}): {body}")
        else:
            lines.append(f"**{sender}**: {body}")

    return "\n".join(lines)
