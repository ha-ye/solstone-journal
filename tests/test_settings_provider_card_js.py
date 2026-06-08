# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from solstone.apps.settings import install_copy

WORKSPACE = Path("solstone/apps/settings/workspace.html")


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


def test_provider_card_helpers_are_coherent():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    source = WORKSPACE.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_function(source, name)
        for name in (
            "formatMlxBytes",
            "localMlxBlockedReason",
            "providerKeyPill",
            "installBadgeLabel",
            "localReadinessBadgeLabel",
            "localReadinessBadgeTone",
            "localReadinessMeta",
            "providerCardKeyFields",
            "providerBadgeTone",
            "providerByteLabel",
            "firstProviderIssue",
            "providerCardMeta",
        )
    )
    install_copy_payload = {
        name: getattr(install_copy, name) for name in install_copy.__all__
    }
    script = (
        f"const INSTALL_COPY = {json.dumps(install_copy_payload)};\n"
        "const IN_FLIGHT_INSTALL_STATES = ['resolving', 'downloading', 'verifying', 'installing'];\n"
        f"{functions}\n"
        "function assert(condition, message) { if (!condition) throw new Error(message); }\n"
        "function assertCoherent(meta) {\n"
        "  assert(!(meta.badgeLabel === 'Ready' && meta.primaryAction === 'install'), 'ready badge must not pair with install action');\n"
        "  assert(!(meta.primaryAction && meta.primaryHref), 'card must not expose two primary action modes');\n"
        "}\n"
        "assert(providerByteLabel('local', 'downloading', 'Downloading', {}) === '', 'absent bytes should suppress label');\n"
        "assert(providerByteLabel('local', 'downloading', 'Downloading', {progress_bytes_received: 4, progress_bytes_total: 0}) === '', 'zero total should suppress label');\n"
        "assert(providerByteLabel('local', 'downloading', 'Downloading', {progress_bytes_received: 12, progress_bytes_total: 10}) === '', 'overrun bytes should suppress label');\n"
        "assert(providerByteLabel('local', 'downloading', 'Downloading', {progress_bytes_received: 4, progress_bytes_total: 10}) === 'Downloading 4 B / 10 B', 'valid bytes should render label');\n"
        "const ready = {status: 'ready', reason_code: 'ready', summary: 'Local is ready', detail: '', recovery_action: null};\n"
        "const loading = {status: 'blocked', reason_code: 'local_model_loading', summary: 'Local model is starting up', detail: 'Try again shortly.', recovery_action: null};\n"
        "const unhealthy = {status: 'unhealthy', reason_code: 'local_server_unhealthy', summary: 'The local model server is not responding.', detail: 'Restart local model setup.', recovery_action: null};\n"
        "const actionView = {status: 'blocked', reason_code: 'ram_insufficient', summary: 'Local model setup needs more memory.', detail: 'Choose another model.', recovery_action: {label: 'Open Settings', href: '/app/settings/#providers'}};\n"
        "let meta = providerCardMeta({install_state: 'downloading'}, 'local', null, ready);\n"
        "assert(meta.badgeLabel === INSTALL_COPY.INSTALL_PHASE_DOWNLOADING, 'in-flight install owns badge');\n"
        "assert(meta.primaryLabel === INSTALL_COPY.INSTALL_BUTTON_INSTALLING && meta.primaryDisabled === true, 'in-flight install owns disabled action');\n"
        "assertCoherent(meta);\n"
        "meta = providerCardMeta({install_state: 'installed'}, 'local', null, ready);\n"
        "assert(meta.badgeLabel === 'Ready' && meta.badgeTone === 'ok', 'installed ready uses readiness badge');\n"
        "assert(!meta.primaryLabel && !meta.primaryAction && !meta.primaryHref, 'installed ready has no action');\n"
        "assertCoherent(meta);\n"
        "meta = providerCardMeta({install_state: 'installed'}, 'local', null, loading);\n"
        "assert(meta.badgeLabel === 'Starting up' && meta.badgeTone === 'progress', 'installed loading uses readiness progress badge');\n"
        "assert(!meta.primaryLabel && !meta.primaryAction && !meta.primaryHref, 'installed loading without recovery has no action');\n"
        "assertCoherent(meta);\n"
        "meta = providerCardMeta({install_state: 'installed'}, 'local', null, unhealthy);\n"
        "assert(meta.badgeLabel === 'Server not responding' && meta.badgeTone === 'warn', 'installed unhealthy uses readiness warning badge');\n"
        "assertCoherent(meta);\n"
        "meta = providerCardMeta({install_state: 'installed'}, 'local', null, actionView);\n"
        "assert(meta.primaryLabel === 'Open Settings' && meta.primaryHref === '/app/settings/#providers', 'readiness recovery uses href action');\n"
        "assert(meta.primaryAction === null, 'readiness recovery must not reuse install action');\n"
        "assertCoherent(meta);\n"
        "meta = providerCardMeta({install_state: 'idle'}, 'local', null, ready);\n"
        "assert(meta.primaryAction === 'install' && meta.badgeLabel !== 'Ready', 'idle install phase owns install action');\n"
        "assertCoherent(meta);\n"
        "meta = providerCardMeta({install_state: 'failed', install_error: 'failed'}, 'local', null, ready);\n"
        "assert(meta.primaryAction === 'install' && meta.primaryLabel === INSTALL_COPY.INSTALL_BUTTON_RETRY, 'failed install phase owns retry action');\n"
        "assertCoherent(meta);\n"
        "meta = providerCardMeta({install_state: 'installed'}, 'local-mlx', null, loading);\n"
        "assert(meta.badgeLabel === INSTALL_COPY.INSTALL_PHASE_INSTALLED, 'MLX branch ignores local readiness view');\n"
    )

    subprocess.run([node, "-e", script], check=True, text=True)


