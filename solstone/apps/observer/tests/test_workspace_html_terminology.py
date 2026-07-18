# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = APP_ROOT / "workspace.html"
APP_JSON = APP_ROOT / "app.json"


def _workspace_text() -> str:
    return WORKSPACE.read_text(encoding="utf-8")


def _owner_copy_text() -> str:
    text = _workspace_text()
    return re.sub(r"<!--.*?-->|/\*.*?\*/", "", text, flags=re.DOTALL)


def test_observer_app_label_uses_devices():
    payload = json.loads(APP_JSON.read_text(encoding="utf-8"))

    assert payload["label"] == "devices"


def test_workspace_owner_copy_replaces_retired_observer_phrases():
    text = _owner_copy_text()

    retired_phrases = (
        "add observer",
        "observer name",
        "loading observers",
        "add your first observer",
        "Revoke observer",
        "Malformed observer response",
        "Couldn't load observers",
    )
    for phrase in retired_phrases:
        assert phrase not in text

    expected_phrases = (
        'aria-label="devices"',
        '<h2 class="section-title">devices</h2>',
        'aria-label="set up a device"',
        '<h2 class="section-title">set up a device</h2>',
        '<label for="observerName">device name</label>',
        '<button type="submit">set up a device</button>',
        "loading devices…",
        'aria-label="device key"',
        '<h3 id="keyModalTitle">sol on <span id="modalObserverName"></span></h3>',
        "paste these into sol on that device, under settings. keep the key secret — anyone with it can add to your journal.",
        '<div class="credential-label">server URL</div>',
        '<div class="credential-label">key</div>',
        "set up your first device",
        "<dt>last reported</dt>",
        "<dt>last sol-ping</dt>",
        "button.textContent = 'reconnecting…';",
        "i couldn't load your devices.",
        'Remove "${name}"? sol on that device won\'t be able to add to your journal.',
        "i couldn't remove that device — your journal may not be reachable.",
        "i couldn't retrieve the key for that device — your journal may not be reachable.",
        "i couldn't set up that device — your journal may not be reachable.",
    )
    unescaped = text.replace("\\'", "'")
    for phrase in expected_phrases:
        assert phrase in unescaped

    assert (
        "Keep this key secret — anyone with it can upload to your journal." not in text
    )
    assert '<div class="credential-label">Server URL</div>' not in text
    assert "<dt>Last reported</dt>" not in text
    assert "<dt>Last sol-ping</dt>" not in text
    assert "button.textContent = 'reconnecting...';" not in text
    assert '<div class="credential-label">address</div>' not in text
    assert "<dt>last asked sol</dt>" not in text
    assert "serverMessage: 'Malformed observer response'" not in text
    assert "serverMessage: ''" in text
