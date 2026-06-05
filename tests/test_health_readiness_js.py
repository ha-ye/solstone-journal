# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from solstone.convey.provider_readiness import semantic_key_for

WORKSPACE = Path("solstone/apps/health/workspace.html")


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


def _extract_readiness_constants(source: str) -> str:
    start = source.index("  const READINESS_SEVERITY_RANK = {")
    end = source.index("  function readinessSeverity", start)
    return source[start:end]


def test_health_readiness_js_semantic_key_glance_and_grouping():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    source = WORKSPACE.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_function(source, name)
        for name in (
            "readinessSeverity",
            "semanticKey",
            "topReadinessGroup",
            "readinessGlance",
            "selectGlanceSentence",
            "recentErrorGroupKey",
            "groupedRecentErrors",
        )
    )
    cases = [
        (
            code,
            provider,
            model,
            semantic_key_for(code, provider, model),
        )
        for code, provider, model in (
            ("provider_key_missing", "anthropic", "claude-test"),
            ("provider_quota_exceeded", "openai", "gpt-test"),
            ("local_model_missing", "local", "qwen-test"),
            ("local_server_unhealthy", "local", "qwen-test"),
            ("chat_timeout", "google", "gemini-test"),
        )
    ]
    script = (
        "let providerReadiness = null;\n"
        "let connectError = false;\n"
        "const STALE_MS = 30000;\n"
        "function relativeTime(ms) { return String(ms) + 'ms'; }\n"
        "function serviceName(name) { return name; }\n"
        "const state = {recentErrors: [], recentErrorsFilter: null};\n"
        "function recentErrorMatchesFilter() { return true; }\n"
        f"{_extract_readiness_constants(source)}\n"
        f"{functions}\n"
        "function assert(condition, message) { if (!condition) throw new Error(message); }\n"
        f"const cases = {json.dumps(cases)};\n"
        "for (const [code, provider, model, expected] of cases) {\n"
        "  assert(semanticKey(code, provider, model) === expected, 'semanticKey parity failed for ' + code);\n"
        "}\n"
        "function baseState() {\n"
        "  return {connected: true, crashed: new Map(), health: {stale_heartbeats: []}, agents: new Map(), imports: new Map(), observers: new Map(), services: new Map([['convey', {}]]), lastEventTs: 1000};\n"
        "}\n"
        "providerReadiness = {summary: {severity: 'blocker'}, groups: [{severity: 'blocker', summary: 'Provider setup needs attention'}]};\n"
        "assert(selectGlanceSentence(baseState(), 2000).key === 'HEALTH_GLANCE_READINESS_BLOCKED', 'blocker readiness should win before observer/catching/ok');\n"
        "providerReadiness = {unavailable: true, summary: {severity: 'neutral'}, groups: []};\n"
        "assert(selectGlanceSentence(baseState(), 2000).key === 'HEALTH_GLANCE_READINESS_UNKNOWN', 'unavailable readiness should prevent OK');\n"
        "providerReadiness = {summary: {severity: 'neutral'}, groups: []};\n"
        "assert(selectGlanceSentence(baseState(), 2000).key === 'HEALTH_GLANCE_OK', 'neutral readiness should allow normal OK');\n"
        "providerReadiness = {summary: {severity: 'blocker'}, groups: [{severity: 'blocker', summary: 'Provider setup needs attention'}]};\n"
        "const serviceState = baseState(); serviceState.crashed.set('sense', {});\n"
        "assert(selectGlanceSentence(serviceState, 2000).key === 'HEALTH_GLANCE_SERVICES_ATTENTION', 'service attention should outrank readiness');\n"
        "state.recentErrors = [\n"
        "  {type: 'agent', reason_code: 'provider_key_missing', provider: 'anthropic', model: 'm1', name: 'daily', error: 'a', ts: 1},\n"
        "  {type: 'agent', reason_code: 'provider_key_missing', provider: 'anthropic', model: 'm2', name: 'daily', error: 'b', ts: 2},\n"
        "  {type: 'agent', reason_code: 'local_model_missing', provider: 'local', model: 'm1', name: 'daily', error: 'c', ts: 3},\n"
        "  {type: 'agent', reason_code: 'local_model_missing', provider: 'local', model: 'm2', name: 'daily', error: 'd', ts: 4},\n"
        "  {type: 'import', service: 'importer', name: 'file', stage: 'read', error: 'same', ts: 5},\n"
        "  {type: 'import', service: 'importer', name: 'file', stage: 'read', error: 'same', ts: 6}\n"
        "];\n"
        "const groups = groupedRecentErrors();\n"
        "const providerGroup = groups.find(group => group.key === 'provider_key_missing:anthropic:');\n"
        "assert(providerGroup && providerGroup.count === 2 && providerGroup.lastTs === 2, 'provider-level errors should group without model');\n"
        "assert(groups.some(group => group.key === 'local_model_missing:local:m1'), 'model-level group should keep model m1');\n"
        "assert(groups.some(group => group.key === 'local_model_missing:local:m2'), 'model-level group should keep model m2');\n"
        "const fallbackGroup = groups.find(group => group.key.startsWith('fallback:import:importer:file:read:same'));\n"
        "assert(fallbackGroup && fallbackGroup.count === 2 && fallbackGroup.lastTs === 6, 'fallback errors should group by service/message');\n"
    )

    subprocess.run([node, "-e", script], check=True, text=True)
