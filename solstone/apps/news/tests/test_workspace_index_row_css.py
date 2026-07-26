# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

WORKSPACE_PATH = Path(__file__).resolve().parents[1] / "workspace.html"


def _rule_block(css: str, selector: str, start: int = 0) -> tuple[int, str]:
    index = css.find(f"{selector} {{", start)
    assert index != -1, f"{selector} rule was not found"
    close = css.find("}", index)
    assert close != -1, f"{selector} rule is not closed"
    return index, css[index : close + 1]


def _media_block(css: str, anchor: str) -> tuple[int, str]:
    start = css.find(anchor)
    assert start != -1, f"{anchor} block was not found"
    depth = 0
    for index in range(start, len(css)):
        char = css[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return start, css[start : index + 1]
    raise AssertionError(f"{anchor} block is not closed")


def _function_source(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    open_paren = source.index("(", start)
    paren_depth = 0
    close_paren = -1
    for index in range(open_paren, len(source)):
        char = source[index]
        if char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth -= 1
            if paren_depth == 0:
                close_paren = index
                break
    assert close_paren != -1, f"function {name} has no closing parameter list"
    brace = source.index("{", close_paren)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"function {name} has no closing brace")


def test_news_index_link_base_rule_uses_grid_tracks_without_flex_layout():
    css = WORKSPACE_PATH.read_text(encoding="utf-8")

    _, rule = _rule_block(css, ".news-index-link")

    assert "display: grid;" in rule
    assert "grid-template-columns: minmax(0, 11rem) 1fr max-content;" in rule
    assert "display: flex;" not in rule
    assert "justify-content: space-between;" not in rule
    assert "align-items: center;" in rule
    assert "gap: 1rem;" in rule
    assert "padding: 1rem 1.1rem;" in rule
    assert "border-radius: 14px;" in rule
    assert "background: #eff6ff;" in rule
    assert "border: 1px solid #93c5fd;" in rule
    assert "text-decoration: none;" in rule
    assert "color: #1f2937;" in rule
    assert "grid-template-rows" not in rule


def test_news_index_link_hover_background_is_preserved():
    css = WORKSPACE_PATH.read_text(encoding="utf-8")

    _, rule = _rule_block(css, ".news-index-link:hover")

    assert "background: #dbeafe;" in rule


def test_news_index_row_text_rules_prevent_wrapping_and_allow_facet_truncation():
    css = WORKSPACE_PATH.read_text(encoding="utf-8")

    _, facet_rule = _rule_block(css, ".news-index-facet")
    _, day_rule = _rule_block(css, ".news-index-day")

    assert "overflow: hidden;" in facet_rule
    assert "text-overflow: ellipsis;" in facet_rule
    assert "white-space: nowrap;" in facet_rule
    assert "white-space: nowrap;" in day_rule


def test_news_index_mobile_layout_uses_two_columns_with_explicit_placements():
    css = WORKSPACE_PATH.read_text(encoding="utf-8")

    _, mobile_block = _media_block(css, "@media (max-width: 720px)")
    _, mobile_link = _rule_block(mobile_block, ".news-index-link")
    _, mobile_facet = _rule_block(mobile_block, ".news-index-facet")
    _, mobile_day = _rule_block(mobile_block, ".news-index-day")
    _, mobile_token = _rule_block(mobile_block, ".news-index-token")

    assert "grid-template-columns: 1fr max-content;" in mobile_link
    assert "grid-column: 1 / -1;" in mobile_facet
    assert "grid-column: 1;" in mobile_day
    assert "grid-column: 2;" in mobile_token
    assert "grid-template-rows" not in mobile_block


def test_news_index_span_classes_are_defined_and_rendered_in_index_rows():
    source = WORKSPACE_PATH.read_text(encoding="utf-8")

    render_index = _function_source(source, "renderIndex")

    assert ".news-index-facet {" in source
    assert ".news-index-day {" in source
    assert ".news-index-token {" in source
    assert 'class="news-index-facet"' in render_index
    assert 'class="news-index-day"' in render_index
    assert 'class="news-index-token"' in render_index


def test_news_actions_wrap_and_do_not_shrink():
    css = WORKSPACE_PATH.read_text(encoding="utf-8")

    _, rule = _rule_block(css, ".news-actions")

    assert "flex-wrap: wrap;" in rule
    assert "flex-shrink: 0;" in rule
