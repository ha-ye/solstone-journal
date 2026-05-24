# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from solstone.think.link.bundle import load_client_identity
from solstone.think.link.ca import cert_fingerprint, generate_ca


def _write_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "peer"
    bundle.mkdir()
    ca = generate_ca(tmp_path / "ca")
    cert_pem = ca.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    (bundle / "private.pem").write_text("private", encoding="utf-8")
    (bundle / "cert.pem").write_text(cert_pem, encoding="utf-8")
    (bundle / "chain.pem").write_text(cert_pem, encoding="utf-8")
    (bundle / "home_attestation.jwt").write_text("jwt", encoding="utf-8")
    (bundle / "peer.json").write_text(
        json.dumps(
            {
                "label": "host-a",
                "instance_id": "12345678-1234-1234-1234-123456789abc",
                "home_label": "solstone",
                "local_endpoints": [{"ip": "127.0.0.1", "port": 7657}],
            }
        ),
        encoding="utf-8",
    )
    return bundle


def test_load_client_identity_happy_path(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path)
    cert_pem = (bundle / "cert.pem").read_text(encoding="utf-8")

    identity = load_client_identity(bundle)

    assert identity.private_key_pem == "private"
    assert identity.fingerprint == cert_fingerprint(cert_pem)
    assert identity.home_instance_id == "12345678-1234-1234-1234-123456789abc"
    assert identity.home_label == "solstone"
    assert identity.home_attestation == "jwt"
    assert identity.local_endpoints == ({"ip": "127.0.0.1", "port": 7657},)


def test_load_client_identity_missing_file(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path)
    (bundle / "chain.pem").unlink()

    with pytest.raises(ValueError, match="missing PL bundle file") as exc_info:
        load_client_identity(bundle)

    assert str(bundle / "chain.pem") in str(exc_info.value)


def test_load_client_identity_bad_peer_json(tmp_path: Path) -> None:
    bundle = _write_bundle(tmp_path)
    (bundle / "peer.json").write_text("{", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid peer.json") as exc_info:
        load_client_identity(bundle)

    assert str(bundle) in str(exc_info.value)
