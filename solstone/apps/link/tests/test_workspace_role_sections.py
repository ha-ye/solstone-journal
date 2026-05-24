# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations


def test_render_devices_function_emits_role_buckets(link_env) -> None:
    env = link_env()
    response = env.client.get("/app/link/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "const roleOrder = ['phone', 'observer', 'peer'];" in body
    assert "link-role-heading" in body
    assert "phone: 'Phones'" in body
    assert "observer: 'Observers'" in body
    assert "peer: 'Peers'" in body
    assert "No devices linked yet." in body
