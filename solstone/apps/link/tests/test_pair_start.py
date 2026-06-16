# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Regression tests for the link pair-start response contract."""

from __future__ import annotations

import ipaddress
import json
import re
import time
import uuid

from solstone.apps.link import routes as link_routes
from solstone.apps.link.crockford32 import decode as crockford_decode
from solstone.apps.link.relay_link import TOTP_STEP_SECONDS, compute_current_totp
from solstone.think.link.ca import load_or_generate_ca
from solstone.think.link.nonces import NONCE_TTL_SECONDS
from solstone.think.link.paths import LinkState, ca_dir

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
        "/app/link/pair-start",
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

    response = env.client.post("/app/link/pair-start", json={})

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
        "/app/link/pair-start",
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
        "/app/link/pair-start",
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
        "/app/link/pair-start",
        json={"device_label": "First Phone"},
    ).get_json()
    second = env.client.post(
        "/app/link/pair-start",
        json={"device_label": "Second Phone"},
    ).get_json()

    assert first["nonce"] != second["nonce"]


def test_pair_start_uses_host_address_override_for_direct_qr(link_env) -> None:
    env = link_env()
    config_path = env.journal / "config" / "journal.json"
    config = json.loads(config_path.read_text("utf-8"))
    config["pairing"] = {"host_url": "http://192.0.2.44:7070"}
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    decoded = _decode_pair_link(payload["pair_link"])
    assert decoded[0:2] == b"\x04\x01"
    assert decoded[2:6] == ipaddress.IPv4Address("192.0.2.44").packed
    assert int.from_bytes(decoded[6:8], "big") == link_routes._secure_listener_port()
    assert int.from_bytes(decoded[6:8], "big") != 7070


def test_pair_start_direct_pair_link_port_uses_secure_listener_source(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()
    config_path = env.journal / "config" / "journal.json"
    config = json.loads(config_path.read_text("utf-8"))
    config["pairing"] = {"host_url": "http://192.0.2.44:7070"}
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    monkeypatch.setattr(link_routes.interface_watcher, "LINK_DIRECT_PORT", 8765)

    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    decoded = _decode_pair_link(payload["pair_link"])
    assert decoded[2:6] == ipaddress.IPv4Address("192.0.2.44").packed
    assert int.from_bytes(decoded[6:8], "big") == 8765


def test_pair_start_no_candidates_rejected_without_nonce(link_env) -> None:
    env = link_env(local_endpoints=[])

    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "pairing_request_invalid"
    assert payload["detail"] == "pair-link requires an IPv4 LAN address; none found"
    assert link_routes._nonces().snapshot() == []


def _fragment(pair_link: str) -> str:
    return pair_link.rsplit("#", 1)[1]


def _decode_pair_link(pair_link: str) -> bytes:
    return crockford_decode(_fragment(pair_link))


def test_pair_start_spl_mints_relay_form_pair_link(link_env) -> None:
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    env = link_env(posture="spl", totp_secret=secret)

    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    decoded = _decode_pair_link(payload["pair_link"])
    instance_id = LinkState.load_or_create().instance_id
    ca = load_or_generate_ca(ca_dir())
    now = int(time.time())

    assert decoded[0] == 0x03
    assert decoded[1:17] == uuid.UUID(instance_id).bytes
    assert int.from_bytes(decoded[17:20], "big") in {
        compute_current_totp(secret, now + delta) for delta in (-1, 0, 1)
    }
    assert len(decoded[20:36]) == 16
    assert decoded[36] == 0x01
    assert decoded[37:53] == bytes.fromhex(ca.spki_fingerprint_sha256())[:16]
    assert decoded[53] == 0x00
    assert len(decoded) == 54


def test_pair_start_spl_uses_thirty_second_expiry_and_nonce_ttl(link_env) -> None:
    env = link_env(posture="spl", totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")

    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    snap = link_routes._nonces().snapshot()
    assert payload["expires_in"] == TOTP_STEP_SECONDS
    assert len(snap) == 1
    assert snap[0].expires_at - snap[0].issued_at == TOTP_STEP_SECONDS


def test_pair_start_spl_keeps_role_less_home_private(link_env, monkeypatch) -> None:
    env = link_env(posture="spl", totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
    monkeypatch.setattr(link_routes, "generate_relay_nonce", lambda: "00" * 16)

    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": "Linked System"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert link_routes._nonces().snapshot()[0].role == ""
    assert b"observer" not in _decode_pair_link(payload["pair_link"])
    assert b"phone" not in _decode_pair_link(payload["pair_link"])


def test_pair_start_spl_missing_totp_secret_errors_without_nonce(link_env) -> None:
    env = link_env(posture="spl")

    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_operation_for_state"
    assert link_routes._nonces().snapshot() == []


def test_pair_start_spl_response_order_and_display_fingerprint(link_env) -> None:
    env = link_env(posture="spl", totp_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")

    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": "Test Phone"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    ca = load_or_generate_ca(ca_dir())
    assert list(payload.keys()) == PAIR_START_KEYS
    assert payload["ca_fingerprint"] == ca.fingerprint_sha256()
    assert payload["ca_fingerprint"] != ca.spki_fingerprint_sha256()
