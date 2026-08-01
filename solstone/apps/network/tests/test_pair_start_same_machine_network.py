# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""A machine pairing with itself is at home, not reachable from anywhere."""

from __future__ import annotations

from solstone.apps.network import routes as link_routes


def test_pair_start_marks_same_machine_nonce(link_env) -> None:
    env = link_env()

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Home", "same_machine": True},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 200
    nonces = link_routes._nonces().snapshot()
    assert len(nonces) == 1
    assert nonces[0].same_machine is True


def test_pair_start_default_is_not_same_machine(link_env) -> None:
    env = link_env()

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Phone"},
    )

    assert response.status_code == 200
    nonces = link_routes._nonces().snapshot()
    assert len(nonces) == 1
    assert nonces[0].same_machine is False


def test_rough_network_still_reports_relay_reach_as_anywhere() -> None:
    """The guard against over-correcting: a real relay peer is unchanged."""
    assert link_routes._rough_network("pl-via-spl") == "anywhere"
    assert link_routes._rough_network("pl-direct") == "network"
    assert link_routes.NETWORK_HOME == "home"


def test_home_label_reaches_the_page(link_env) -> None:
    """The page maps the raw value; without the payload key it silently falls back."""
    env = link_env()

    payload = env.client.get("/app/network/api/state").get_json()["link_copy"]

    assert payload["RECENT_NETWORK_LABEL_HOME"] == "at home"
