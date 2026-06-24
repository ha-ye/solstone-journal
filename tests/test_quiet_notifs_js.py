# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def _extract_quiet_notifs_iife(source: str) -> str:
    marker = "  quietNotifs: "
    start = source.index(marker) + len(marker)
    terminator = "})()"
    end = source.index(terminator, start) + len(terminator)
    return source[start:end]


def test_quiet_notifs_retains_full_messages_and_caps_count():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    app_js = Path("solstone/convey/static/app.js").read_text(encoding="utf-8")
    quiet_notifs_iife = _extract_quiet_notifs_iife(app_js)
    script = (
        "const storage = {};\n"
        "global.localStorage = {\n"
        "  getItem(key) { return storage[key] || '[]'; },\n"
        "  setItem(key, value) { storage[key] = String(value); }\n"
        "};\n"
        "global.document = { getElementById: () => null };\n"
        "global.window = {};\n"
        f"const store = ({quiet_notifs_iife});\n"
        "function assert(condition, message) { if (!condition) throw new Error(message); }\n"
        "const long = 'x'.repeat(300);\n"
        "store.add({ source: 'svc', message: long, ts: 1 });\n"
        "const retained = store.getAll()[0];\n"
        "assert(retained.message === long, 'quiet notification should retain full message');\n"
        "assert(retained.message.length === 300, 'quiet notification should not truncate to 120 chars');\n"
        "for (let index = 0; index < 25; index++) {\n"
        "  store.add({ source: 'svc', message: `msg-${index}`, ts: index + 2 });\n"
        "}\n"
        "const capped = store.getAll();\n"
        "assert(capped.length === 20, 'quiet notifications should cap at 20 entries');\n"
        "assert(!capped.some(n => n.message === long), 'oldest long message should be evicted');\n"
        "assert(!capped.some(n => n.message === 'msg-0'), 'oldest short message should be evicted');\n"
        "assert(capped[0].message === 'msg-24', 'newest entry should be returned first');\n"
        "assert(capped.some(n => n.message === 'msg-24'), 'last-added message should be present');\n"
        "assert(capped[capped.length - 1].message === 'msg-5', 'oldest retained message should be msg-5');\n"
    )

    subprocess.run([node, "-e", script], check=True, text=True)
