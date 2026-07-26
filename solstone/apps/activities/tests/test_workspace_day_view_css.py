# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

WORKSPACE_PATH = Path(__file__).resolve().parents[1] / "workspace.html"


def _class_specificity(selector: str) -> int:
    return selector.count(".")


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


def test_day_view_occ_min_height_mobile_override_wins_by_cascade():
    css = WORKSPACE_PATH.read_text(encoding="utf-8")
    base_selector = ".occ"
    mobile_selector = ".occ"

    base_index, base_rule = _rule_block(css, base_selector)
    media_index, mobile_block = _media_block(css, "@media (max-width: 480px)")
    mobile_index, mobile_rule = _rule_block(mobile_block, mobile_selector)

    assert "min-height: 22px;" in base_rule
    assert "min-height: 30px;" in mobile_rule
    assert _class_specificity(base_selector) <= _class_specificity(mobile_selector)
    assert base_index < media_index + mobile_index


def test_day_view_activity_emission_uses_alpha_background_and_solid_border():
    source = WORKSPACE_PATH.read_text(encoding="utf-8")

    assert "border-color:${color};background:${color}40" in source
    assert "border-color:${color};background:${color}'" not in source
    assert 'style="border-left-color:${color}">' in source
    assert "border-left-color:${color}40" not in source


def test_occ_activity_owns_filter_transition_for_interaction_states():
    css = WORKSPACE_PATH.read_text(encoding="utf-8")

    _, activity_rule = _rule_block(css, ".occ-activity")
    _, hover_rule = _rule_block(css, ".occ-activity:hover")
    _, active_rule = _rule_block(css, ".occ-activity:active")

    assert "transition: filter 0.15s ease;" in activity_rule
    assert "transition: opacity 0.15s ease;" not in activity_rule
    assert "filter: brightness(0.92);" in hover_rule
    assert "filter: brightness(0.86);" in active_rule
    assert "opacity:" not in hover_rule
    assert "opacity:" not in active_rule


def test_occ_timeline_blocks_do_not_use_low_opacity_or_white_text():
    css = WORKSPACE_PATH.read_text(encoding="utf-8")

    _, occ_rule = _rule_block(css, ".occ")
    _, activity_rule = _rule_block(css, ".occ-activity")

    assert "opacity:" not in occ_rule
    assert "opacity:" not in activity_rule
    assert "color: #111827;" in occ_rule
    assert "color: #fff;" not in occ_rule
