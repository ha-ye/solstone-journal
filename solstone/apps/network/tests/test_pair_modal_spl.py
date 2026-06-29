# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pair modal posture rendering regressions."""

from __future__ import annotations

from solstone.apps.network import copy


def _link_pair_script(body: str) -> str:
    start = body.index("const SPL_PAIR_WINDOW_MS")
    end = body.index("function getUnpairFocusable", start)
    return body[start:end]


def test_spl_pair_modal_is_qr_only_with_five_minute_window(link_env) -> None:
    env = link_env(posture="spl", service_token="svc-token")

    response = env.client.get("/app/network/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="link-pair-manual-code"' not in body
    assert 'id="link-pair-rotation-ring"' not in body
    assert 'id="link-pair-rotation"' not in body
    assert 'id="link-pair-network"' in body
    assert copy.PAIR_NETWORK_LINE in body
    assert copy.WINDOW_CLOSED_BUTTON in body
    assert copy.EXPIRED_BUTTON not in body
    assert "this code expired" not in body
    assert "countdown-number" not in body
    assert "LINK_POSTURE" not in body
    pair_script = _link_pair_script(body)
    assert "data.rotating" not in pair_script
    assert "rotationTimer" not in pair_script
    assert "startRotationRing" not in pair_script
    assert "LINK_POSTURE" not in pair_script
    assert "Number(data.expires_in) || 300" in pair_script
    assert pair_script.count("5 * 60 * 1000") == 1
    assert "function startSplPairWindow" in pair_script
    assert "function closeSplPairWindow" in pair_script
    assert (
        "windowTimer = setTimeout(closeSplPairWindow, SPL_PAIR_WINDOW_MS)"
        in pair_script
    )
    assert "latestStatus?.posture === 'spl'" in pair_script
    assert "startSplPairWindow();" in pair_script


def test_direct_pair_modal_keeps_network_and_expired_copy(
    link_env,
) -> None:
    env = link_env()

    response = env.client.get("/app/network/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="link-pair-manual-code"' not in body
    assert copy.PAIR_NETWORK_LINE in body
    assert 'id="link-pair-rotation"' not in body
    assert 'id="link-pair-network"' in body
    assert copy.EXPIRED_BUTTON in body
    assert "LINK_POSTURE" not in body
