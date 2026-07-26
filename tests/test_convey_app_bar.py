# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_CSS_PATH = REPO_ROOT / "solstone" / "convey" / "static" / "app.css"


def _class_specificity(selector: str) -> int:
    return selector.count(".")


def _rule_block(css: str, selector: str, start: int = 0) -> tuple[int, str]:
    index = css.find(f"{selector} {{", start)
    assert index != -1, f"{selector} rule was not found"
    close = css.find("}", index)
    assert close != -1, f"{selector} rule is not closed"
    return index, css[index : close + 1]


def _block_at(css: str, start: int) -> tuple[int, str]:
    depth = 0
    for index in range(start, len(css)):
        char = css[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return start, css[start : index + 1]
    raise AssertionError(f"block at {start} is not closed")


def _media_blocks(css: str, query: str = "@media") -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    start = css.find(query)
    while start != -1:
        blocks.append(_block_at(css, start))
        start = css.find(query, start + len(query))
    return blocks


def _unconditional_app_bar_geometry_rule(css: str) -> tuple[int, str]:
    media_spans = [
        (index, index + len(block)) for index, block in _media_blocks(css, "@media")
    ]
    start = css.find(".app-bar {")
    while start != -1:
        if not any(
            span_start <= start < span_end for span_start, span_end in media_spans
        ):
            rule_index, rule = _rule_block(css, ".app-bar", start)
            if "left:" in rule and "right:" in rule and "max-width:" in rule:
                return rule_index, rule
        start = css.find(".app-bar {", start + len(".app-bar {"))
    raise AssertionError("unconditional .app-bar geometry rule was not found")


def _mobile_app_bar_left_rules(css: str) -> list[tuple[int, str, str, int]]:
    rules: list[tuple[int, str, str, int]] = []
    for block_index, block in _media_blocks(css, "@media (max-width: 768px)"):
        rule_index = block.find(".app-bar {")
        if rule_index == -1:
            continue
        _, rule = _rule_block(block, ".app-bar", rule_index)
        if "left:" in rule:
            rules.append((block_index + rule_index, rule, block, rule_index))
    return rules


def test_desktop_app_bar_geometry_remains_unchanged():
    css = APP_CSS_PATH.read_text(encoding="utf-8")
    _, base_rule = _unconditional_app_bar_geometry_rule(css)

    assert "left: var(--menu-bar-width-minimal);" in base_rule
    assert "right: 0;" in base_rule
    assert "max-width: 1200px;" in base_rule


def test_exactly_one_mobile_app_bar_rule_declares_left_with_comment():
    css = APP_CSS_PATH.read_text(encoding="utf-8")
    rules = _mobile_app_bar_left_rules(css)

    assert len(rules) == 1

    _absolute_index, rule, block, rule_index = rules[0]
    comment_index = block.rfind("/*", 0, rule_index)
    comment_end = block.find("*/", comment_index)

    assert comment_index != -1
    assert comment_end != -1
    assert comment_end < rule_index
    assert comment_index < rule_index
    assert "left: 8px;" in rule
    assert "right: 8px;" in rule
    assert "padding: 10px;" in rule
    assert "gap: 10px;" in rule


def test_later_mobile_app_bar_rule_wins_by_source_order_at_equal_specificity():
    css = APP_CSS_PATH.read_text(encoding="utf-8")
    base_selector = ".app-bar"
    mobile_selector = ".app-bar"
    base_index, _base_rule = _unconditional_app_bar_geometry_rule(css)
    rules = _mobile_app_bar_left_rules(css)

    assert len(rules) == 1

    mobile_index, _mobile_rule, _block, _block_rule_index = rules[0]

    assert _class_specificity(base_selector) == _class_specificity(mobile_selector)
    assert base_index < mobile_index
