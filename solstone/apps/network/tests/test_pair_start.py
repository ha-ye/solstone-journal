# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Regression tests for the link pair-start response contract."""

from __future__ import annotations

import ipaddress
import json
import re

from solstone.apps.network import routes as link_routes
from solstone.apps.network.crockford32 import decode as crockford_decode
from solstone.apps.network.relay_link import decode_pair_window_link, derive_rk
from solstone.think.link.ca import load_or_generate_ca
from solstone.think.link.local_endpoints import LocalEndpoint
from solstone.think.link.nonces import NONCE_TTL_SECONDS
from solstone.think.link.paths import ca_dir

PAIR_START_KEYS = [
    "nonce",
    "pair_link",
    "expires_in",
    "device_label",
    "ca_fingerprint",
]


def test_pair_start_shape_and_locked_order(link_env) -> None:
    env = link_env()

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert list(payload.keys()) == PAIR_START_KEYS
    assert re.fullmatch(
        r"^https://go\.solstone\.app/p#[0-9A-HJKMNP-TV-Z]{64}$",
        payload["pair_link"],
    )
    snap = link_routes._nonces().snapshot()
    assert payload["expires_in"] == NONCE_TTL_SECONDS
    assert len(snap) == 1
    assert snap[0].expires_at - snap[0].issued_at == NONCE_TTL_SECONDS
    assert "pair_url" not in payload
    assert "qr_payload" not in payload


def test_pair_start_omitted_assigned_label_stores_empty(link_env) -> None:
    env = link_env()

    response = env.client.post("/app/network/pair-start", json={})

    assert response.status_code == 200
    payload = response.get_json()
    assert list(payload.keys()) == PAIR_START_KEYS
    assert payload["device_label"] == ""
    snap = link_routes._nonces().snapshot()
    assert len(snap) == 1
    assert snap[0].device_label == ""


def test_pair_start_blank_assigned_label_stores_empty(link_env) -> None:
    env = link_env()

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "   "},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["device_label"] == ""
    snap = link_routes._nonces().snapshot()
    assert len(snap) == 1
    assert snap[0].device_label == ""


def test_pair_start_allows_lenient_assigned_label(link_env) -> None:
    env = link_env()
    label = "device — added Jun 13!"

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": label},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["device_label"] == label
    snap = link_routes._nonces().snapshot()
    assert len(snap) == 1
    assert snap[0].device_label == label


def test_pair_start_mints_distinct_nonce(link_env) -> None:
    env = link_env()

    first = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "First Phone"},
    ).get_json()
    second = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Second Phone"},
    ).get_json()

    assert first["nonce"] != second["nonce"]


def test_pair_start_uses_host_address_override_for_direct_qr(link_env) -> None:
    env = link_env()
    config_path = env.journal / "config" / "journal.json"
    config = json.loads(config_path.read_text("utf-8"))
    config["pairing"] = {"home_address": "192.0.2.44:7657"}
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    decoded = _decode_pair_link(payload["pair_link"])
    assert decoded[0:2] == b"\x04\x01"
    assert decoded[2:6] == ipaddress.IPv4Address("192.0.2.44").packed
    assert int.from_bytes(decoded[6:8], "big") == link_routes._secure_listener_port()


