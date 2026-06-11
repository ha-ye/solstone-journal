# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.apps.link import routes as link_routes


@pytest.mark.parametrize(
    ("role", "expected_role"),
    [
        ("", ""),
        ("phone", "phone"),
        ("observer", "observer"),
        ("peer", "peer"),
    ],
)
def test_pair_start_stores_accepted_role(
    link_env,
    role: str,
    expected_role: str,
) -> None:
    env = link_env()

    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": "x", "role": role},
    )

    assert response.status_code == 200
    nonces = link_routes._nonces().snapshot()
    assert len(nonces) == 1
    assert nonces[0].role == expected_role


def test_pair_start_default_role_less(link_env) -> None:
    env = link_env()

    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": "x"},
    )

    assert response.status_code == 200
    nonces = link_routes._nonces().snapshot()
    assert len(nonces) == 1
    assert nonces[0].role == ""


def test_pair_start_null_role_is_role_less(link_env) -> None:
    env = link_env()

    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": "x", "role": None},
    )

    assert response.status_code == 200
    nonces = link_routes._nonces().snapshot()
    assert len(nonces) == 1
    assert nonces[0].role == ""


@pytest.mark.parametrize("role", ["bogus", "Observer", 42])
def test_pair_start_rejects_invalid_role(link_env, role: object) -> None:
    env = link_env()

    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": "x", "role": role},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "pairing_request_invalid"
