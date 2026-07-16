# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import copy
import json
from pathlib import Path

from solstone.think.pairing import config as pairing_config


def _read_config(journal: Path) -> dict:
    return json.loads((journal / "config" / "journal.json").read_text("utf-8"))


def test_host_address_sets_canonical_override(link_env) -> None:
    env = link_env()

    response = env.client.post(
        "/app/network/host-address",
        json={"home_address": "192.168.1.44:7657"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "home_address": "192.168.1.44:7657",
    }
    assert _read_config(env.journal)["pairing"]["home_address"] == "192.168.1.44:7657"


def test_host_address_normalizes_bare_ipv4_port(link_env) -> None:
    env = link_env()

    response = env.client.post(
        "/app/network/host-address",
        json={"home_address": " 192.168.1.44:7657 "},
    )

    assert response.status_code == 200
    assert response.get_json()["home_address"] == "192.168.1.44:7657"
    assert _read_config(env.journal)["pairing"]["home_address"] == "192.168.1.44:7657"


def test_host_address_clears_on_empty_null_or_missing(link_env) -> None:
    env = link_env()

    for body in ({"home_address": ""}, {"home_address": None}, {}):
        env.client.post(
            "/app/network/host-address",
            json={"home_address": "192.168.1.44:7657"},
        )
        response = env.client.post("/app/network/host-address", json=body)

        assert response.status_code == 200
        assert _read_config(env.journal)["pairing"]["home_address"] is None


def test_host_address_rejects_hostname_without_writing(link_env) -> None:
    env = link_env()
    before = copy.deepcopy(_read_config(env.journal))

    response = env.client.post(
        "/app/network/host-address",
        json={"home_address": "mylab.local:7657"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_config_value"
    assert payload["detail"] == pairing_config.HOME_ADDRESS_HOSTNAME_UNSUPPORTED
    assert _read_config(env.journal) == before


def test_host_address_rejects_malformed_without_writing(link_env) -> None:
    env = link_env()
    before = copy.deepcopy(_read_config(env.journal))

    response = env.client.post(
        "/app/network/host-address",
        json={"home_address": "http://192.168.1.44"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_config_value"
    assert payload["detail"] == pairing_config.HOME_ADDRESS_INVALID
    assert _read_config(env.journal) == before