def test_providers_anchor_focuses_panel_and_handles_same_hash():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    source = WORKSPACE.read_text(encoding="utf-8")
    assert 'id="providersPanel" class="providers-panel" tabindex="-1"' in source
    functions = "\n".join(
        _extract_function(source, name)
        for name in (
            "focusProvidersPanel",
            "handleProvidersAnchorClick",
            "switchSection",
        )
    )
    script = (
        "const VALID_SECTIONS = ['profile', 'providers'];\n"
        "let scrollCalls = [];\n"
        "let focusCalls = [];\n"
        "let historyCalls = [];\n"
        "function assert(condition, message) { if (!condition) throw new Error(message); }\n"
        "function classListStub() { return { calls: [], toggle(name, value) { this.calls.push([name, value]); this[name] = value; } }; }\n"
        "const providersPanel = {\n"
        "  scrollIntoView(options) { scrollCalls.push(options); },\n"
        "  focus(options) { focusCalls.push(options); },\n"
        "};\n"
        "const navSelect = { value: 'profile' };\n"
        "const navItems = [\n"
        "  { dataset: { section: 'profile' }, classList: classListStub(), attrs: {}, setAttribute(name, value) { this.attrs[name] = value; } },\n"
        "  { dataset: { section: 'providers' }, classList: classListStub(), attrs: {}, setAttribute(name, value) { this.attrs[name] = value; } },\n"
        "];\n"
        "const sections = [\n"
        "  { id: 'section-profile', classList: classListStub() },\n"
        "  { id: 'section-providers', classList: classListStub() },\n"
        "];\n"
        "global.window = { selectedFacet: null, location: { pathname: '/app/settings/', hash: '#providers' } };\n"
        "global.history = { replaceState(_state, _title, hash) { historyCalls.push(hash); window.location.hash = hash; } };\n"
        "global.document = {\n"
        "  getElementById(id) {\n"
        "    if (id === 'providersPanel') return providersPanel;\n"
        "    if (id === 'navSelect') return navSelect;\n"
        "    throw new Error('unexpected id ' + id);\n"
        "  },\n"
        "  querySelectorAll(selector) {\n"
        "    if (selector === '.settings-nav-item') return navItems;\n"
        "    if (selector === '.settings-section') return sections;\n"
        "    throw new Error('unexpected selector ' + selector);\n"
        "  },\n"
        "};\n"
        f"{functions}\n"
        "switchSection('providers');\n"
        "assert(scrollCalls.length === 1, 'providers switch should scroll panel');\n"
        "assert(scrollCalls[0].behavior === 'smooth' && scrollCalls[0].block === 'start', 'scroll options should match');\n"
        "assert(focusCalls.length === 1 && focusCalls[0].preventScroll === true, 'providers switch should focus panel');\n"
        "assert(navSelect.value === 'providers', 'mobile select should track providers');\n"
        "assert(historyCalls[0] === '#providers', 'providers switch should update hash');\n"
        "assert(navItems[1].classList.active === true && navItems[1].attrs['aria-selected'] === 'true', 'providers tab should activate');\n"
        "assert(sections[1].classList.active === true, 'providers section should activate');\n"
        "let prevented = 0;\n"
        "handleProvidersAnchorClick({\n"
        "  target: { closest(selector) { assert(selector === 'a', 'anchor lookup should be narrow'); return { pathname: '/app/settings/', hash: '#providers' }; } },\n"
        "  preventDefault() { prevented += 1; },\n"
        "});\n"
        "assert(prevented === 1, 'same-document providers link should be intercepted');\n"
        "assert(scrollCalls.length === 2 && focusCalls.length === 2, 'same-hash click should refocus providers panel');\n"
        "handleProvidersAnchorClick({\n"
        "  target: { closest() { return { pathname: '/other', hash: '#providers' }; } },\n"
        "  preventDefault() { prevented += 1; },\n"
        "});\n"
        "assert(prevented === 1, 'different path should not be intercepted');\n"
        "assert(scrollCalls.length === 2 && focusCalls.length === 2, 'different path should not refocus panel');\n"
    )

    subprocess.run([node, "-e", script], check=True, text=True)


