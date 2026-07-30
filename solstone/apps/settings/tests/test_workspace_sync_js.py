# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WORKSPACE = Path("solstone/apps/settings/workspace.html")
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


def test_populate_sync_handles_removed_granola_control_and_sets_obsidian():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    populate_sync = _extract_function(SOURCE, "populateSync")
    script = (
        "const elements = Object.create(null);\n"
        "function makeElement(id) {\n"
        "  elements[id] = { id, style: {}, disabled: false, checked: false };\n"
        "}\n"
        "[\n"
        "  'field-plaud-sync-enabled',\n"
        "  'plaudSyncCard',\n"
        "  'plaudSyncField',\n"
        "  'plaudSyncTokenNote',\n"
        "  'field-obsidian-sync-enabled',\n"
        "].forEach(makeElement);\n"
        "global.document = {\n"
        "  getElementById(id) { return elements[id] || null; }\n"
        "};\n"
        "function assert(condition, message) { if (!condition) throw new Error(message); }\n"
        f"{populate_sync}\n"
        "let threw = false;\n"
        "try {\n"
        "  populateSync({\n"
        "    plaud: { available: true, enabled: true, configured: true },\n"
        "    obsidian: { available: true, enabled: true, configured: true },\n"
        "  });\n"
        "} catch (err) {\n"
        "  threw = true;\n"
        "}\n"
        "assert(!threw, 'populateSync should not throw when Granola control is absent');\n"
        "assert(\n"
        "  elements['field-obsidian-sync-enabled'].checked === true,\n"
        "  'obsidian toggle should reflect saved enabled state'\n"
        ");\n"
    )

    subprocess.run([node, "-e", script], check=True, text=True)
