# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from solstone.apps.network.routes import _build_pair_link
from solstone.think.link import join_cli
from solstone.think.link.ca import generate_ca, sign_csr

PAIR_LINK = _build_pair_link("10.0.0.42", 7657, "a" * 32, "b" * 64)


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        home="http://receiver",
        code=PAIR_LINK,
        as_role="peer",
        label="my-peer",
    )


def _pair_response(
    tmp_path: Path,
    *,
    csr_pem: str,
    device_label: str,
    local_endpoints: list[dict[str, object]] | None = None,
) -> join_cli.PairResponse:
    ca = generate_ca(tmp_path / "ca")
    ca_pem = ca.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    client_cert, _fingerprint = sign_csr(ca, csr_pem, device_label)
    return join_cli.PairResponse(
        client_cert=client_cert,
        ca_chain=[ca_pem],
        instance_id="inst-1",
        home_label="solstone",
        home_attestation="header.payload.signature",
        local_endpoints=local_endpoints
        if local_endpoints is not None
        else [{"host": "127.0.0.1", "port": 7657}],
    )


def _mock_post_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_post_pair(
        _request: join_cli.DirectPairRequest,
        body: dict[str, str],
        _private_key: object,
    ) -> join_cli.PairResponse:
        return _pair_response(
            tmp_path,
            csr_pem=body["csr"],
            device_label=body["device_label"],
            local_endpoints=[{"host": "8.8.8.8", "port": 7657}],
        )

    monkeypatch.setattr(join_cli, "_post_pair", fake_post_pair)


def test_pair_link_happy_path_writes_peer_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _mock_post_pair(monkeypatch, tmp_path)

    result = join_cli.main(_args())

    assert result == 0
    bundle = tmp_path / "journal" / "peers" / "inst-1"
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
    for name in join_cli.BUNDLE_FILES:
        assert (bundle / name).exists()
        assert stat.S_IMODE((bundle / name).stat().st_mode) == 0o600
    peer = json.loads((bundle / "peer.json").read_text("utf-8"))
    assert list(peer.keys()) == [
        "label",
        "paired_at",
        "instance_id",
        "home_label",
        "fingerprint",
        "local_endpoints",
        "role",
    ]
    assert peer["role"] == "peer"
    assert peer["instance_id"] == "inst-1"
    assert peer["label"] == "my-peer"
    assert peer["local_endpoints"] == [{"host": "8.8.8.8", "port": 7657}]
    ca_cert = x509.load_pem_x509_certificate((bundle / "chain.pem").read_bytes())
    client_cert = x509.load_pem_x509_certificate((bundle / "cert.pem").read_bytes())
    join_cli._verify_leaf_signed_by_pinned_ca(client_cert, ca_cert)
    assert not (tmp_path / "xdg" / "solstone-observer" / "spl" / "my-peer").exists()
