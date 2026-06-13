# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest

from solstone.apps.link import routes as link_routes
from solstone.think.link.auth import AuthorizedClients, ClientEntry
from solstone.think.link.paths import authorized_clients_path


def test_devices_api_includes_role_field(link_env) -> None:
    env = link_env()
    store = AuthorizedClients(authorized_clients_path())
    store.add("sha256:linked", "linked", "inst-1")
    store.add("sha256:peer", "peer", "inst-1", role="peer")

    response = env.client.get("/app/link/api/devices")

    assert response.status_code == 200
    devices = response.get_json()["devices"]
    roles_by_label = {device["device_label"]: device["role"] for device in devices}
    assert roles_by_label == {
        "linked": "",
        "peer": "peer",
    }


@pytest.mark.parametrize(
    ("assigned", "client", "expected"),
    [
        ("assigned", "client", "assigned (client)"),
        ("same", "same", "same"),
        ("assigned", "", "assigned"),
        ("", "client", "client"),
        ("", "", ""),
    ],
)
def test_entry_to_json_composes_display_label(
    assigned: str,
    client: str,
    expected: str,
) -> None:
    entry = ClientEntry(
        fingerprint="sha256:abcdef",
        device_label=assigned,
        paired_at="2026-04-19T00:00:00Z",
        instance_id="inst-1",
        client_label=client,
    )

    payload = link_routes._entry_to_json(entry)

    assert payload["display_label"] == expected
    assert "client_label" not in payload


def test_devices_api_legacy_entry_defaults_to_role_less(link_env) -> None:
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
    assert devices[0]["role"] == ""
    assert devices[0]["display_label"] == "legacy"
    assert "client_label" not in devices[0]
