# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEBSOCKET = Path("solstone/convey/static/websocket.js")
WORKSPACE = Path("solstone/apps/entities/workspace.html")


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


def test_entities_cortex_listener_accepts_status_frames():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    websocket_source = WEBSOCKET.read_text(encoding="utf-8")
    html = WORKSPACE.read_text(encoding="utf-8")
    setup_cortex_listener = _extract_function(html, "setupCortexListener")
    script = (
        "globalThis.window = globalThis;\n"
        "const logErrors = [];\n"
        "window.logError = (e, ctx) => logErrors.push({ e, ctx });\n"
        "window.CONVEY_COPY = { RELOAD_HINT: 'reload to retry' };\n"
        "window.AppServices = {};\n"
        "let domReady = null;\n"
        "const fakeDoc = {\n"
        "  readyState: 'loading',\n"
        "  addEventListener: (name, cb) => { if (name === 'DOMContentLoaded') domReady = cb; },\n"
        "  querySelector: () => null\n"
        "};\n"
        "window.document = fakeDoc;\n"
        "globalThis.document = fakeDoc;\n"
        "class FakeEventSource {\n"
        "  constructor(url) { this.url = url; FakeEventSource.last = this; }\n"
        "  close() {}\n"
        "}\n"
        "globalThis.EventSource = FakeEventSource;\n"
        "window.EventSource = FakeEventSource;\n"
        f"{websocket_source}\n"
        "function assert(c, m){ if(!c) throw new Error(m); }\n"
        "assert(typeof domReady === 'function', 'DOMContentLoaded callback not captured');\n"
        "domReady();\n"
        "assert(FakeEventSource.last && typeof FakeEventSource.last.onmessage === 'function', 'EventSource onmessage not wired');\n"
        "const parseErrors = [];\n"
        "window.appEvents.onParseError((err, raw) => parseErrors.push(err));\n"
        "let cortexSub;\n"
        "const pendingEntities = new Map();\n"
        "const pendingAgentCallbacks = new Map();\n"
        "const ENTITIES_CORTEX_STALL_MS = 120000;\n"
        "const updateCalls = [];\n"
        "const completeCalls = [];\n"
        "const failCalls = [];\n"
        "const timeoutCalls = [];\n"
        "function updatePendingEntity(id, entity){ updateCalls.push({id, entity}); }\n"
        "function completePendingEntity(id, result){ completeCalls.push({id, result}); }\n"
        "function failPendingEntity(id, msg){ failCalls.push({id, msg}); }\n"
        "function handleEntitiesTimeout(id){ timeoutCalls.push(id); }\n"
        f"{setup_cortex_listener}\n"
        "setupCortexListener();\n"
        "pendingEntities.set('use-xyz', { name: 'X', element: { dataset: {} } });\n"
        "cortexSub.pending.track('use-xyz');\n"
        "FakeEventSource.last.onmessage({ data: JSON.stringify({ tract:'cortex', event:'status', running_uses:1, uses:[{ use_id:'use-xyz', name:'X', provider:'p', elapsed_seconds:3 }] }) });\n"
        "assert(logErrors.length === 0, 'status frame should not log drop/error');\n"
        "assert(parseErrors.length === 0, 'status frame should not fan out parse error');\n"
        "assert(pendingEntities.has('use-xyz'), 'status frame should leave pending entity state unchanged');\n"
        "assert(updateCalls.length===0 && completeCalls.length===0 && failCalls.length===0, 'status frame should not hit use-tied branches');\n"
        "FakeEventSource.last.onmessage({ data: JSON.stringify({ tract:'cortex', event:'finish', use_id:'use-xyz', result:{ ok:true } }) });\n"
        "assert(completeCalls.length===1 && completeCalls[0].id==='use-xyz', 'finish frame should complete tracked use');\n"
        "assert(logErrors.length===0 && parseErrors.length===0, 'finish frame should not log or parse-error');\n"
        "cortexSub.pending.clearAll();\n"
        "process.exit(0);\n"
    )

    subprocess.run([node, "-e", script], check=True, text=True)
