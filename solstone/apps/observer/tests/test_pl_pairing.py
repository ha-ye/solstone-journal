# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for role-less PL link pairing before observer self-registration."""

from __future__ import annotations

import io
import json
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from solstone.apps.link import routes as link_routes
from solstone.apps.link.tests.conftest import _StubWatcher
from solstone.convey.secure_listener import ConveyIdentity
from solstone.think.link.local_endpoints import LocalEndpoint
from solstone.think.link.nonces import Nonce


@pytest.fixture
def pair_env(tmp_path, monkeypatch):
    def _create():
        journal = tmp_path / "journal"
        journal.mkdir(exist_ok=True)

        config_dir = journal / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "journal.json").write_text(
            json.dumps(
                {
                    "setup": {"completed_at": 1700000000000},
                },
                indent=2,
            )
        )
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

        from solstone.convey import create_app

        app = create_app(journal=str(journal))
        client = app.test_client()
        monkeypatch.setattr(
            link_routes,
            "get_interface_watcher",
            lambda: _StubWatcher(
                [LocalEndpoint(ip="192.168.1.50", port=7657, scope="lan")]
            ),
        )

        class Env:
            def __init__(self) -> None:
                self.journal = journal
                self.app = app
                self.client = client

        return Env()

    return _create


def _make_csr(label: str = "test") -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _start_pair(env, *, role: str = "", label: str = "Pair Device") -> dict:
    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": label, "role": role},
    )
    assert response.status_code == 200
    return response.get_json()


def _pair(env, *, role: str = "", label: str = "Pair Device") -> dict:
    started = _start_pair(env, role=role, label=label)
    response = env.client.post(
        "/app/link/pair",
        json={"nonce": started["nonce"], "csr": _make_csr(label)},
    )
    assert response.status_code == 200
    return response.get_json()


def _pl_identity(fingerprint: str, *, label: str = "Owner Phone") -> ConveyIdentity:
    return ConveyIdentity(
        mode="pl-via-spl",
        fingerprint=fingerprint,
        device_label=label,
        paired_at="2026-05-20T00:00:00Z",
        session_id="session-1",
    )


def _observer_record_paths(env) -> list:
    return sorted((env.journal / "apps" / "observer" / "observers").glob("*.json"))


def _journal_source_paths(env) -> list:
    return sorted((env.journal / "apps" / "import" / "journal_sources").glob("*.json"))


def test_role_less_pairing_does_not_mint_observer_record(pair_env) -> None:
    env = pair_env()

    response = _pair(env, label="Linked System")

    assert _observer_record_paths(env) == []
    entries = link_routes._authorized().snapshot()
    assert len(entries) == 1
    assert entries[0].fingerprint == response["fingerprint"]
    assert entries[0].role == ""


def test_role_less_pl_ingest_returns_auth_required(pair_env) -> None:
    env = pair_env()
    response = _pair(env, label="Linked System")

    ingest = env.client.post(
        "/app/observer/ingest",
        environ_overrides={
            "pl.identity": _pl_identity(response["fingerprint"], label="Owner Phone")
        },
        data={
            "day": "20250103",
            "segment": "120000_300",
            "files": (io.BytesIO(b"phone content"), "phone.txt"),
        },
    )

    assert _observer_record_paths(env) == []
    assert ingest.status_code == 401
    assert ingest.get_json()["reason_code"] == "auth_required"


def test_attestation_failure_does_not_write_observer_or_authorized(
    pair_env,
    monkeypatch,
) -> None:
    env = pair_env()

    def fail_attestation(*args, **kwargs):
        raise RuntimeError("attestation failed")

    class Authorized:
        def add(self, *args, **kwargs) -> None:
            pytest.fail("authorized add should not run after attestation failure")

    monkeypatch.setattr(link_routes, "mint_attestation", fail_attestation)
    monkeypatch.setattr(link_routes, "_authorized", lambda: Authorized())
    now = int(time.time())
    consumed = Nonce(
        value="nonce",
        device_label="Observer Laptop",
        issued_at=now,
        expires_at=now + 300,
        used=True,
        role="",
    )

    with pytest.raises(RuntimeError, match="attestation failed"):
        link_routes._complete_pairing(
            consumed,
            _make_csr("attestation"),
            "Observer Laptop",
            "",
            network="network",
        )

    assert _observer_record_paths(env) == []


def test_peer_journal_source_rolls_back_when_authorized_add_fails(
    pair_env,
    monkeypatch,
) -> None:
    env = pair_env()

    class BrokenAuthorized:
        def add(self, *args, **kwargs) -> None:
            raise RuntimeError("ledger write failed")

    monkeypatch.setattr(link_routes, "_authorized", lambda: BrokenAuthorized())
    now = int(time.time())
    consumed = Nonce(
        value="nonce",
        device_label="Peer Laptop",
        issued_at=now,
        expires_at=now + 300,
        used=True,
        role="peer",
    )

    with pytest.raises(RuntimeError, match="ledger write failed"):
        link_routes._complete_pairing(
            consumed,
            _make_csr("rollback"),
            "Peer Laptop",
            "",
            network="network",
        )

    assert _observer_record_paths(env) == []
    assert _journal_source_paths(env) == []
