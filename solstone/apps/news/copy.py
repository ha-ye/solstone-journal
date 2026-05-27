# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc
"""owner-facing strings; do not inline in templates."""

NEWS_SIDEBAR_LABEL = "newsletters"

NEWS_KICKER = "facet newsletter"

NEWS_INDEX_H1 = "newsletters"

NEWS_SUBTITLE = "sol's daily per-facet summaries from your journal."

NEWS_EMPTY_BODY = (
    "Every day, sol writes one newsletter per facet that had activity — "
    "your meetings, decisions, follow-ups, and what changed for that part "
    "of your life. A short read per facet, in your journal, with sol's notes."
)

NEWS_EMPTY_TOMORROW_WITH_DATE = "Your first newsletters arrive {tomorrow}."
NEWS_EMPTY_NO_DATE = "Newsletters arrive as your journal fills."
NEWS_EMPTY_UNTIL_THEN = (
    "Until then, this page will be empty. "
    "Newsletters appear here every day, newest first, grouped by facet."
)

NEWS_SAMPLE_LINK_LABEL = "see a sample newsletter →"

NEWS_POPULATED_FRAMING = (
    "Every day, sol writes one newsletter per facet with activity — newest below."
)
NEWS_POPULATED_SAMPLE_LINK = "see a sample"
NEWS_POPULATED_NEXT_FOOTER = "next newsletters: {when}"

NEWS_DETAIL_SUBTITLE = "sol's notes for {facet} on this day."
NEWS_DETAIL_DEBUG_LINK = "see how this was generated →"

NEWS_SAMPLE_BANNER = "This is a sample newsletter — not from your journal."
NEWS_SAMPLE_H1 = "sample newsletter"

# Inlined from solstone/tests/fixtures/journal/facets/verona/news/20260310.md
# tests/fixtures/ is excluded from PyPI wheels, so the sample route must not
# read the fixture off disk at runtime — installed builds would 404.
# Sync-guarded against the disk fixture by test_sample_content_matches_fixture.
SAMPLE_CONTENT = """# 2026-03-10 News - Verona

## Verona Platform Joint Venture Officially Launches
**Source:** press-release | **Time:** 14:00
Montague Tech and Capulet Industries announced a joint venture to develop the Verona Platform, a unified API gateway combining mesh routing and schema translation. The platform demonstrated sub-millisecond latency at 10,000 req/s during the board presentation.
"""
