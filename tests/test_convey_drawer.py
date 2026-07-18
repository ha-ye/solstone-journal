# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from pathlib import Path

STATIC_ROOT = Path("solstone/convey/static")
BODY_WORKSPACE = Path("solstone/apps/body/workspace.html")


def _owner_regions(source: str) -> list[str]:
    return re.findall(
        r"// --- owner-facing strings ---\n(.*?)// --- end owner-facing strings ---",
        source,
        flags=re.DOTALL,
    )


def test_drawer_static_headers_match_day_grid_byte_for_byte():
    assert (STATIC_ROOT / "drawer.js").read_bytes().splitlines(keepends=True)[:2] == (
        STATIC_ROOT / "day-grid.js"
    ).read_bytes().splitlines(keepends=True)[:2]
    assert (STATIC_ROOT / "drawer.css").read_bytes().splitlines(keepends=True)[:2] == (
        STATIC_ROOT / "day-grid.css"
    ).read_bytes().splitlines(keepends=True)[:2]


def test_drawer_shell_links_follow_day_grid():
    source = (STATIC_ROOT / "shell.html").read_text(encoding="utf-8")

    assert source.index("/static/day-grid.css") < source.index("/static/drawer.css")
    assert source.index("/static/day-grid.js") < source.index("/static/drawer.js")
    assert source.index("/static/drawer.js") < source.index("/static/shell_boot.js")


def test_drawer_js_contract_and_constraints():
    source = (STATIC_ROOT / "drawer.js").read_text(encoding="utf-8")

    assert "(function () {" in source
    assert "'use strict';" in source
    assert "window.Drawer = Object.freeze({ render, preserveOpen });" in source
    assert "Storage" not in source
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "aria-" not in source


def test_drawer_owner_facing_region_is_empty_and_clean():
    source = (STATIC_ROOT / "drawer.js").read_text(encoding="utf-8")
    regions = _owner_regions(source)
    banned = {"capture", "watch", "record", "monitor", "track", "collect", "user"}

    assert regions == ["  "]
    for region in regions:
        lowered = region.lower()
        assert region == lowered
        assert {word for word in banned if word in lowered} == set()


def test_drawer_css_has_no_generated_owner_text_or_user_select():
    source = (STATIC_ROOT / "drawer.css").read_text(encoding="utf-8")

    assert "user-select" not in source
    generated = re.findall(r"content\s*:\s*([^;]+);", source)
    assert all(value.strip() in {'""', "''"} for value in generated)


def test_drawer_css_classes_have_consumers():
    css = (STATIC_ROOT / "drawer.css").read_text(encoding="utf-8")
    consumers = "\n".join(
        [
            (STATIC_ROOT / "drawer.js").read_text(encoding="utf-8"),
            (STATIC_ROOT / "tests" / "drawer.html").read_text(encoding="utf-8"),
            BODY_WORKSPACE.read_text(encoding="utf-8"),
        ]
    )
    classes = set(re.findall(r"\.(drawer(?:-[A-Za-z0-9_]+)*|ev-meta)\b", css))

    assert classes
    assert {name for name in classes if name not in consumers} == set()


def test_drawer_smoke_harness_covers_contract():
    source = (STATIC_ROOT / "tests" / "drawer.html").read_text(encoding="utf-8")

    assert '<link rel="stylesheet" href="../drawer.css">' in source
    assert '<script src="../drawer.js"></script>' in source
    assert "function assert(name, condition, detail)" in source
    assert "function equal(name, actual, expected)" in source
    assert "data-drawer-id" in source
    assert "drawer-chev" in source
    assert "drawer-summary-text" in source
    assert "drawer-body" in source
    assert "drawer-chip--warn" in source
    assert "drawer-chip--danger" in source
    assert "emphasized line text" in source
    assert "digit runs emphasized" in source
    assert "label has no emphasis" in source
    assert "chip has no emphasis" in source
    assert "prose line has no emphasis" in source
    assert "no line omits line span" in source
    assert "preserve restores open id" in source
    assert "vanished id is clean no-op" in source
