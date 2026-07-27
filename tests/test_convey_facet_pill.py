# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import shutil
import subprocess
from pathlib import Path

import pytest


def _function_body(text: str, name: str) -> str:
    start = text.index(f"function {name}(")
    nxt = text.index("\n  //", start + 1)
    return text[start:nxt]


def _run_node(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    subprocess.run([node, "-e", script], check=True, text=True)


def _class_specificity(selector: str) -> int:
    return selector.count(".")


def _rule_block(css: str, selector: str, start: int = 0) -> tuple[int, str]:
    index = css.find(f"{selector} {{", start)
    assert index != -1, f"{selector} rule was not found"
    close = css.find("}", index)
    assert close != -1, f"{selector} rule is not closed"
    return index, css[index : close + 1]


def _render_facet_chooser_script(body: str) -> str:
    fn = _function_body(
        Path("solstone/convey/static/app.js").read_text(encoding="utf-8"),
        "renderFacetChooser",
    )
    assert "const badgeSvc = window.AppServices?.badges?.facet;" in fn
    assert fn.count("{") == fn.count("}"), "renderFacetChooser extraction is truncated"
    return f"""
function assert(condition, message) {{ if (!condition) throw new Error(message); }}

let activeFacets = [];
let facetsDisabled = false;
const window = {{
  selectedFacet: null,
  location: {{pathname: '/app/home/'}},
  AppServices: null,
}};

function makeElement(tagName = 'div') {{
  const node = {{
    tagName: tagName.toUpperCase(),
    children: [],
    attributes: {{}},
    dataset: {{}},
    style: {{}},
    className: '',
    textContent: '',
    title: '',
    tabIndex: undefined,
    draggable: false,
    onclick: null,
    parentElement: null,
    appendChild(child) {{
      child.parentElement = this;
      this.children.push(child);
      return child;
    }},
    setAttribute(name, value) {{
      this.attributes[name] = String(value);
    }},
    removeAttribute(name) {{
      delete this.attributes[name];
    }},
    querySelector(selector) {{
      return querySelectorWithin(this, selector);
    }},
  }};
  Object.defineProperty(node, 'innerHTML', {{
    get() {{
      return this._innerHTML || '';
    }},
    set(value) {{
      this._innerHTML = String(value);
      if (value === '') this.children = [];
    }},
  }});
  return node;
}}

function hasClass(node, name) {{
  return String(node.className || '').split(/\\s+/).includes(name);
}}

function descendants(node) {{
  return node.children.flatMap((child) => [child, ...descendants(child)]);
}}

function querySelectorWithin(node, selector) {{
  const items = descendants(node);
  if (selector === '.facet-pill') return items.find((item) => hasClass(item, 'facet-pill')) || null;
  if (selector === '.facet-pill[tabindex="0"]') {{
    return items.find((item) => hasClass(item, 'facet-pill') && item.tabIndex === 0) || null;
  }}
  if (selector === '.facet-filter-status') {{
    return items.find((item) => hasClass(item, 'facet-filter-status')) || null;
  }}
  return null;
}}

const facetPillsContainer = makeElement('div');
const facetBar = makeElement('div');
facetBar.classList = {{
  contains(name) {{
    return name === 'facets-disabled' && facetsDisabled;
  }},
}};

const document = {{
  querySelector(selector) {{
    if (selector === '.facet-pills-container') return facetPillsContainer;
    if (selector === '.facet-bar') return facetBar;
    if (selector === '.facet-filter-status') return querySelectorWithin(facetBar, selector);
    return null;
  }},
  createElement(tagName) {{
    return makeElement(tagName);
  }},
}};

function applyFacetTheme() {{}}
function applyPillStyle() {{}}
function selectFacet() {{}}
function openFacetCreateModal() {{}}

{fn}

{body}
"""


def test_mobile_facet_pills_overflow_override_follows_base_rule():
    css = Path("solstone/convey/static/app.css").read_text(encoding="utf-8")

    base_anchor = ".facet-bar .facet-pills-container {\n  flex: 1;\n  display: flex;"
    mobile_anchor = (
        "@media (max-width: 768px) {\n"
        "  .facet-bar .facet-pills-container {\n"
        "    overflow-x: auto;"
    )
    base_index = css.find(base_anchor)
    mobile_index = css.find(mobile_anchor)

    assert base_index != -1, "base facet-pills container rule was not found"
    assert mobile_index != -1, "mobile facet-pills overflow override was not found"

    base_close = css.find("}", base_index)
    assert base_close != -1, "base facet-pills container rule is not closed"
    assert base_index < css.find("overflow-x: clip", base_index) < base_close
    assert base_index < mobile_index


def test_mobile_facet_pills_block_prevents_pill_compression_below_content_width():
    css = Path("solstone/convey/static/app.css").read_text(encoding="utf-8")

    mobile_anchor = (
        "@media (max-width: 768px) {\n"
        "  .facet-bar .facet-pills-container {\n"
        "    overflow-x: auto;"
    )
    mobile_index = css.find(mobile_anchor)
    assert mobile_index != -1, "mobile facet-pills block was not found"

    depth = 0
    mobile_close = -1
    for index in range(mobile_index, len(css)):
        char = css[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                mobile_close = index
                break

    assert mobile_close != -1, "mobile facet-pills block is not closed"
    mobile_block = css[mobile_index : mobile_close + 1]
    _, container_rule = _rule_block(mobile_block, ".facet-bar .facet-pills-container")
    _, pill_rule = _rule_block(
        mobile_block, ".facet-bar .facet-pills-container .facet-pill"
    )

    assert "overflow-x: auto;" in container_rule
    assert "justify-content: flex-start;" in container_rule
    assert "flex-shrink: 0;" in pill_rule


def test_mobile_facet_chrome_compacts_without_shrinking_touch_targets():
    css = Path("solstone/convey/static/app.css").read_text(encoding="utf-8")

    mobile_anchor = (
        "@media (max-width: 768px) {\n"
        "  .facet-bar .facet-pills-container {\n"
        "    overflow-x: auto;"
    )
    mobile_index = css.find(mobile_anchor)
    assert mobile_index != -1, "mobile facet chrome density block was not found"

    depth = 0
    mobile_close = -1
    for index in range(mobile_index, len(css)):
        char = css[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                mobile_close = index
                break

    assert mobile_close != -1, "mobile facet chrome density block is not closed"
    mobile_block = css[mobile_index : mobile_close + 1]
    _, bar_rule = _rule_block(mobile_block, ".facet-bar")
    _, container_rule = _rule_block(mobile_block, ".facet-bar .facet-pills-container")
    _, pill_rule = _rule_block(
        mobile_block, ".facet-bar .facet-pills-container .facet-pill"
    )
    _, trailing_space_rule = _rule_block(
        mobile_block, ".facet-bar .facet-pills-container::after"
    )

    assert "padding-inline: 8px;" in bar_rule
    assert "gap: 8px;" in bar_rule
    assert "gap: 8px;" in container_rule
    assert "padding-inline: 6px;" in pill_rule
    assert "min-inline-size: 44px;" in pill_rule
    assert 'content: "";' in trailing_space_rule
    assert "flex: 0 0 8px;" in trailing_space_rule


def test_mobile_facet_pill_flex_shrink_override_wins_by_specificity():
    css = Path("solstone/convey/static/app.css").read_text(encoding="utf-8")

    mobile_selector = ".facet-bar .facet-pills-container .facet-pill"
    competing_selector = ".facet-pill"
    mobile_index, mobile_rule = _rule_block(css, mobile_selector)
    competing_anchor = f"\n{competing_selector} {{\n"
    competing_index = css.find(competing_anchor)
    assert competing_index != -1, "unconditional .facet-pill rule was not found"
    competing_close = css.find("}", competing_index)
    assert competing_close != -1, "unconditional .facet-pill rule is not closed"
    competing_rule = css[competing_index + 1 : competing_close + 1]

    assert mobile_selector.endswith(competing_selector)
    assert _class_specificity(mobile_selector) == 3
    assert _class_specificity(competing_selector) == 1
    assert _class_specificity(mobile_selector) > _class_specificity(competing_selector)
    assert mobile_index < competing_index, (
        "source order runs against the mobile flex-shrink override; specificity carries it"
    )
    assert "flex-shrink: 0;" in mobile_rule
    assert "flex-shrink: 1;" in competing_rule


def test_mobile_facet_container_justify_override_wins_by_source_order():
    css = Path("solstone/convey/static/app.css").read_text(encoding="utf-8")

    selector = ".facet-bar .facet-pills-container"
    base_index, base_rule = _rule_block(css, selector)
    mobile_index, mobile_rule = _rule_block(css, selector, base_index + 1)
    base_justify_index = css.find("justify-content: center;", base_index)
    mobile_justify_index = css.find("justify-content: flex-start;", mobile_index)

    assert _class_specificity(selector) == 2
    assert "justify-content: center;" in base_rule
    assert "justify-content: flex-start;" in mobile_rule
    assert base_index < base_justify_index < mobile_index
    assert mobile_index < mobile_justify_index
    assert mobile_justify_index > base_justify_index


def test_base_facet_layout_rules_remain_unchanged():
    css = Path("solstone/convey/static/app.css").read_text(encoding="utf-8")

    base_container = (
        ".facet-bar .facet-pills-container {\n"
        "  flex: 1;\n"
        "  display: flex;\n"
        "  gap: 10px;\n"
        "  align-items: center;\n"
        "  justify-content: center;\n"
        "  overflow-x: clip;\n"
        "  overflow-y: visible;\n"
        "  min-width: 0;  /* Allow container to shrink below content size */\n"
        "}"
    )
    base_pill = (
        ".facet-pill {\n"
        "  appearance: none;\n"
        "  font-family: inherit;\n"
        "  color: inherit;\n"
        "  line-height: inherit;\n"
        "  display: flex;\n"
        "  align-items: center;\n"
        "  padding: 8px 16px;\n"
        "  border-radius: 20px;\n"
        "  background: var(--pill-bg-rest, #f5f5f5);\n"
        "  border: 1px solid var(--facet-border, #e5e0db);\n"
        "  cursor: pointer;\n"
        "  font-size: 15px;\n"
        "  font-weight: 500;\n"
        "  transition: transform 0.2s ease, opacity 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease, background 0.2s ease, color 0.2s ease;\n"
        "  user-select: none;\n"
        "  position: relative;\n"
        "  min-width: 0;        /* Allow pill to shrink below content size */\n"
        "  flex-shrink: 1;      /* Allow shrinking when space is tight */\n"
        "}"
    )

    assert base_container in css
    assert base_pill in css


def test_facet_pill_label_ellipsis_rule_remains_unchanged():
    css = Path("solstone/convey/static/app.css").read_text(encoding="utf-8")

    label_rule = (
        ".facet-pill .label {\n"
        "  overflow: hidden;\n"
        "  text-overflow: ellipsis;\n"
        "  white-space: nowrap;\n"
        "  min-width: 0;        /* Can shrink to zero width */\n"
        "  flex-shrink: 1;\n"
        "  user-select: none;\n"
        "  pointer-events: none;\n"
        "}"
    )

    assert label_rule in css


def test_mobile_flex_shrink_override_is_scoped_to_rendered_facet_row():
    css = Path("solstone/convey/static/app.css").read_text(encoding="utf-8")
    app_js = Path("solstone/convey/static/app.js").read_text(encoding="utf-8")
    fn = _function_body(app_js, "renderFacetChooser")

    override_selector = ".facet-bar .facet-pills-container .facet-pill"
    class_assignment = "pill.className = 'facet-pill';"
    append_call = "facetPillsContainer.appendChild(pill);"
    class_index = fn.find(class_assignment)
    append_index = fn.find(append_call)

    assert f"  {override_selector} {{\n    flex-shrink: 0;" in css
    assert override_selector.startswith(".facet-bar .facet-pills-container ")
    assert app_js.count(class_assignment) == 1
    assert class_index != -1, "renderFacetChooser does not create facet pills"
    assert append_index != -1, "renderFacetChooser does not append facet pills"
    assert class_index < append_index


def test_selected_pill_defers_to_css_for_contrast_safe_treatment():
    fn = _function_body(
        Path("solstone/convey/static/app.js").read_text(encoding="utf-8"),
        "applyPillStyle",
    )

    # The selected pill must not force white-on-color inline; the contrast-safe
    # .selected treatment (soft facet wash + dark ink + facet-hued border) lives
    # in app.css. Inline writes would override the stylesheet and re-break it.
    assert "pill.style.color = 'white';" not in fn
    assert "var(--status-inactive)" not in fn
    # It still marks selection via the class the CSS rule keys on.
    assert "pill.classList.add('selected');" in fn


def test_unselected_pill_reset_and_color_setters_unchanged():
    fn = _function_body(
        Path("solstone/convey/static/app.js").read_text(encoding="utf-8"),
        "applyPillStyle",
    )

    assert "pill.style.setProperty('--pill-color', facet.color);" in fn
    assert "pill.style.setProperty('--pill-bg', hexToRgba(facet.color, 0.2));" in fn
    assert (
        "pill.style.setProperty('--pill-bg-rest', hexToRgba(facet.color, 0.08));" in fn
    )
    assert "pill.style.background = '';" in fn
    assert "pill.style.color = '';" in fn
    assert "pill.style.borderColor = '';" in fn


def test_render_facet_chooser_disabled_returns_without_pills():
    _run_node(
        _render_facet_chooser_script(
            """
activeFacets = [
  {name: 'work', title: 'Work', emoji: 'W'},
  {name: 'personal', title: 'Personal', emoji: 'P'},
];
facetsDisabled = true;
facetPillsContainer.setAttribute('role', 'toolbar');
facetPillsContainer.setAttribute('aria-label', 'facet filter');

renderFacetChooser();

const pills = facetPillsContainer.children.filter((child) => hasClass(child, 'facet-pill'));
assert(pills.length === 0, 'disabled facets should not append facet pills');
assert(facetPillsContainer.attributes['aria-hidden'] === 'true', 'disabled facets should hide container from assistive tech');
assert(!('role' in facetPillsContainer.attributes), 'disabled facets should remove toolbar role');
assert(!('aria-label' in facetPillsContainer.attributes), 'disabled facets should remove toolbar label');
"""
        )
    )


def test_render_facet_chooser_enabled_populated_renders_pills():
    _run_node(
        _render_facet_chooser_script(
            """
activeFacets = [
  {name: 'work', title: 'Work', emoji: 'W'},
  {name: 'personal', title: 'Personal', emoji: 'P'},
];
facetsDisabled = false;
facetPillsContainer.setAttribute('aria-hidden', 'true');

renderFacetChooser();

const pills = facetPillsContainer.children.filter((child) => hasClass(child, 'facet-pill'));
assert(!('aria-hidden' in facetPillsContainer.attributes), 'enabled facets should remove aria-hidden');
assert(facetPillsContainer.attributes.role === 'toolbar', 'enabled facets should render a toolbar');
assert(facetPillsContainer.attributes['aria-label'] === 'facet filter', 'enabled facets should label the toolbar');
assert(pills.length === activeFacets.length, 'enabled facets should render one pill per facet');
assert(pills.map((pill) => pill.children.at(-1).textContent).join(',') === 'Work,Personal', 'pills should render facet labels');
"""
        )
    )
