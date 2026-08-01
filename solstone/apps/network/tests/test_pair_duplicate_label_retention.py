# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import datetime as dt

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.paths import LinkState, authorized_clients_path

FP_OLD = "sha256:" + "a" * 64


def _make_csr(label: str = "duplicate-label") -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _authorized() -> AuthorizedClients:
    return AuthorizedClients(authorized_clients_path())


def _seed_predecessor(label: str = "iPhone") -> str:
    instance_id = LinkState.load_or_create().instance_id
    _authorized().add(
        fingerprint=FP_OLD,
        device_label=label,
        instance_id=instance_id,
        role="",
        paired_at="2026-04-19T00:00:01Z",
    )
    return FP_OLD


def _pair(env, *, label: str = "iPhone", same_machine: bool = False) -> dict:
    payload: dict[str, object] = {"device_label": label, "role": ""}
    post_kwargs: dict[str, object] = {}
    if same_machine:
        payload["same_machine"] = True
        post_kwargs["environ_base"] = {"REMOTE_ADDR": "127.0.0.1"}

    started = env.client.post("/app/network/pair-start", json=payload, **post_kwargs)
    assert started.status_code == 200
    response = env.client.post(
        f"/app/network/pair?token={started.get_json()['nonce']}",
        json={"csr": _make_csr(label)},
    )
    assert response.status_code == 200
    return response.get_json()


def test_duplicate_label_pair_keeps_predecessor_authorized_for_ordinary_pair(
    link_env,
) -> None:
    env = link_env()
    predecessor = _seed_predecessor()

    paired = _pair(env)

    authorized = _authorized()
    assert paired["fingerprint"] != predecessor
    assert authorized.is_authorized(predecessor) is True
    assert authorized.is_authorized(paired["fingerprint"]) is True


def test_duplicate_label_pair_keeps_predecessor_authorized_for_same_machine_pair(
    link_env,
) -> None:
    env = link_env()
    predecessor = _seed_predecessor()

    paired = _pair(env, same_machine=True)

    authorized = _authorized()
    assert paired["fingerprint"] != predecessor
    assert authorized.is_authorized(predecessor) is True
    assert authorized.is_authorized(paired["fingerprint"]) is True


def test_duplicate_label_pair_preserves_ghost_distinguishability(link_env) -> None:
    env = link_env()
    predecessor = _seed_predecessor()
    _authorized().touch_last_seen(
        predecessor,
        now=dt.datetime(2026, 4, 19, 0, 5, tzinfo=dt.UTC),
    )

    paired = _pair(env)

    response = env.client.get("/app/network/api/devices")
    assert response.status_code == 200
    devices = {
        device["fingerprint"]: device for device in response.get_json()["devices"]
    }
    assert set(devices) == {predecessor, paired["fingerprint"]}
    assert devices[predecessor]["last_seen_at"] == "2026-04-19T00:05:00Z"
    assert devices[paired["fingerprint"]]["last_seen_at"] is None
    assert (
        devices[predecessor]["fingerprint_short"]
        != devices[paired["fingerprint"]]["fingerprint_short"]
    )
