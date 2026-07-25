# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

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

CALL_RE = re.compile(r"window\.ConveyIcons\.svg\(\s*'([a-z0-9-]+)'\s*\)")
SVG_RE = re.compile(r"<svg\b[\s\S]*?</svg>")

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
    empty_icons_start = text.find("const emptyIcons = {")
    empty_icons_end = text.find("\n  };", empty_icons_start)
    for match in SVG_RE.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]
        in_empty_icons = (
            empty_icons_start != -1
            and empty_icons_start <= match.start() <= empty_icons_end
        )
        if (
            "surface-state-icon" in line
            or 'class="empty-icon"' in line
            or 'class=\\"empty-icon\\"' in line
            or in_empty_icons
        ):
            slots.append(match.group(0))
    return slots


def _converted_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in CONVERTED_FILES)


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


def test_convey_icon_call_sites_are_allow_listed_and_complete():
    requested = set(CALL_RE.findall(_converted_source()))
    allow_list = set(CONVEY_ICON_NAMES)
    assert requested <= allow_list
    assert allow_list <= requested


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

    assert ".surface-state-retry" in support_js
    assert "loadTickets(deps)" in support_js
    assert ".surface-state-retry" in sol
    assert "loadTalents" in sol
    assert ".surface-state-secondary" in support
    assert "detail.classList.remove('active');" in support
    assert "detail.innerHTML = '';" in support
    assert "list.style.display = '';" in support
