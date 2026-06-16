# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ipaddress

from solstone.apps.link import routes as link_routes
from solstone.apps.link.crockford32 import decode as crockford_decode
from solstone.think.link.local_endpoints import LocalEndpoint

NONCE = "000102030405060708090a0b0c0d0e0f"
CA_FP = "a0a1a2a3a4a5a6a7a8a9aaabacadaeaf"


def _fragment(pair_link: str) -> str:
    return pair_link.rsplit("#", 1)[1]


def _decode_blob(pair_link: str) -> bytes:
    return crockford_decode(_fragment(pair_link))


def _split_v05(pair_link: str) -> tuple[int, list[str], str, str]:
    blob = _decode_blob(pair_link)
    assert blob[0:2] == b"\x05\x01"
    count = blob[2]
    port = int.from_bytes(blob[3:5], "big")
    address_start = 5
    address_end = address_start + 4 * count
    addresses = [
        str(ipaddress.IPv4Address(blob[offset : offset + 4]))
        for offset in range(address_start, address_end, 4)
    ]
    nonce = blob[address_end : address_end + 16].hex()
    ca_pin = blob[address_end + 16 : address_end + 32].hex()
    assert len(blob) == address_end + 32
    return port, addresses, nonce, ca_pin


def _post_pair_start(env) -> dict:
    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": "Test Phone"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert isinstance(payload, dict)
    return payload


def test_pair_link_reference_vectors() -> None:
    v04 = link_routes._build_pair_link("192.0.2.10", 7657, NONCE, CA_FP)
    assert (
        _decode_blob(v04).hex() == "0401c000020a1de9000102030405060708090a0b0c0d0e0f"
        "a0a1a2a3a4a5a6a7a8a9aaabacadaeaf"
    )

    v05_count_2 = link_routes._build_pair_link_v05(
        ["192.0.2.10", "198.51.100.20"],
        7657,
        NONCE,
        CA_FP,
    )
    assert (
        v05_count_2 == "https://go.solstone.app/p#"
        "0M0G47F9R00042P66DJ18001081G81860W40J2GB1G6GW3X0M6HA7955MTKTHADANEPAVBNF"
    )
    assert (
        _decode_blob(v05_count_2).hex()
        == "0501021de9c000020ac6336414000102030405060708090a0b0c0d0e0f"
        "a0a1a2a3a4a5a6a7a8a9aaabacadaeaf"
    )

    v05_count_4 = link_routes._build_pair_link_v05(
        ["192.0.2.10", "198.51.100.20", "203.0.113.30", "10.0.0.40"],
        7657,
        NONCE,
        CA_FP,
    )
    assert (
        v05_count_4 == "https://go.solstone.app/p#"
        "0M0G87F9R00042P66DJ19JR0E4F0M000500020G30G2GC1R81450P30D1R7T18D2MEJAB9N7N2MTNAXCNPQAY"
    )
    assert (
        _decode_blob(v05_count_4).hex() == "0501041de9c000020ac6336414cb00711e0a000028"
        "000102030405060708090a0b0c0d0e0f"
        "a0a1a2a3a4a5a6a7a8a9aaabacadaeaf"
    )


def test_pair_start_emit_switch_v04_then_v05(link_env) -> None:
    env = link_env(
        local_endpoints=[LocalEndpoint(ip="192.0.2.10", port=7657, scope="lan")]
    )
    single = _decode_blob(_post_pair_start(env)["pair_link"])
    assert single[0] == 0x04
    assert len(single) == 40

    env = link_env(
        local_endpoints=[
            LocalEndpoint(ip="192.0.2.10", port=7657, scope="lan"),
            LocalEndpoint(ip="198.51.100.20", port=7657, scope="lan"),
        ]
    )
    multiple = _decode_blob(_post_pair_start(env)["pair_link"])
    assert multiple[0] == 0x05


def test_pair_start_candidates_sourced_from_snapshot_not_detect(
    link_env,
    monkeypatch,
) -> None:
    monkeypatch.setattr(link_routes, "_detect_lan_ip", lambda: "10.0.0.99")
    env = link_env(
        local_endpoints=[
            LocalEndpoint(ip="192.0.2.10", port=7657, scope="lan"),
            LocalEndpoint(ip="198.51.100.20", port=7657, scope="lan"),
        ]
    )

    _, addresses, _, _ = _split_v05(_post_pair_start(env)["pair_link"])

    assert addresses == ["192.0.2.10", "198.51.100.20"]
    assert "10.0.0.99" not in addresses


def test_pair_start_default_route_moved_first(link_env, monkeypatch) -> None:
    monkeypatch.setattr(link_routes, "_detect_lan_ip", lambda: "198.51.100.20")
    env = link_env(
        local_endpoints=[
            LocalEndpoint(ip="192.0.2.10", port=7657, scope="lan"),
            LocalEndpoint(ip="198.51.100.20", port=7657, scope="lan"),
        ]
    )

    _, addresses, _, _ = _split_v05(_post_pair_start(env)["pair_link"])

    assert addresses == ["198.51.100.20", "192.0.2.10"]


def test_pair_start_v05_uses_secure_listener_port_not_endpoint_ports(
    link_env,
    monkeypatch,
) -> None:
    monkeypatch.setattr(link_routes.interface_watcher, "LINK_DIRECT_PORT", 8765)
    env = link_env(
        local_endpoints=[
            LocalEndpoint(ip="192.0.2.10", port=1111, scope="lan"),
            LocalEndpoint(ip="198.51.100.20", port=2222, scope="lan"),
        ]
    )

    port, _, _, _ = _split_v05(_post_pair_start(env)["pair_link"])

    assert port == 8765


def test_pair_start_order_then_cap_excludes_ipv6(link_env, monkeypatch) -> None:
    monkeypatch.setattr(link_routes, "_detect_lan_ip", lambda: "192.0.2.14")
    env = link_env(
        local_endpoints=[
            LocalEndpoint(ip="192.0.2.10", port=7657, scope="lan"),
            LocalEndpoint(ip="192.0.2.11", port=7657, scope="lan"),
            LocalEndpoint(ip="fd00::1", port=7657, scope="ula"),
            LocalEndpoint(ip="192.0.2.11", port=7657, scope="lan"),
            LocalEndpoint(ip="192.0.2.12", port=7657, scope="lan"),
            LocalEndpoint(ip="192.0.2.13", port=7657, scope="lan"),
            LocalEndpoint(ip="192.0.2.14", port=7657, scope="lan"),
        ]
    )

    _, addresses, _, _ = _split_v05(_post_pair_start(env)["pair_link"])

    assert addresses == ["192.0.2.14", "192.0.2.10", "192.0.2.11", "192.0.2.12"]
    assert len(addresses) == 4
    assert "fd00::1" not in addresses


def test_pair_link_v05_length_law() -> None:
    for count in range(1, 5):
        nonce = "11" * 16
        ca_fp = "22" * 32
        candidates = [f"192.0.2.{index}" for index in range(1, count + 1)]
        pair_link = link_routes._build_pair_link_v05(candidates, 7657, nonce, ca_fp)
        blob = _decode_blob(pair_link)
        address_end = 5 + 4 * count

        assert len(blob) == 37 + 4 * count
        assert blob[0:2] == b"\x05\x01"
        assert blob[2] == count
        assert int.from_bytes(blob[3:5], "big") == 7657
        assert blob[address_end : address_end + 16] == bytes.fromhex(nonce)
        assert blob[address_end + 16 : address_end + 32] == bytes.fromhex(ca_fp)[:16]
