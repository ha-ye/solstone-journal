# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WORKSPACE = Path("solstone/apps/search/workspace.html")
SOURCE = WORKSPACE.read_text(encoding="utf-8")


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace_start = source.index("{", start)
    depth = 0
    in_string: str | None = None
    escaped = False
    for index in range(brace_start, len(source)):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char in {"'", '"', "`"}:
            in_string = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"could not extract {name}")


def test_render_filters_uses_talents_for_agent_sidebar():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    render_filters = _extract_function(SOURCE, "renderFilters")
    script = (
        "let facetInnerHtml = '';\n"
        "let agentInnerHtml = '';\n"
        "const facetList = {\n"
        "  set innerHTML(value) { facetInnerHtml = value; },\n"
        "  get innerHTML() { return facetInnerHtml; },\n"
        "  querySelectorAll() { return []; }\n"
        "};\n"
        "const agentList = {\n"
        "  set innerHTML(value) { agentInnerHtml = value; },\n"
        "  get innerHTML() { return agentInnerHtml; },\n"
        "  querySelectorAll() { return []; }\n"
        "};\n"
        "let currentFacet = '';\n"
        "let currentAgent = '';\n"
        "function escapeHtml(v) { return String(v); }\n"
        "function updateHash() {}\n"
        "function doSearch() {}\n"
        "function assert(c, m) { if (!c) throw new Error(m); }\n"
        f"{render_filters}\n"
        "renderFilters([], [{name:'recap', label:'Recap', icon:'📝', count:3}]);\n"
        "assert(agentList.innerHTML.includes('Recap'), 'talent label should render');\n"
        "assert(agentList.innerHTML.includes('3'), 'talent count should render');\n"
        "renderFilters([], []);\n"
        "assert(agentList.innerHTML.includes('talents appear here'), 'empty talent state should render');\n"
    )

    subprocess.run([node, "-e", script], check=True, text=True)


def test_search_api_results_read_talents_key():
    assert "renderFilters(data.facets, data.talents" in SOURCE
    assert "renderFilters(data.facets, data.agents)" not in SOURCE
