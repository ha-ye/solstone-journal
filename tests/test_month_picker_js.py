# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _extract_js_function(source: str, function_name: str) -> str:
    marker = f"  async function {function_name}(ym) {{"
    start = source.index(marker) + 2
    depth = 0
    in_string: str | None = None
    escaped = False
    for index in range(start, len(source)):
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
    raise AssertionError(f"could not extract {function_name}")


def test_month_picker_get_month_data_dedupes_inflight_requests() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    source = Path("solstone/convey/static/month-picker.js").read_text(encoding="utf-8")
    get_month_data = _extract_js_function(source, "getMonthData")
    script = (
        "const cache = {};\n"
        "const inflight = {};\n"
        "global.window = { selectedFacet: null };\n"
        "function logMonthStatsError(_error) {}\n"
        "let fetchCalls = 0;\n"
        "const pending = [];\n"
        "async function fetchMonthData(ym, facet) {\n"
        "  fetchCalls += 1;\n"
        "  return new Promise((resolve, reject) => {\n"
        "    pending.push({ ym, facet, resolve, reject });\n"
        "  });\n"
        "}\n"
        f"{get_month_data}\n"
        "function assert(condition, message) { if (!condition) throw new Error(message); }\n"
        "async function main() {\n"
        "  const first = getMonthData('202603');\n"
        "  const second = getMonthData('202603');\n"
        "  assert(fetchCalls === 1, 'same month/facet should share one fetch');\n"
        "  assert(pending.length === 1, 'dedup should create one pending fetch');\n"
        "  pending.shift().resolve({ data: { '20260304': 6 }, error: null });\n"
        "  const [r1, r2] = await Promise.all([first, second]);\n"
        "  assert(r1 === r2, 'deduped callers should receive same object');\n"
        "  assert(r1.data['20260304'] === 6, 'resolved data should be cached');\n"
        "\n"
        "  const rejected = getMonthData('202604');\n"
        "  assert(fetchCalls === 2, 'reject case should start a fetch');\n"
        "  pending.shift().reject(new Error('boom'));\n"
        "  await rejected.catch(() => {});\n"
        "  const retried = getMonthData('202604');\n"
        "  assert(fetchCalls === 3, 'rejected inflight should clear for retry');\n"
        "  pending.shift().resolve({ data: { '20260401': 1 }, error: null });\n"
        "  await retried;\n"
        "\n"
        "  window.selectedFacet = 'a';\n"
        "  const facetA = getMonthData('202605');\n"
        "  window.selectedFacet = 'b';\n"
        "  const facetB = getMonthData('202605');\n"
        "  assert(fetchCalls === 5, 'different facets should not share inflight fetches');\n"
        "  assert(pending.length === 2, 'facet isolation should leave two pending fetches');\n"
        "  const pendingA = pending.shift();\n"
        "  const pendingB = pending.shift();\n"
        "  assert(pendingA.facet === 'a', 'first facet should be a');\n"
        "  assert(pendingB.facet === 'b', 'second facet should be b');\n"
        "  pendingA.resolve({ data: { a: 1 }, error: null });\n"
        "  pendingB.resolve({ data: { b: 1 }, error: null });\n"
        "  await Promise.all([facetA, facetB]);\n"
        "  console.log('PASS');\n"
        "}\n"
        "main().catch(error => { console.error(error.stack || error); process.exit(1); });\n"
    )

    result = subprocess.run(
        [node, "-e", script],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "PASS" in result.stdout
