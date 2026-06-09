# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Post-hook for persisting facet newsletter markdown."""

from __future__ import annotations

import logging

from solstone.think.tools.facets import facet_news

logger = logging.getLogger(__name__)


def post_process(result: str, context: dict) -> None:
    """Persist non-empty facet newsletters through the facet news tool."""
    facet = str(context.get("facet") or "").strip()
    day = str(context.get("day") or "").strip()
    if not facet:
        logger.error("facet_newsletter hook: missing facet")
        return None
    if not day:
        logger.error("facet_newsletter hook: missing day")
        return None

    content = (result or "").strip()
    if not content:
        logger.info("facet_newsletter hook: blank newsletter for %s %s", facet, day)
        return None
    if content == "No activity":
        logger.info("facet_newsletter hook: no activity for %s %s", facet, day)
        return None

    try:
        response = facet_news(facet, day, markdown=content)
    except Exception as exc:
        logger.error("facet_newsletter hook: failed to save %s %s: %s", facet, day, exc)
        return None

    if response.get("error"):
        logger.error(
            "facet_newsletter hook: failed to save %s %s: %s",
            facet,
            day,
            response["error"],
        )
    return None
