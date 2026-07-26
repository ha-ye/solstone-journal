# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Mapping

import pytest

from scripts.build_convey_icons import (
    CONVEY_ICON_NAMES,
    load_lucide_icons,
    render_convey_icons_js,
    selected_icons,
)

ROOT = Path(__file__).resolve().parents[1]
CONVEY_ICONS_JS = ROOT / "solstone" / "convey" / "static" / "convey_icons.js"

CONVERTED_FILES = (
    ROOT / "solstone" / "apps" / "search" / "workspace.html",
    ROOT / "solstone" / "apps" / "sol" / "workspace.html",
    ROOT / "solstone" / "apps" / "import" / "workspace.html",
    ROOT / "solstone" / "apps" / "support" / "workspace.html",
    ROOT / "solstone" / "apps" / "support" / "static" / "support.js",
    ROOT / "solstone" / "apps" / "stats" / "workspace.html",
    ROOT / "solstone" / "apps" / "stats" / "static" / "dashboard.js",
    ROOT / "solstone" / "apps" / "activities" / "workspace.html",
    ROOT / "solstone" / "apps" / "transcripts" / "workspace.html",
    ROOT / "solstone" / "apps" / "speakers" / "workspace.html",
)

CALL_RE = re.compile(
    r"window\.ConveyIcons\??\.svg\(\s*(?P<q>['\"`])(?P<name>[a-z0-9-]+)(?P=q)\s*\)"
)
SVG_RE = re.compile(r"<svg\b[\s\S]*?</svg>")
L2_ICON_SLOT_CLASSES = (
    "icon-slot",
    "entity-delete-btn",
    "voiceprint-icon",
    "facet-rel-voiceprint",
    "btn-icon",
    "vitals-chip",
    "trust-indicator",
    "link-hero-icon",
    "col-activity",
    "activity-item",
    "summary-item",
)
ICON_SLOT_CLASSES = (
    "surface-state-icon",
    "empty-icon",
    *L2_ICON_SLOT_CLASSES,
)
ICON_SLOT_CLASS_PATTERN = "|".join(re.escape(cls) for cls in ICON_SLOT_CLASSES)
ICON_SLOT_OPEN_RE = re.compile(
    r"<(?P<tag>[A-Za-z][A-Za-z0-9:-]*)\b"
    r"(?=[^>]*\bclass=(?P<quote>['\"])[^'\"]*"
    rf"(?<![A-Za-z0-9_-])(?:{ICON_SLOT_CLASS_PATTERN})(?![A-Za-z0-9_-])"
    r"[^'\"]*(?P=quote))"
    r"[^>]*>",
    re.IGNORECASE,
)
EMPTY_ICONS_MAP_RE = re.compile(r"const\s+emptyIcons\s*=\s*\{[\s\S]*?\n\s*\};")

CONVERTED_GLYPH_RESIDUE = {
    "solstone/apps/search/workspace.html": (
        'aria-hidden="true">🔍</div>',
        "icon: '🔍'",
    ),
    "solstone/apps/sol/workspace.html": (
        '<div class="empty-state-icon">🤖</div>',
        '<div class="empty-state-icon">⚠️</div>',
    ),
    "solstone/apps/import/workspace.html": (
        '<div class="no-imports-icon">📥</div>',
        '<div class="no-imports-icon">🔍</div>',
    ),
    "solstone/apps/support/static/support.js": (
        '<div class="support-empty-icon">🛟</div>',
        '<div class="support-empty-icon">⚠️</div>',
        '<div class="support-empty-icon">⋯</div>',
    ),
    "solstone/apps/support/workspace.html": (
        '<div class="support-empty-icon">⚠️</div>',
        '<div class="support-empty-icon">⋯</div>',
    ),
    "solstone/apps/stats/static/dashboard.js": (
        "['📊']",
        "|| '📊'",
        "['🎙️']",
        "emptyIcon: '🏷️'",
        "emptyIcon: '⚡'",
    ),
    "solstone/apps/activities/workspace.html": (
        '<div class="timeline-empty"><div class="empty-icon"><svg',
    ),
    "solstone/apps/transcripts/workspace.html": (
        '<div class="surface-state-icon" aria-hidden="true"><svg',
        "day: '<svg",
        "nothing: '<svg",
        "transcript: '<svg",
        "audio: '<svg",
        "screen: '<svg",
        "signals: '<svg",
    ),
    "solstone/apps/speakers/workspace.html": (
        '<div class="surface-state-icon" aria-hidden="true"><svg',
        "segment: '<svg",
        "cursor: '<svg",
        "people: '<svg",
        "text: '<svg",
        "audio: '<svg",
    ),
}

