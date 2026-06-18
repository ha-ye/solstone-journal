# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from solstone.convey.chat_sources import parse_sol_sources


def test_markdown_link_source_uses_link_text_and_timeline_url():
    assert parse_sol_sources("[March standup](sol://20260313/archon/091500_300)") == [
        {
            "ref": "sol://20260313/archon/091500_300",
            "label": "March standup",
            "url": "/app/timeline/20260313",
        }
    ]


def test_bare_facet_news_source_derives_label_and_url():
    assert parse_sol_sources("See sol://facets/work/news/20260326") == [
        {
            "ref": "sol://facets/work/news/20260326",
            "label": "work · news",
            "url": "/app/news/work/20260326",
        }
    ]


def test_bare_day_source_derives_month_day_label():
    assert parse_sol_sources("See sol://20260313/archon/091500_300") == [
        {
            "ref": "sol://20260313/archon/091500_300",
            "label": "Mar 13",
            "url": "/app/timeline/20260313",
        }
    ]


def test_events_facet_shape_has_no_url():
    assert parse_sol_sources("See sol://facets/verona/events/20260308") == [
        {
            "ref": "sol://facets/verona/events/20260308",
            "label": "verona · events",
            "url": None,
        }
    ]


def test_reflections_drop_facet_in_url():
    assert parse_sol_sources("See sol://facets/x/reflections/20260310") == [
        {
            "ref": "sol://facets/x/reflections/20260310",
            "label": "x · reflections",
            "url": "/app/reflections/20260310",
        }
    ]


def test_dedupes_by_ref_preserving_first_seen_label():
    assert parse_sol_sources(
        "[First](sol://20260313/archon/091500_300) "
        "[Second](sol://20260313/archon/091500_300) "
        "sol://facets/work/news/20260326"
    ) == [
        {
            "ref": "sol://20260313/archon/091500_300",
            "label": "First",
            "url": "/app/timeline/20260313",
        },
        {
            "ref": "sol://facets/work/news/20260326",
            "label": "work · news",
            "url": "/app/news/work/20260326",
        },
    ]


def test_unknown_shape_degrades_to_ref_label_and_no_url():
    assert parse_sol_sources("See sol://unknown/value") == [
        {
            "ref": "sol://unknown/value",
            "label": "sol://unknown/value",
            "url": None,
        }
    ]


def test_empty_string_returns_no_sources():
    assert parse_sol_sources("") == []
