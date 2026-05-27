# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""Compute the next-newsletter cue token for owner-facing copy.

The cue is `tomorrow morning` when the next daily-dream fire is on the next
calendar day, else `<Weekday> morning` for the day after. The daily fire time
is not cheaply recoverable from the facet_newsletter talent metadata, so we
default to `tomorrow morning` — a safe fallback per the spec.
"""

from __future__ import annotations

from datetime import date, datetime


def format_news_list_date(date_str: str) -> str:
    """Format YYYYMMDD as 'Mon May 26, 2026' (no leading zero on day)."""
    try:
        date_obj = datetime.strptime(date_str, "%Y%m%d")
        return date_obj.strftime(f"%a %b {date_obj.day}, %Y")
    except ValueError:
        return date_str


def next_newsletter_when(today: date) -> str:
    """Return `tomorrow morning` or `<Weekday> morning` for the populated-state footer.

    Without a recoverable daily-dream fire time, we always say `tomorrow morning`
    — the spec's `tomorrow` fallback. Kept as a function so a future enhancement
    that does recover the fire time can produce `<Weekday> morning` cleanly.
    """
    _ = today
    return "tomorrow morning"