def test_context_groups_render_with_type_specific_provider_defaults():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    source = WORKSPACE.read_text(encoding="utf-8")
    start = source.index("function renderContextGroups(")
    end = source.index("\nfunction setupContextEventListeners", start)
    render_context_groups = source[start:end]
    script = (
        f"{render_context_groups}\n"
        "function assert(condition, message) { if (!condition) throw new Error(message); }\n"
        "let rendered = '';\n"
        "global.document = {\n"
        "  getElementById(id) {\n"
        "    assert(id === 'contextGroups', 'unexpected element id ' + id);\n"
        "    return { set innerHTML(value) { rendered = value; }, get innerHTML() { return rendered; } };\n"
        "  },\n"
        "  querySelectorAll() { return []; },\n"
        "};\n"
        "global.setupContextEventListeners = function() {};\n"
        "renderContextGroups({\n"
        "  generate: { provider: 'local', tier: 2, backup: 'google' },\n"
        "  providers: [{ name: 'local', label: 'Local (on-device)' }],\n"
        "  contexts: {},\n"
        "  context_defaults: {\n"
        "    'talent.system.chat': { label: 'Chat', group: 'Think', tier: 2 },\n"
        "    'detect.created': { label: 'Date Detection', group: 'Import', tier: 3 },\n"
        "  },\n"
        "});\n"
        "assert(rendered.includes('Chat'), 'context label should render');\n"
        "assert(rendered.includes('Date Detection'), 'second context label should render');\n"
        "assert(rendered.includes('Local (on-device)'), 'provider option should render');\n"
    )

    subprocess.run([node, "-e", script], check=True, text=True)
