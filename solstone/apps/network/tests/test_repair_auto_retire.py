# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from solstone.apps.network import routes as link_routes
from solstone.think.link.paths import LinkState


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
    role: str = "",
    client_label: str | None = None,
) -> dict:
    started = env.client.post(
        "/app/network/pair-start",
        json={"device_label": label, "role": role},
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


def _entries_for_label(label: str):
    return [
        entry
        for entry in link_routes._authorized().snapshot()
        if entry.device_label == label
    ]


def test_repair_same_label_collapses_to_one(link_env) -> None:
    env = link_env()

    first = _pair(env, label="Jer's iPhone")
    second = _pair(env, label="Jer's iPhone")

    entries = _entries_for_label("Jer's iPhone")
    assert len(entries) == 1
    assert entries[0].fingerprint == second["fingerprint"]
    assert link_routes._authorized().is_authorized(first["fingerprint"]) is False
    assert link_routes._authorized().is_authorized(second["fingerprint"]) is True


def test_repair_collapses_already_accrued_duplicates(link_env) -> None:
    env = link_env()
    instance_id = LinkState.load_or_create().instance_id
    seeded_fingerprints = [
        "sha256:aaa111",
        "sha256:bbb222",
        "sha256:ccc333",
    ]
    authorized = link_routes._authorized()
    for fingerprint in seeded_fingerprints:
        authorized.add(
            fingerprint=fingerprint,
            device_label="watch",
            instance_id=instance_id,
            role="",
        )

    repaired = _pair(env, label="watch")

    entries = _entries_for_label("watch")
    assert len(entries) == 1
    assert entries[0].fingerprint == repaired["fingerprint"]
    for fingerprint in seeded_fingerprints:
        assert link_routes._authorized().is_authorized(fingerprint) is False
    assert link_routes._authorized().is_authorized(repaired["fingerprint"]) is True


def test_repair_distinct_labels_untouched(link_env) -> None:
    env = link_env()

    iphone_first = _pair(env, label="iPhone")
    ipad = _pair(env, label="iPad")
    iphone_second = _pair(env, label="iPhone")

    entries = link_routes._authorized().snapshot()
    assert len(entries) == 2
    assert link_routes._authorized().is_authorized(iphone_first["fingerprint"]) is False
    assert link_routes._authorized().is_authorized(ipad["fingerprint"]) is True
    assert link_routes._authorized().is_authorized(iphone_second["fingerprint"]) is True


def test_repair_blank_label_retires_nothing_and_adds_both(link_env) -> None:
    env = link_env()

    first = _pair(env, label="")
    second = _pair(env, label="")

    entries = _entries_for_label("")
    assert len(entries) == 2
    assert link_routes._authorized().is_authorized(first["fingerprint"]) is True
    assert link_routes._authorized().is_authorized(second["fingerprint"]) is True


def test_device_repair_does_not_retire_peer_entry(link_env) -> None:
    env = link_env()

    peer = _pair(env, label="shared", role="peer")
    device = _pair(env, label="shared")

    assert link_routes._authorized().is_authorized(peer["fingerprint"]) is True
    assert link_routes._authorized().is_authorized(device["fingerprint"]) is True


def test_peer_pairing_runs_no_retire_pass(link_env) -> None:
    env = link_env()

    device = _pair(env, label="shared")
    peer = _pair(env, label="shared", role="peer")

    assert link_routes._authorized().is_authorized(device["fingerprint"]) is True
    assert link_routes._authorized().is_authorized(peer["fingerprint"]) is True


def test_repair_retire_failure_does_not_fail_pair(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = link_env()
    first = _pair(env, label="X")

    def raise_remove(self, fingerprint: str) -> bool:
        raise RuntimeError("remove failed")

    monkeypatch.setattr(link_routes.AuthorizedClients, "remove", raise_remove)
    caplog.set_level(logging.WARNING, logger=link_routes.__name__)
    started = env.client.post(
        "/app/network/pair-start",
        json={"device_label": "X", "role": ""},
    )
    assert started.status_code == 200

    response = env.client.post(
        f"/app/network/pair?token={started.get_json()['nonce']}",
        json={"csr": _make_csr("X")},
    )

    assert response.status_code == 200
    second = response.get_json()
    assert link_routes._authorized().is_authorized(second["fingerprint"]) is True
    assert link_routes._authorized().is_authorized(first["fingerprint"]) is True
    assert any("auto-retire failed" in record.message for record in caplog.records)


def test_repair_emits_honest_device_superseded_signal(
    link_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = link_env()
    calls = []

    def mock_emit(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    monkeypatch.setattr(link_routes, "emit", mock_emit)

    first = _pair(env, label="Jer's iPhone")
    superseded_calls = [
        (args, kwargs)
        for args, kwargs in calls
        if args == ("link", "device_superseded")
    ]
    assert superseded_calls == []

    second = _pair(env, label="Jer's iPhone")

    superseded_calls = [
        (args, kwargs)
        for args, kwargs in calls
        if args == ("link", "device_superseded")
    ]
    assert len(superseded_calls) == 1
    _args, kwargs = superseded_calls[0]
    assert kwargs["device_label"] == "Jer's iPhone"
    assert kwargs["retired_fingerprint"] == first["fingerprint"]
    assert kwargs["replaced_by"] == second["fingerprint"]
