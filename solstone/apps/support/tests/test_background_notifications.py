# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APP_JS = REPO_ROOT / "solstone" / "convey" / "static" / "app.js"
CONVEY_ICONS_JS = REPO_ROOT / "solstone" / "convey" / "static" / "convey_icons.js"
SUPPORT_BACKGROUND = REPO_ROOT / "solstone" / "apps" / "support" / "background.html"


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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_proactive_support_notifications_are_keyed_by_service() -> None:
    notifications_object = _extract_notifications_object(
        APP_JS.read_text(encoding="utf-8")
    )
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        const fs = require('fs');
        const vm = require('vm');
        const notificationsSource = {json.dumps(notifications_object)};
        const storage = {{}};
        const browserCalls = [];
        const listeners = {{}};
        let registered = null;

        function FakeNotification(title, options) {{
          browserCalls.push({{ title, options }});
        }}
        FakeNotification.permission = 'granted';

        function escapeHtml(value) {{
          return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
        }}

        const window = {{
          Notification: FakeNotification,
          addEventListener() {{}},
          appEvents: {{
            listen(channel, listener) {{
              listeners[channel] = listener;
            }},
          }},
        }};
        window.AppServices = {{
          escapeHtml,
          badges: {{ app: {{ set() {{}} }} }},
          register(name, service) {{
            registered = {{ name, service }};
          }},
          registerTask() {{
            return {{ stop() {{}} }};
          }},
        }};

        const context = {{
          window,
          AppServices: window.AppServices,
          Notification: FakeNotification,
          localStorage: {{
            getItem(key) {{ return storage[key] || '[]'; }},
            setItem(key, value) {{ storage[key] = String(value); }},
          }},
          console: {{ warn() {{}}, debug() {{}}, error() {{}}, log() {{}} }},
          Date,
          JSON,
          String,
          Number,
          Object,
          Array,
          Set,
          Map,
          Math,
          setTimeout() {{ return 1; }},
          clearTimeout() {{}},
          setInterval() {{ return 1; }},
          clearInterval() {{}},
        }};
        context.global = context;
        context.globalThis = context;
        vm.createContext(context);
        vm.runInContext(
          fs.readFileSync(process.argv[1], 'utf8'),
          context,
          {{ filename: 'convey_icons.js' }}
        );
        const notifications = vm.runInContext(
          '(' + notificationsSource + ')',
          context,
          {{ filename: 'notifications-object.js' }}
        );
        notifications._render = function render() {{}};
        window.AppServices.notifications = notifications;

        context.__backgroundCode = fs.readFileSync(process.argv[2], 'utf8');
        vm.runInContext(
          'new Function(__backgroundCode)()',
          context,
          {{ filename: 'support-background.html' }}
        );
        assert.strictEqual(registered.name, 'support');
        registered.service.initialize();
        assert.strictEqual(typeof listeners.support, 'function');

        listeners.support({{
          event: 'proactive_suggestion',
          service: 'sense',
          message: 'sense has failed 1 of 3 times',
        }});
        listeners.support({{
          event: 'proactive_suggestion',
          service: 'sense',
          message: 'sense has failed 2 of 3 times',
        }});
        listeners.support({{
          event: 'proactive_suggestion',
          service: 'sense',
          message: 'sense has failed 3 of 3 times',
        }});
        listeners.support({{
          event: 'proactive_suggestion',
          service: 'cortex',
          message: 'cortex has failed 1 of 3 times',
        }});

        assert.strictEqual(notifications._stack.length, 2);
        assert.strictEqual(
          notifications._stack[0].key,
          'support:proactive_suggestion:sense'
        );
        assert.strictEqual(
          notifications._stack[0].message,
          'sense has failed 3 of 3 times'
        );
        assert.strictEqual(notifications._stack[0].title, 'Support suggestion');
        assert.strictEqual(notifications._stack[0].icon, 'life-buoy');
        assert.strictEqual(notifications._stack[0].action, '/app/support');
        assert.strictEqual(notifications._stack[0].dismissible, true);
        assert.strictEqual(notifications._stack[0].autoDismiss, 30000);
        assert.strictEqual(
          notifications._stack[1].key,
          'support:proactive_suggestion:cortex'
        );
        assert.strictEqual(
          notifications._stack[1].message,
          'cortex has failed 1 of 3 times'
        );
        assert.notStrictEqual(notifications._stack[0].id, notifications._stack[1].id);
        assert.strictEqual(notifications._history.length, 2);
        assert.strictEqual(browserCalls.length, 2);
        assert.strictEqual(browserCalls[0].title, 'Support suggestion');
        assert.strictEqual(browserCalls[0].options.body, 'sense has failed 1 of 3 times');
        assert.strictEqual(browserCalls[1].options.body, 'cortex has failed 1 of 3 times');
        """
    )
    result = subprocess.run(
        [
            "node",
            "-e",
            script,
            str(CONVEY_ICONS_JS),
            str(SUPPORT_BACKGROUND),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
