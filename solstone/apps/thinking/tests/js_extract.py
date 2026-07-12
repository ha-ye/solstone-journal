# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re


def extract_js_const(source: str, const_name: str) -> str:
    match = re.search(
        rf"  const {re.escape(const_name)} = new Set\(\[[^\]]+\]\);",
        source,
    )
    if match is None:
        raise AssertionError(f"could not extract {const_name}")
    return match.group(0).strip()


def extract_js_function(source: str, function_name: str) -> str:
    markers = [
        f"  function {function_name}",
        f"  async function {function_name}",
    ]
    starts = [source.index(marker) for marker in markers if marker in source]
    if not starts:
        raise AssertionError(f"could not extract {function_name}")
    start = min(starts) + 2
    brace = source.index(") {", start) + 2
    depth = 0
    in_string: str | None = None
    escaped = False
    for index in range(brace, len(source)):
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
