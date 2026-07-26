# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

APP_CSS_PATH = Path("solstone/convey/static/app.css")
LIVE_MOBILE_MENU_MEDIA_ANCHOR = (
    "@media (max-width: 768px) {\n  body:not(.menu-full) .menu-bar {"
)
MOBILE_BARE_FACET_AFTER_ANCHOR = "\n  .facet-bar::after {\n"
MOBILE_MENU_FULL_FACET_AFTER_ANCHOR = "\n  body.menu-full .facet-bar::after {\n"
SURFACE_STATE_DESC_ANCHOR = "\n.surface-state-desc {\n"
ACTIVE_DIALOG_MARKER_SELECTOR = (
    "[data-convey-active-dialog],\n[data-convey-active-dialog-host]"
)


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


def test_surface_state_desc_color_uses_accessible_gray():
    css = APP_CSS_PATH.read_text(encoding="utf-8")

    assert css.count(SURFACE_STATE_DESC_ANCHOR) == 1
    _, rule = _rule_block(css, ".surface-state-desc")

    assert "color: #6b7280;" in rule
    assert "#9ca3af" not in rule


def test_surface_state_container_color_uses_accessible_gray():
    css = APP_CSS_PATH.read_text(encoding="utf-8")

    assert css.count("\n.surface-state {\n") == 1
    _, rule = _rule_block(css, ".surface-state")

    assert "color: #6b7280;" in rule
    assert "#9ca3af" not in rule


def test_surface_state_desc_metrics_stay_unchanged():
    css = APP_CSS_PATH.read_text(encoding="utf-8")

    _, rule = _rule_block(css, ".surface-state-desc")

    assert "margin: 0;" in rule
    assert "max-width: 36ch;" in rule
    assert "font-size: 14px;" in rule
    assert "line-height: 1.5;" in rule


def test_surface_state_heading_keeps_existing_gray():
    css = APP_CSS_PATH.read_text(encoding="utf-8")

    assert css.count("\n.surface-state-heading {\n") == 1
    _, rule = _rule_block(css, ".surface-state-heading")

    assert "color: #4b5563;" in rule


def test_active_dialog_z_token_sits_above_shell_tags():
    css = APP_CSS_PATH.read_text(encoding="utf-8")
    _, root_rule = _rule_block(css, ":root")

    assert "--z-tags: 400;" in root_rule
    assert "--z-active-dialog: 10000;" in root_rule
    assert ".skip-link (9999)" in root_rule
    assert "presentation-mode body::after (9998)" in root_rule
    assert "click-to-exit pseudo-element" in root_rule
    assert root_rule.index("--z-tags: 400;") < root_rule.index(
        "--z-active-dialog: 10000;"
    )


def test_workspace_view_transition_is_disabled_while_managed_dialog_is_open():
    css = APP_CSS_PATH.read_text(encoding="utf-8")
    _, workspace_rule = _rule_block(css, ".workspace")
    _, active_rule = _rule_block(css, "body.has-managed-dialog .workspace")

    assert "view-transition-name: workspace;" in workspace_rule
    assert "view-transition-name: none;" in active_rule


def test_active_dialog_marker_uses_active_dialog_z_token():
    css = APP_CSS_PATH.read_text(encoding="utf-8")
    _, marker_rule = _rule_block(css, ACTIVE_DIALOG_MARKER_SELECTOR)

    assert "App workspace <style> blocks load after app.css" in marker_rule
    assert "z-index: var(--z-active-dialog) !important;" in marker_rule


def test_mobile_menu_full_facet_bar_separator_override_wins_by_source_order():
    css = APP_CSS_PATH.read_text(encoding="utf-8")
    desktop_selector = "body.menu-full .facet-bar::after"
    base_index, base_rule = _rule_block(css, ".facet-bar::after")
    desktop_index, desktop_rule = _rule_block(css, desktop_selector)
    media_index, mobile_block = _media_block(css, LIVE_MOBILE_MENU_MEDIA_ANCHOR)
    mobile_index = mobile_block.find(MOBILE_MENU_FULL_FACET_AFTER_ANCHOR)
    assert mobile_index != -1, "mobile menu-full facet-bar separator rule was not found"
    mobile_selector_start = mobile_index + len("\n  ")
    mobile_selector_end = mobile_block.find(" {", mobile_selector_start)
    assert mobile_selector_end != -1, (
        "mobile menu-full facet-bar selector was not found"
    )
    mobile_selector = mobile_block[mobile_selector_start:mobile_selector_end]
    _, mobile_rule = _rule_block(mobile_block, "  body.menu-full .facet-bar::after")

    assert "left: var(--menu-bar-width-minimal);" in base_rule
    assert "left: var(--menu-bar-width-full);" in desktop_rule
    assert "left: 0;" in mobile_rule
    assert mobile_selector == desktop_selector
    assert _class_specificity(desktop_selector) == 2
    assert base_index < desktop_index
    assert desktop_index < media_index + mobile_index, (
        "mobile menu-full facet-bar separator override must follow the desktop rule; "
        "source order is the mechanism"
    )


def test_live_mobile_reassert_block_contains_facet_separator_left_overrides():
    css = APP_CSS_PATH.read_text(encoding="utf-8")
    _, mobile_block = _media_block(css, LIVE_MOBILE_MENU_MEDIA_ANCHOR)

    bare_index = mobile_block.find(MOBILE_BARE_FACET_AFTER_ANCHOR)
    menu_full_index = mobile_block.find(MOBILE_MENU_FULL_FACET_AFTER_ANCHOR)
    hide_index = mobile_block.find(
        "\n  body:not(.menu-full) .menu-bar .menu-items,\n"
        "  body:not(.menu-full) .menu-bar .menu-expander {"
    )
    assert bare_index != -1, "mobile bare facet-bar separator rule was not found"
    assert menu_full_index != -1, (
        "mobile menu-full facet-bar separator rule was not found"
    )
    assert hide_index != -1, "mobile menu item hiding rule was not found"
    _, bare_rule = _rule_block(mobile_block, "  .facet-bar::after")
    _, menu_full_rule = _rule_block(mobile_block, "  body.menu-full .facet-bar::after")

    assert 0 < bare_index < menu_full_index < hide_index
    assert "left: 0;" in bare_rule
    assert "left: 0;" in menu_full_rule


def test_mobile_facet_separator_overrides_do_not_override_status_pane_right_inset():
    css = APP_CSS_PATH.read_text(encoding="utf-8")
    status_selector = "body:has(.status-pane.visible) .facet-bar::after"
    media_index, mobile_block = _media_block(css, LIVE_MOBILE_MENU_MEDIA_ANCHOR)
    status_index, status_rule = _rule_block(css, status_selector)

    assert not any(
        line.lstrip().startswith("right:") for line in mobile_block.splitlines()
    )
    assert css.count(f"{status_selector} {{") == 1
    assert status_index < media_index
    assert "right: var(--status-pane-width);" in status_rule