def test_pair_start_direct_pair_link_port_uses_secure_listener_source(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()
    config_path = env.journal / "config" / "journal.json"
    config = json.loads(config_path.read_text("utf-8"))
    config["pairing"] = {"home_address": "192.0.2.44:7657"}
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    monkeypatch.setattr(link_routes.interface_watcher, "LINK_DIRECT_PORT", 8765)

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    decoded = _decode_pair_link(payload["pair_link"])
    assert decoded[2:6] == ipaddress.IPv4Address("192.0.2.44").packed
    assert int.from_bytes(decoded[6:8], "big") == 8765


def test_pair_start_no_candidates_rejected_without_nonce(
    link_env,
    monkeypatch,
) -> None:
    env = link_env(local_endpoints=[])
    monkeypatch.setattr(link_routes, "_detect_lan_ip", lambda: None)

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "pairing_request_invalid"
    assert payload["detail"] == "pair-link requires an IPv4 LAN address; none found"
    assert link_routes._nonces().snapshot() == []


def test_pair_start_uses_route_fallback_when_snapshot_empty(
    link_env,
    monkeypatch,
) -> None:
    env = link_env(local_endpoints=[])
    monkeypatch.setattr(link_routes, "_detect_lan_ip", lambda: "192.168.1.50")

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    decoded = _decode_pair_link(payload["pair_link"])
    assert decoded[0:2] == b"\x04\x01"
    assert decoded[2:6] == ipaddress.IPv4Address("192.168.1.50").packed
    assert int.from_bytes(decoded[6:8], "big") == link_routes._secure_listener_port()


def test_pair_start_resolver_exception_rejected_without_nonce(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()

    def fail_candidates(endpoints):
        raise RuntimeError("watcher exploded")

    monkeypatch.setattr(link_routes, "_resolve_pair_link_candidates", fail_candidates)

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "pairing_request_invalid"
    assert payload["detail"] == "pair-link requires an IPv4 LAN address; none found"
    assert link_routes._nonces().snapshot() == []


def test_pair_start_override_survives_resolver_exception(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()
    config_path = env.journal / "config" / "journal.json"
    config = json.loads(config_path.read_text("utf-8"))
    config["pairing"] = {"home_address": "192.168.1.44:7657"}
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    def fail_candidates(endpoints):
        raise RuntimeError("watcher exploded")

    monkeypatch.setattr(link_routes, "_resolve_pair_link_candidates", fail_candidates)

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    decoded = _decode_pair_link(payload["pair_link"])
    assert decoded[0:2] == b"\x04\x01"
    assert decoded[2:6] == ipaddress.IPv4Address("192.168.1.44").packed
    assert int.from_bytes(decoded[6:8], "big") == link_routes._secure_listener_port()


def test_pair_start_detected_order_matches_api_status(
    link_env,
    monkeypatch,
) -> None:
    env = link_env(
        local_endpoints=[
            LocalEndpoint(ip="192.168.1.51", port=1111, scope="lan"),
            LocalEndpoint(ip="192.168.1.50", port=2222, scope="lan"),
            LocalEndpoint(ip="192.168.1.52", port=3333, scope="lan"),
        ]
    )
    monkeypatch.setattr(link_routes, "_detect_lan_ip", lambda: "192.168.1.50")

    status_response = env.client.get(
        "/app/network/api/status",
        base_url="http://localhost:7657",
    )
    assert status_response.status_code == 200
    status_payload = status_response.get_json()
    status_addresses = [
        entry["address"].partition(":")[0]
        for entry in status_payload["home_candidates"]
    ]

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Test Phone"},
    )
    assert response.status_code == 200
    pair_addresses = _decode_pair_link_addresses(response.get_json()["pair_link"])

    assert status_addresses == ["192.168.1.50", "192.168.1.51", "192.168.1.52"]
    assert pair_addresses == status_addresses


def _fragment(pair_link: str) -> str:
    return pair_link.rsplit("#", 1)[1]


def _decode_pair_link(pair_link: str) -> bytes:
    return crockford_decode(_fragment(pair_link))


def _decode_pair_link_addresses(pair_link: str) -> list[str]:
    decoded = _decode_pair_link(pair_link)
    if decoded[0:2] == b"\x04\x01":
        return [str(ipaddress.IPv4Address(decoded[2:6]))]
    assert decoded[0:2] == b"\x05\x01"
    count = decoded[2]
    return [
        str(ipaddress.IPv4Address(decoded[offset : offset + 4]))
        for offset in range(5, 5 + count * 4, 4)
    ]


def test_pair_start_spl_mints_relay_form_pair_link(link_env) -> None:
    env = link_env(posture="spl", service_token="svc-token-xyz")

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    decoded = _decode_pair_link(payload["pair_link"])
    ca = load_or_generate_ca(ca_dir())

    assert decoded[0] == 0x06
    assert len(decoded) == 27
    assert decoded[9] == 0x01
    assert decoded[10:26] == bytes.fromhex(ca.spki_fingerprint_sha256())[:16]
    assert decoded[26] == 0x00

    parsed = decode_pair_window_link(payload["pair_link"])
    assert parsed.relay_origin is None
    assert len(parsed.s) == 8

    snap = link_routes._nonces().snapshot()
    assert len(snap) == 1
    assert snap[0].value == parsed.s.hex()

    assert len(env.pair_window_calls) == 1
    call = env.pair_window_calls[0]
    assert call["rk"] == derive_rk(parsed.s)
    assert call["service_token"] == "svc-token-xyz"
    assert call["relay_endpoint"] == link_routes.relay_url()


def test_pair_start_spl_uses_five_minute_expiry_and_nonce_ttl(link_env) -> None:
    env = link_env(posture="spl", service_token="svc")

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    snap = link_routes._nonces().snapshot()
    assert payload["expires_in"] == NONCE_TTL_SECONDS
    assert len(snap) == 1
    assert snap[0].expires_at - snap[0].issued_at == NONCE_TTL_SECONDS


def test_pair_start_spl_keeps_role_less_home_private(link_env) -> None:
    env = link_env(posture="spl", service_token="svc")

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Linked System"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert link_routes._nonces().snapshot()[0].role == ""
    assert b"observer" not in _decode_pair_link(payload["pair_link"])
    assert b"phone" not in _decode_pair_link(payload["pair_link"])


def test_pair_start_spl_missing_service_token_errors_without_nonce(link_env) -> None:
    env = link_env(posture="spl")

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_operation_for_state"
    assert link_routes._nonces().snapshot() == []
    assert env.pair_window_calls == []


def test_pair_start_spl_window_open_failure_errors_without_nonce(link_env) -> None:
    env = link_env(posture="spl", service_token="svc", pair_window_opens=False)

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["reason_code"] == "pairing_relay_unavailable"
    assert "pair_link" not in payload
    assert link_routes._nonces().snapshot() == []
    assert len(env.pair_window_calls) == 1
    assert env.pair_window_handles[0].cancelled is True


def test_pair_start_spl_response_order_and_display_fingerprint(link_env) -> None:
    env = link_env(posture="spl", service_token="svc")

    response = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    ca = load_or_generate_ca(ca_dir())
    assert list(payload.keys()) == PAIR_START_KEYS
    assert payload["ca_fingerprint"] == ca.fingerprint_sha256()
    assert payload["ca_fingerprint"] != ca.spki_fingerprint_sha256()