OUT_OF_SCOPE_GLYPHS = {
    "solstone/apps/support/workspace.html": (
        "<h3>🛟 getting help</h3>",
        "<h3>🔍 search the knowledge base</h3>",
        "const icons = {'known-issue': '⚠️', 'maintenance': '🔧', 'info': '📢'};",
    ),
    "solstone/apps/import/workspace.html": (
        '<h3 class="import-guide-title">⚡ quick import</h3>',
    ),
}


def _node_or_skip() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    return node


def _generated_icons_from_js(source: str) -> dict[str, str]:
    prefix = "  const ICONS = Object.freeze("
    start = source.index(prefix) + len(prefix)
    end = source.index("\n  });", start) + len("\n  }")
    return json.loads(source[start:end])


def _assert_icon_maps_match(actual: dict[str, str], expected: dict[str, str]) -> None:
    assert actual.keys() == expected.keys()
    mismatched = [
        name for name in sorted(expected) if actual.get(name) != expected[name]
    ]
    assert not mismatched, "mismatched Lucide icon(s): " + ", ".join(mismatched)


def _icon_slot_svgs(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    slots: list[str] = []
    for match in ICON_SLOT_OPEN_RE.finditer(text):
        tag = match.group("tag")
        close = re.search(
            rf"</{re.escape(tag)}\s*>", text[match.end() :], re.IGNORECASE
        )
        if close is None:
            continue
        slot_body = text[match.end() : match.end() + close.start()]
        slots.extend(svg.group(0) for svg in SVG_RE.finditer(slot_body))

    # Regression tripwire: the removed emptyIcons maps are icon sources too.
    for match in EMPTY_ICONS_MAP_RE.finditer(text):
        slots.extend(svg.group(0) for svg in SVG_RE.finditer(match.group(0)))
    return slots


def _converted_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in CONVERTED_FILES)


def requested_icon_names(source: str) -> set[str]:
    return {match.group("name") for match in CALL_RE.finditer(source)}


def _inline_slot_icon_names(path: Path, svg_to_name: Mapping[str, str]) -> set[str]:
    return {svg_to_name[svg] for svg in _icon_slot_svgs(path) if svg in svg_to_name}


def test_convey_icons_runtime_accessor_in_browser_vm():
    node = _node_or_skip()
    script = textwrap.dedent(
        """
        const assert = require('assert');
        const fs = require('fs');
        const vm = require('vm');
        const source = fs.readFileSync(process.argv[1], 'utf8');
        const window = {};
        const context = { window };
        vm.createContext(context);
        vm.runInContext(source, context);
        assert(window.ConveyIcons);
        const names = JSON.parse(process.argv[2]);
        for (const name of names) {
          const svg = window.ConveyIcons.svg(name);
          assert(svg && svg.includes('<svg'), name + ' did not return SVG markup');
        }
        assert.strictEqual(window.ConveyIcons.svg('not-a-real-icon'), '');
        assert.doesNotThrow(() => window.ConveyIcons.svg(null));
        """
    )
    subprocess.run(
        [node, "-e", script, str(CONVEY_ICONS_JS), json.dumps(CONVEY_ICON_NAMES)],
        check=True,
        text=True,
    )


def test_convey_icons_match_lucide_and_generated_output():
    expected = selected_icons(load_lucide_icons())
    source = CONVEY_ICONS_JS.read_text(encoding="utf-8")
    _assert_icon_maps_match(_generated_icons_from_js(source), expected)
    assert source == render_convey_icons_js(expected)


