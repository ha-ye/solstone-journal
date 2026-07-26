# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
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


def _run_notifications_script(body: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")

    app_js = Path("solstone/convey/static/app.js").read_text(encoding="utf-8")
    notifications_object = _extract_notifications_object(app_js)
    script = (
        "const assert = require('assert');\n"
        "const fs = require('fs');\n"
        "const vm = require('vm');\n"
        f"const notificationsSource = {json.dumps(notifications_object)};\n"
        "const storage = {};\n"
        "const warnings = [];\n"
        "function escapeHtml(value) {\n"
        "  return String(value ?? '')\n"
        "    .replace(/&/g, '&amp;')\n"
        "    .replace(/</g, '&lt;')\n"
        "    .replace(/>/g, '&gt;')\n"
        "    .replace(/\"/g, '&quot;')\n"
        "    .replace(/'/g, '&#39;');\n"
        "}\n"
        "const window = { AppServices: { escapeHtml }, addEventListener() {} };\n"
        "const localStorage = {\n"
        "  getItem(key) { return storage[key] || '[]'; },\n"
        "  setItem(key, value) { storage[key] = String(value); }\n"
        "};\n"
        "const never = { then() { return never; }, catch() { return never; } };\n"
        "const context = {\n"
        "  window,\n"
        "  localStorage,\n"
        "  console: {\n"
        "    warn(...args) { warnings.push(args.map(String).join(' ')); },\n"
        "    debug() {},\n"
        "    error() {},\n"
        "    log() {}\n"
        "  },\n"
        "  Date,\n"
        "  JSON,\n"
        "  String,\n"
        "  Number,\n"
        "  Object,\n"
        "  Array,\n"
        "  Set,\n"
        "  Map,\n"
        "  Math,\n"
        "  setTimeout() { return 0; },\n"
        "  clearTimeout() {},\n"
        "  setInterval() { return 0; },\n"
        "  clearInterval() {},\n"
        "  fetch() { return never; }\n"
        "};\n"
        "context.global = context;\n"
        "context.globalThis = context;\n"
        "vm.createContext(context);\n"
        "vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context, { filename: 'convey_icons.js' });\n"
        "const notifications = vm.runInContext('(' + notificationsSource + ')', context, { filename: 'notifications-object.js' });\n"
        "window.AppServices.notifications = notifications;\n"
        "function assertNoPayload(html) {\n"
        "  const text = String(html);\n"
        "  assert(!text.includes('<img'), 'raw image tag should not render');\n"
        "  assert(!/<[^>]*\\sonerror\\s*=/.test(text), 'raw event handler should not render');\n"
        "}\n"
        "function makeElement(tagName = 'div') {\n"
        "  const el = {\n"
        "    tagName: String(tagName).toUpperCase(),\n"
        "    attributes: {},\n"
        "    children: [],\n"
        "    listeners: {},\n"
        "    style: {},\n"
        "    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },\n"
        "    _innerHTML: '',\n"
        "    _textContent: '',\n"
        "    set innerHTML(value) { this._innerHTML = String(value ?? ''); },\n"
        "    get innerHTML() { return this._innerHTML; },\n"
        "    set textContent(value) { this._textContent = String(value ?? ''); },\n"
        "    get textContent() { return this._textContent; },\n"
        "    setAttribute(name, value) { this.attributes[name] = String(value); },\n"
        "    getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; },\n"
        "    addEventListener(type, fn) { this.listeners[type] = fn; },\n"
        "    appendChild(child) { this.children.push(child); return child; },\n"
        "    removeChild(child) { this.children = this.children.filter(item => item !== child); },\n"
        "    querySelector() { return null; },\n"
        "    querySelectorAll() { return []; },\n"
        "    focus() {},\n"
        "    contains() { return false; }\n"
        "  };\n"
        "  return el;\n"
        "}\n" + body
    )

    subprocess.run(
        [
            node,
            "-e",
            script,
            "solstone/convey/static/convey_icons.js",
            "solstone/convey/static/status_pane.js",
        ],
        check=True,
        text=True,
    )


def test_keyed_notifications_dedupe_with_work_keys():
    _run_notifications_script(
        "notifications._render = function() { this.renderCount = (this.renderCount || 0) + 1; };\n"
        "const first = notifications.show({key: 'provider_key_missing:google:', work_key: 'day/seg/a', title: 'Paused', message: 'first', icon: 'mailbox', action: '/settings'});\n"
        "const second = notifications.show({key: 'provider_key_missing:google:', work_key: 'day/seg/b', title: 'Still paused', message: 'second', icon: 'triangle-alert', action: '/settings'});\n"
        "assert(first === second, 'same key should return existing id');\n"
        "assert(notifications._stack.length === 1, 'same key should keep one stack entry');\n"
        "assert(notifications.count() === 1, 'badge count should count one active group');\n"
        "assert(notifications._stack[0].count === 2, 'two work keys should count two affected items');\n"
        "assert(notifications._stack[0].badge === '2 segments', 'count badge should render affected count');\n"
        "assert(notifications._stack[0].message === 'second', 'update should refresh message');\n"
        "assert(notifications._stack[0].icon === 'triangle-alert', 'key update should refresh icon name');\n"
        "assert(notifications._history.length === 1, 'key update should not duplicate history');\n"
        "notifications.show({key: 'provider_key_missing:google:', work_key: 'day/seg/b', title: 'Still paused', message: 'third'});\n"
        "assert(notifications._stack[0].count === 2, 'same work key should not double-count');\n"
        "assert(notifications._stack[0].icon === 'triangle-alert', 'omitted update icon should keep existing icon');\n"
        "assert(notifications._history.length === 1, 'repeat update should not duplicate history');\n"
        "notifications.show({title: 'Unkeyed', message: 'plain'});\n"
        "assert(notifications._stack.length === 2, 'keyed plus unkeyed should keep two entries');\n"
        "assert(notifications._stack[1].icon === 'mailbox', 'unkeyed card should use default icon name');\n"
        "assert(notifications.count() === 2, 'count should include keyed group and unkeyed card');\n"
        "assert(notifications._history.length === 2, 'unkeyed card should append history');\n"
    )


def test_notification_icons_resolve_safely_and_browser_payload_has_no_icon():
    _run_notifications_script(
        "notifications._render = function() {};\n"
        "const payload = '<img src=x onerror=alert(1)>';\n"
        "const badId = notifications.show({title: 'Bad icon', message: 'fallback', icon: payload});\n"
        "assert(badId === 1, 'bad icon should still create a card');\n"
        "assert(notifications._stack[0].icon === 'mailbox', 'bad icon should normalize to mailbox');\n"
        "assert(notifications._history[0].icon === 'mailbox', 'bad icon history should store mailbox');\n"
        "assert(warnings.length === 1, 'bad icon should warn once per emission');\n"
        "assert(warnings[0].includes(payload), 'warning should name bad value');\n"
        "context.document = { createElement: makeElement };\n"
        "const card = notifications._createCard({id: 99, app: 'system', icon: payload, title: 'Injected', message: '', action: null, facet: null, dismissible: true, badge: null, timestamp: Date.now(), lastSeen: Date.now(), autoDismiss: null, buttons: []});\n"
        "assertNoPayload(card.innerHTML);\n"
        "assert(card.innerHTML.includes(notifications._iconSvgByName.mailbox), 'card should show mailbox fallback');\n"
        "const iconEl = { innerHTML: '' };\n"
        "const fakeCard = { style: {}, onclick: null, querySelector(selector) { return selector === '.notification-app-icon' ? iconEl : null; } };\n"
        "notifications._updateCard(fakeCard, {id: 2, icon: 'triangle-alert', title: 'Updated', message: '', badge: null, action: null, facet: null, buttons: [], timestamp: Date.now(), lastSeen: Date.now()});\n"
        "assert(iconEl.innerHTML === notifications._iconSvgByName['triangle-alert'], 'update should refresh card icon');\n"
        "const browserCalls = [];\n"
        "function FakeNotification(title, options) { browserCalls.push({title, options}); }\n"
        "FakeNotification.permission = 'granted';\n"
        "context.Notification = FakeNotification;\n"
        "window.Notification = FakeNotification;\n"
        "notifications.show({app: 'system', title: 'Browser title', message: 'Browser body', icon: 'triangle-alert'});\n"
        "assert(browserCalls.length === 1, 'browser notification should still be delivered');\n"
        "assert(browserCalls[0].title === 'Browser title', 'browser title should be delivered');\n"
        "assert(browserCalls[0].options.body === 'Browser body', 'browser body should be delivered');\n"
        "assert(browserCalls[0].options.tag === 'system-2', 'browser tag should be delivered');\n"
        "assert(!Object.prototype.hasOwnProperty.call(browserCalls[0].options, 'icon'), 'browser notification should not receive icon');\n"
    )


def test_notification_badge_escapes_initial_card_markup():
    _run_notifications_script(
        "context.document = { createElement: makeElement };\n"
        "const payload = '<img src=x onerror=alert(1)>';\n"
        "const card = notifications._createCard({id: 7, app: 'system', icon: 'mailbox', title: 'Badge', message: '', action: null, facet: null, dismissible: true, badge: payload, timestamp: Date.now(), lastSeen: Date.now(), autoDismiss: null, buttons: []});\n"
        "assertNoPayload(card.innerHTML);\n"
        "assert(!card.innerHTML.includes(payload), 'raw badge payload should not render');\n"
        "assert(card.innerHTML.includes('&lt;img src=x onerror=alert(1)&gt;'), 'badge should render escaped payload');\n"
    )


def test_legacy_history_icons_render_as_vouched_status_pane_svgs():
    _run_notifications_script(
        "const statusIcon = makeElement('button');\n"
        "const statusPane = makeElement('aside');\n"
        "const historyContainer = makeElement('div');\n"
        "const ids = {\n"
        "  'notification-history': historyContainer,\n"
        "  'status-pane-console-link': makeElement('a'),\n"
        "  'status-live-region': makeElement('div'),\n"
        "  'status-sentence': makeElement('div'),\n"
        "  'status-detail': makeElement('div'),\n"
        "  'ws-status-raw': makeElement('div'),\n"
        "  'ws-uptime-raw': makeElement('div'),\n"
        "  'ws-last-message-raw': makeElement('div')\n"
        "};\n"
        "context.document = {\n"
        "  querySelector(selector) { if (selector === '.facet-bar .status-icon') return statusIcon; if (selector === '.status-pane') return statusPane; return null; },\n"
        "  getElementById(id) { return ids[id] || null; },\n"
        "  addEventListener() {},\n"
        "  createElement: makeElement,\n"
        "  createTextNode(text) { const node = makeElement('span'); node.textContent = text; return node; }\n"
        "};\n"
        "window.appEvents = { statusLabel: 'connected', getMetrics() { return { state: 'connected', uptimeMs: 0, lastMessageMs: null, connected: true }; } };\n"
        "window.AppServices.quietNotifs = { markViewed() {}, getAll() { return []; } };\n"
        "notifications._history = [\n"
        "  {app: 'system', icon: '👁️', title: 'Legacy eye', message: '', action: null, facet: null, timestamp: Date.now()},\n"
        "  {app: 'system', icon: '<img src=x onerror=alert(1)>', title: 'Unsafe', message: '', action: null, facet: null, timestamp: Date.now()}\n"
        "];\n"
        "vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context, { filename: 'status_pane.js' });\n"
        "statusIcon.listeners.click({ stopPropagation() {} });\n"
        "assert(historyContainer.innerHTML.includes(notifications._iconSvgByName.eye), 'legacy eye should render eye SVG');\n"
        "assert(historyContainer.innerHTML.includes(notifications._iconSvgByName.mailbox), 'unknown history icon should render mailbox SVG');\n"
        "assert(historyContainer.innerHTML.includes('class=\"icon-slot\"'), 'history row should use icon-slot');\n"
        "assertNoPayload(historyContainer.innerHTML);\n"
    )
