# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.paths import authorized_clients_path


def test_devices_api_includes_role_field(link_env) -> None:
    env = link_env()
    store = AuthorizedClients(authorized_clients_path())
    store.add("sha256:phone", "phone", "inst-1", role="phone")
    store.add("sha256:observer", "observer", "inst-1", role="observer")

    response = env.client.get("/app/link/api/devices")

    assert response.status_code == 200
    devices = response.get_json()["devices"]
    roles_by_label = {device["device_label"]: device["role"] for device in devices}
    assert roles_by_label == {
        "phone": "phone",
        "observer": "observer",
    }


def test_devices_api_legacy_entry_defaults_to_phone(link_env) -> None:
    env = link_env()
    path = authorized_clients_path()
    path.write_text(
        json.dumps(
            [
                {
                    "fingerprint": "sha256:legacy",
                    "device_label": "legacy",
                    "paired_at": "2026-04-19T00:00:00Z",
                    "instance_id": "inst-1",
                }
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    response = env.client.get("/app/link/api/devices")

    assert response.status_code == 200
    devices = response.get_json()["devices"]
    assert len(devices) == 1
    assert devices[0]["role"] == "phone"