def test_convey_icon_comparator_rejects_mismatched_svg():
    expected = {"search": "<svg>right</svg>"}
    actual = {"search": "<svg>wrong</svg>"}
    with pytest.raises(AssertionError, match="mismatched Lucide icon"):
        _assert_icon_maps_match(actual, expected)


def test_icon_slot_detector_finds_multiline_slot_svg(tmp_path: Path):
    fixture = tmp_path / "workspace.html"
    fixture.write_text(
        textwrap.dedent(
            """
            <div class="surface-state-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24"><path d="M0 0h24v24H0z"></path></svg>
            </div>
            """
        ),
        encoding="utf-8",
    )

    assert _icon_slot_svgs(fixture) == [
        '<svg viewBox="0 0 24 24"><path d="M0 0h24v24H0z"></path></svg>'
    ]


def test_convey_icon_call_pattern_extracts_supported_literals_and_unknowns():
    source = "\n".join(
        (
            "window.ConveyIcons.svg('bell')",
            'window.ConveyIcons.svg("bell-off")',
            "window.ConveyIcons.svg(`trash-2`)",
            "window.ConveyIcons?.svg('sparkles')",
        )
    )
    assert requested_icon_names(source) == {"bell", "bell-off", "trash-2", "sparkles"}

    requested = requested_icon_names("window.ConveyIcons?.svg('not-real')")
    assert requested - set(CONVEY_ICON_NAMES) == {"not-real"}


def test_convey_icon_call_sites_are_allow_listed_and_complete():
    requested = requested_icon_names(_converted_source())
    svg_to_name = {svg: name for name, svg in load_lucide_icons().items()}
    inline = set()
    for path in CONVERTED_FILES:
        inline.update(_inline_slot_icon_names(path, svg_to_name))
    used = requested | inline
    allow_list = set(CONVEY_ICON_NAMES)
    assert used <= allow_list
    assert allow_list <= used


def test_convey_icon_slots_have_no_unvouched_inline_svg():
    lucide_values = set(load_lucide_icons().values())
    offenders = []
    for path in CONVERTED_FILES:
        for svg in _icon_slot_svgs(path):
            if svg not in lucide_values:
                offenders.append(f"{path.relative_to(ROOT)}: {svg[:80]}")
    assert not offenders, "unvouched icon-slot SVG(s): " + "; ".join(offenders)


def test_converted_glyphs_are_gone_and_out_of_scope_glyphs_survive():
    for rel_path, snippets in CONVERTED_GLYPH_RESIDUE.items():
        text = (ROOT / rel_path).read_text(encoding="utf-8")
        for snippet in snippets:
            assert snippet not in text, f"converted glyph residue remains: {rel_path}"

    for rel_path, snippets in OUT_OF_SCOPE_GLYPHS.items():
        text = (ROOT / rel_path).read_text(encoding="utf-8")
        for snippet in snippets:
            assert snippet in text, f"out-of-scope glyph changed: {rel_path}"


def test_surface_state_error_actions_are_wired_after_conversion():
    support_js = (
        ROOT / "solstone" / "apps" / "support" / "static" / "support.js"
    ).read_text(encoding="utf-8")
    sol = (ROOT / "solstone" / "apps" / "sol" / "workspace.html").read_text(
        encoding="utf-8"
    )
    support = (ROOT / "solstone" / "apps" / "support" / "workspace.html").read_text(
        encoding="utf-8"
    )

    assert re.search(
        r"const retryBtn = list\.querySelector\('\.surface-state-retry'\);\s*"
        r"if \(retryBtn\) retryBtn\.addEventListener\('click', \(\) => "
        r"loadTickets\(deps\)\);",
        support_js,
    )
    assert re.search(
        r"loadingView\.querySelector\('\.surface-state-retry'\)\.onclick = "
        r"\(\) => loadTalents\(\);",
        sol,
    )
    assert re.search(
        r"const errorBackBtn = detail\.querySelector\('\.surface-state-secondary'\);\s*"
        r"if \(errorBackBtn\) errorBackBtn\.addEventListener\('click', \(\) => \{\s*"
        r"detail\.classList\.remove\('active'\);\s*"
        r"detail\.innerHTML = '';\s*"
        r"list\.style\.display = '';\s*"
        r"\}\);",
        support,
    )
