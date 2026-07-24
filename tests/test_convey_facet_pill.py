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
