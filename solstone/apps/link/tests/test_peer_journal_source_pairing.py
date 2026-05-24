# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import time
from importlib import import_module

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from solstone.apps.link import routes as link_routes
from solstone.apps.observer.utils import load_observer_by_fingerprint
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.nonces import Nonce

journal_sources = import_module("solstone.apps.import.journal_sources")


def _make_csr(label: str = "test") -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _start_pair(env, *, role: str, label: str = "Pair Device") -> dict:
    response = env.client.post(
        "/app/link/pair-start",
        json={"device_label": label, "role": role},
    )
    assert response.status_code == 200
    return response.get_json()


def _pair(env, *, role: str, label: str = "Pair Device") -> dict:
    started = _start_pair(env, role=role, label=label)
    response = env.client.post(
        "/app/link/pair",
        json={"nonce": started["nonce"], "csr": _make_csr(label)},
    )
    assert response.status_code == 200
    return response.get_json()


def _consumed_nonce(role: str, label: str = "Peer Laptop") -> Nonce:
    now = int(time.time())
    return Nonce(
        value="nonce",
        device_label=label,
        issued_at=now,
        expires_at=now + 300,
        used=True,
        manual_code=None,
        role=role,
    )


def _journal_source_paths(env) -> list:
    return sorted((env.journal / "apps" / "import" / "journal_sources").glob("*.json"))


def _import_dirs(env) -> list:
    imports_dir = env.journal / "imports"
    if not imports_dir.exists():
        return []
    return sorted(path for path in imports_dir.glob("*") if path.is_dir())


def _authorized_entries() -> list:
    return AuthorizedClients(link_routes.authorized_clients_path()).snapshot()


def _state_dir(env, fingerprint: str):
    prefix = fingerprint.replace("sha256:", "")[:16]
    return env.journal / "imports" / prefix


def test_peer_role_pairing_mints_journal_source_state_dir_and_authorized(
    link_env,
) -> None:
    env = link_env()

    response = _pair(env, role="peer", label="Peer Laptop")

    source = journal_sources.load_journal_source_by_fingerprint(response["fingerprint"])
    assert source is not None
    assert source["pair_mode"] == "pl"
    assert source["fingerprint"] == response["fingerprint"]
    assert source["device_label"] == "Peer Laptop"
    assert "key" not in source
    assert _state_dir(env, response["fingerprint"]).is_dir()
    assert (
        _state_dir(env, response["fingerprint"]) / "segments" / "state.json"
    ).exists()

    entries = link_routes._authorized().snapshot()
    assert len(entries) == 1
    assert entries[0].fingerprint == response["fingerprint"]
    assert entries[0].role == "peer"


def test_phone_role_pairing_does_not_mint_journal_source(link_env) -> None:
    env = link_env()

    response = _pair(env, role="phone", label="Owner Phone")

    assert (
        journal_sources.load_journal_source_by_fingerprint(response["fingerprint"])
        is None
    )
    assert _journal_source_paths(env) == []
    entries = link_routes._authorized().snapshot()
    assert len(entries) == 1
    assert entries[0].role == "phone"


def test_observer_role_pairing_mints_observer_not_journal_source(link_env) -> None:
    env = link_env()

    response = _pair(env, role="observer", label="Observer Laptop")

    assert load_observer_by_fingerprint(response["fingerprint"]) is not None
    assert (
        journal_sources.load_journal_source_by_fingerprint(response["fingerprint"])
        is None
    )
    assert _journal_source_paths(env) == []
    entries = link_routes._authorized().snapshot()
    assert len(entries) == 1
    assert entries[0].role == "observer"


