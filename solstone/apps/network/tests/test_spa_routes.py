# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations


def test_network_spa_routes_and_static_assets_resolve(link_env) -> None:
    env = link_env()

    qr_response = env.client.get("/static/pairing-qr.js")
    assert qr_response.status_code == 200

    index_response = env.client.get("/app/network/")
    assert index_response.status_code == 200
    assert b'data-solstone-shell="spa"' in index_response.data

    alias_response = env.client.get("/app/link/")
    assert alias_response.status_code == 302
    assert alias_response.headers["Location"] == "/app/network/"

    workspace_response = env.client.get("/app/network/workspace")
    assert workspace_response.status_code == 200
    assert b'id="link-workspace-root"' in workspace_response.data

    state_response = env.client.get("/app/network/api/state")
    assert state_response.status_code == 200
    assert set(state_response.get_json()) == {"posture", "link_copy"}
