# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from solstone.apps.network import routes as link_routes
from solstone.think.link.paths import authorized_clients_path


def _make_csr(label: str = "test") -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _pair(
    env,
    *,
    label: str,
    client_label: str | None = None,
) -> dict:
    started = env.client.post(
        "/app/network/pair-start",
        json={"device_label": label, "role": ""},
    )
    assert started.status_code == 200

    body = {"csr": _make_csr(client_label or label or "test")}
    if client_label is not None:
        body["device_label"] = client_label
    response = env.client.post(
        f"/app/network/pair?token={started.get_json()['nonce']}",
        json=body,
    )
    assert response.status_code == 200
    return response.get_json()


def test_blank_assigned_label_collides_on_client_label(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()
    events: list[tuple[str, str, dict]] = []

    def record_emit(tract: str, event: str, **payload: object) -> None:
        events.append((tract, event, payload))

    monkeypatch.setattr(link_routes, "emit", record_emit)

    first = _pair(env, label="", client_label="iPhone")
    second = _pair(env, label="", client_label="iPhone")

    response = env.client.get("/app/network/api/devices")
    assert response.status_code == 200
    devices = {
        device["fingerprint"]: device for device in response.get_json()["devices"]
    }
    assert devices[first["fingerprint"]]["device_label"] == ""
    assert devices[first["fingerprint"]]["display_label"] == "iPhone"
    assert devices[second["fingerprint"]]["device_label"] == ""
    assert devices[second["fingerprint"]]["display_label"] == "iPhone (2)"

    persisted = {
        item["fingerprint"]: item
        for item in json.loads(authorized_clients_path().read_text("utf-8"))
    }
    assert persisted[first["fingerprint"]]["device_label"] == ""
    assert persisted[first["fingerprint"]]["client_label"] == "iPhone"
    assert "label_ordinal" not in persisted[first["fingerprint"]]
    assert persisted[second["fingerprint"]]["device_label"] == ""
    assert persisted[second["fingerprint"]]["client_label"] == "iPhone"
    assert persisted[second["fingerprint"]]["label_ordinal"] == 2

    pair_complete_labels = [
        payload["device_label"]
        for tract, event, payload in events
        if tract == "link" and event == "pair_complete"
    ]
    assert pair_complete_labels == ["iPhone", "iPhone (2)"]