def test_peer_journal_source_mint_failure_does_not_add_authorized(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()

    def fail_mint(*args, **kwargs):
        raise RuntimeError("journal source mint failed")

    class Authorized:
        def add(self, *args, **kwargs) -> None:
            pytest.fail("authorized add should not run after mint failure")

    monkeypatch.setattr(link_routes, "mint_pl_journal_source_record", fail_mint)
    monkeypatch.setattr(link_routes, "_authorized", lambda: Authorized())

    with pytest.raises(RuntimeError, match="journal source mint failed"):
        link_routes._complete_pairing(
            _consumed_nonce("peer"), _make_csr("mint"), "Peer Laptop"
        )

    assert _journal_source_paths(env) == []


def test_peer_route_mint_failure_returns_500_without_side_effects(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()

    def fail_mint(*args, **kwargs):
        raise RuntimeError("journal source mint failed")

    class Authorized:
        def add(self, *args, **kwargs) -> None:
            pytest.fail("authorized add should not run after mint failure")

    monkeypatch.setattr(link_routes, "mint_pl_journal_source_record", fail_mint)
    monkeypatch.setattr(link_routes, "_authorized", lambda: Authorized())

    started = _start_pair(env, role="peer", label="Peer Laptop")
    response = env.client.post(
        "/app/link/pair",
        json={"nonce": started["nonce"], "csr": _make_csr("mint")},
    )

    assert response.status_code == 500
    assert _journal_source_paths(env) == []
    assert _import_dirs(env) == []
    assert _authorized_entries() == []


def test_peer_state_dir_failure_unlinks_journal_source_and_skips_authorized(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()

    def fail_state_dir(*args, **kwargs):
        raise RuntimeError("state dir failed")

    class Authorized:
        def add(self, *args, **kwargs) -> None:
            pytest.fail("authorized add should not run after state dir failure")

    monkeypatch.setattr(link_routes, "create_state_directory", fail_state_dir)
    monkeypatch.setattr(link_routes, "_authorized", lambda: Authorized())

    with pytest.raises(RuntimeError, match="state dir failed"):
        link_routes._complete_pairing(
            _consumed_nonce("peer"), _make_csr("state-dir"), "Peer Laptop"
        )

    assert _journal_source_paths(env) == []


def test_peer_route_state_dir_failure_returns_500_and_unlinks_journal_source(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()

    def fail_state_dir(*args, **kwargs):
        raise RuntimeError("state dir failed")

    class Authorized:
        def add(self, *args, **kwargs) -> None:
            pytest.fail("authorized add should not run after state dir failure")

    monkeypatch.setattr(link_routes, "create_state_directory", fail_state_dir)
    monkeypatch.setattr(link_routes, "_authorized", lambda: Authorized())

    started = _start_pair(env, role="peer", label="Peer Laptop")
    response = env.client.post(
        "/app/link/pair",
        json={"nonce": started["nonce"], "csr": _make_csr("state-dir")},
    )

    assert response.status_code == 500
    assert _journal_source_paths(env) == []
    assert _import_dirs(env) == []
    assert _authorized_entries() == []


def test_peer_journal_source_rolls_back_when_authorized_add_fails(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()

    class BrokenAuthorized:
        def add(self, *args, **kwargs) -> None:
            raise RuntimeError("ledger write failed")

    monkeypatch.setattr(link_routes, "_authorized", lambda: BrokenAuthorized())

    with pytest.raises(RuntimeError, match="ledger write failed"):
        link_routes._complete_pairing(
            _consumed_nonce("peer"), _make_csr("rollback"), "Peer Laptop"
        )

    assert _journal_source_paths(env) == []
    import_dirs = sorted(
        path for path in (env.journal / "imports").glob("*") if path.is_dir()
    )
    assert len(import_dirs) == 1
    assert (import_dirs[0] / "segments" / "state.json").exists()


def test_peer_route_authorized_failure_returns_500_unlinks_record_and_keeps_state_dir(
    link_env,
    monkeypatch,
) -> None:
    env = link_env()

    class BrokenAuthorized:
        def add(self, *args, **kwargs) -> None:
            raise RuntimeError("ledger write failed")

    monkeypatch.setattr(link_routes, "_authorized", lambda: BrokenAuthorized())

    started = _start_pair(env, role="peer", label="Peer Laptop")
    response = env.client.post(
        "/app/link/pair",
        json={"nonce": started["nonce"], "csr": _make_csr("authorized")},
    )

    assert response.status_code == 500
    assert _journal_source_paths(env) == []
    assert _authorized_entries() == []
    import_dirs = _import_dirs(env)
    assert len(import_dirs) == 1
    assert (import_dirs[0] / "segments" / "state.json").exists()
