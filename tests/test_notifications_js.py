# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _extract_notifications_object(source: str) -> str:
    marker = "  notifications: {"
    start = source.index(marker) + len("  notifications: ")
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
    raise AssertionError("could not extract notifications service")


def test_keyed_notifications_dedupe_with_work_keys():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    app_js = Path("solstone/convey/static/app.js").read_text(encoding="utf-8")
    notifications_object = _extract_notifications_object(app_js)
    script = (
        "const storage = {};\n"
        "global.localStorage = {\n"
        "  getItem(key) { return storage[key] || '[]'; },\n"
        "  setItem(key, value) { storage[key] = String(value); }\n"
        "};\n"
        "global.window = { AppServices: { escapeHtml(value) { return String(value); } } };\n"
        f"const notifications = ({notifications_object});\n"
        "notifications._render = function() { this.renderCount = (this.renderCount || 0) + 1; };\n"
        "function assert(condition, message) { if (!condition) throw new Error(message); }\n"
        "const first = notifications.show({key: 'provider_key_missing:google:', work_key: 'day/seg/a', title: 'Paused', message: 'first', icon: 'I', action: '/settings'});\n"
        "const second = notifications.show({key: 'provider_key_missing:google:', work_key: 'day/seg/b', title: 'Still paused', message: 'second', icon: 'J', action: '/settings'});\n"
        "assert(first === second, 'same key should return existing id');\n"
        "assert(notifications._stack.length === 1, 'same key should keep one stack entry');\n"
        "assert(notifications.count() === 1, 'badge count should count one active group');\n"
        "assert(notifications._stack[0].count === 2, 'two work keys should count two affected items');\n"
        "assert(notifications._stack[0].badge === '2 segments', 'count badge should render affected count');\n"
        "assert(notifications._stack[0].message === 'second', 'update should refresh message');\n"
        "assert(notifications._history.length === 1, 'key update should not duplicate history');\n"
        "notifications.show({key: 'provider_key_missing:google:', work_key: 'day/seg/b', title: 'Still paused', message: 'third'});\n"
        "assert(notifications._stack[0].count === 2, 'same work key should not double-count');\n"
        "assert(notifications._history.length === 1, 'repeat update should not duplicate history');\n"
        "notifications.show({title: 'Unkeyed', message: 'plain'});\n"
        "assert(notifications._stack.length === 2, 'keyed plus unkeyed should keep two entries');\n"
        "assert(notifications.count() === 2, 'count should include keyed group and unkeyed card');\n"
        "assert(notifications._history.length === 2, 'unkeyed card should append history');\n"
    )

    subprocess.run([node, "-e", script], check=True, text=True)
