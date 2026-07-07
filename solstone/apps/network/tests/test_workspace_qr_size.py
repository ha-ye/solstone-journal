# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Regression test for link pair QR expired-overlay markup."""

from __future__ import annotations

from solstone.apps.network import copy


def test_workspace_qr_expired_overlay(link_env) -> None:
    env = link_env()
    response = env.client.get("/app/network/workspace")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    state_response = env.client.get("/app/network/api/state")
    assert state_response.status_code == 200

    assert 'id="link-qr-expired"' in body
    assert ".link-qr-container.is-expired" in body
    assert (
        state_response.get_json()["link_copy"]["EXPIRED_BUTTON"] == copy.EXPIRED_BUTTON
    )
    assert "qrContainer.classList.add('is-expired')" in body
