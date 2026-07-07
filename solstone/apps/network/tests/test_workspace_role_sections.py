# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.apps.network import copy


def test_render_devices_function_emits_device_sections(link_env) -> None:
    env = link_env()
    response = env.client.get("/app/network/workspace")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    state_response = env.client.get("/app/network/api/state")
    assert state_response.status_code == 200
    payload = state_response.get_json()["link_copy"]

    assert 'id="link-paired-h2"' in body
    assert 'data-copy="DEVICE_SECTION_TITLE"' in body
    assert payload["DEVICE_SECTION_TITLE"] == copy.DEVICE_SECTION_TITLE
    assert 'id="link-devices-count"' in body
    assert payload["DEVICE_PAIR_CTA"] == copy.DEVICE_PAIR_CTA
    assert "const roleOrder = ['phone', 'observer', 'peer'];" not in body
    assert "roleLabels" not in body
    assert "No devices linked yet." not in body

    assert "document.createElement('details')" in body
    assert "link-device-group-details" in body
    assert "summary.textContent = `${label} (${devices.length})`;" in body
    assert payload["DEVICE_GROUP_LABELS"] == copy.DEVICE_GROUP_LABELS

    assert "const ONLINE_THRESHOLD_SECONDS = 60;" in body
    assert "const RECENT_THRESHOLD_SECONDS = 86400;" in body
    assert "const GROUP_FILTER_THRESHOLD = 8;" in body
    assert "function deviceStatus(lastSeenIso)" in body
    assert "if (!lastSeenIso) return offline;" in body
    assert "if (Number.isNaN(then)) return offline;" in body
    for glyph in ("○", "●", "◐"):
        assert glyph in body

    assert 'class="link-recent-section"' in body
    assert 'id="link-recent-list"' in body
    assert payload["RECENT_SECTION_TITLE"] == copy.RECENT_SECTION_TITLE
    assert payload["RECENT_NETWORK_LABEL"] == copy.RECENT_NETWORK_LABEL

    init_start = body.index("function initLink()")
    init_end = body.index("if (document.readyState", init_start)
    init_body = body[init_start:init_end]
    assert "window.appEvents.listen('link'" in init_body
    assert "pair_complete" in init_body

    assert payload["DEVICE_EMPTY_TITLE"] == copy.DEVICE_EMPTY_TITLE
    assert payload["DEVICE_EMPTY_BODY"] == copy.DEVICE_EMPTY_BODY
    assert payload["REFRESH_FAIL_NOTICE"] == copy.REFRESH_FAIL_NOTICE
    assert payload["UNPAIR_BODY"] == copy.UNPAIR_BODY
