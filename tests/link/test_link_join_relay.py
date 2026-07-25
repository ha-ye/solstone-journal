# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from solstone.apps.network.relay_link import (
    decode_pair_window_link,
    derive_rk,
    encode_pair_window_link,
)
from solstone.think.link import join_cli
from solstone.think.link.ca import LoadedCa, load_or_generate_ca, sign_csr
from solstone.think.link.client import ClientIdentity
from solstone.think.link.mark import jid_from_spki
from solstone.think.link.paths import DEFAULT_RELAY_URL

S = bytes.fromhex("0123456789abcdef")
CA_FP_SPKI = "deadbeefcafebabe0123456789abcdef"


def _spki_der(ca: LoadedCa) -> bytes:
    return ca.cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _signed_leaf(
    ca: LoadedCa, label: str = "relay-client"
) -> tuple[str, x509.Certificate]:
    _private_key, _private_key_pem, csr_pem = join_cli._build_csr(label)
    cert_pem, _fingerprint = sign_csr(ca, csr_pem, label)
    leaf = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
    return cert_pem, leaf


def _ca_pem(ca: LoadedCa) -> str:
    return ca.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _pair_response(
    ca: LoadedCa,
    client_cert: str,
    *,
    instance_id: str,
) -> join_cli.PairResponse:
    return join_cli.PairResponse(
        client_cert=client_cert,
        ca_chain=[_ca_pem(ca)],
        instance_id=instance_id,
        home_label="solstone",
        home_attestation="header.payload.signature",
        local_endpoints=[
            {"host": "127.0.0.1", "port": 7657},
            {"host": "8.8.8.8", "port": 7657},
        ],
    )


def _relay_request() -> join_cli.RelayPairRequest:
    return join_cli.RelayPairRequest(
        relay_endpoint="https://relay.example",
        rk=derive_rk(S),
        s=S,
        ca_fp_spki=bytes.fromhex(CA_FP_SPKI),
        inner_path=f"/app/network/pair?token={S.hex()}",
    )


def test_parse_relay_pair_link_default_origin() -> None:
    link = encode_pair_window_link(S, CA_FP_SPKI, relay_origin=None)

    req = join_cli._parse_pair_request(link, None)

    assert isinstance(req, join_cli.RelayPairRequest)
    assert req.relay_endpoint == DEFAULT_RELAY_URL
    assert req.s == S
    assert req.rk == derive_rk(S)
    assert req.ca_fp_spki == bytes.fromhex(CA_FP_SPKI)
    assert req.inner_path == f"/app/network/pair?token={S.hex()}"


def test_parse_relay_pair_link_custom_origin() -> None:
    link = encode_pair_window_link(
        S,
        CA_FP_SPKI,
        relay_origin="https://relay.example",
    )
    parsed = decode_pair_window_link(link)

    req = join_cli._parse_pair_request(link, None)

    assert parsed.relay_origin == "https://relay.example"
    assert isinstance(req, join_cli.RelayPairRequest)
    assert req.relay_endpoint == "https://relay.example"
    assert req.s == S
    assert req.rk == derive_rk(S)
    assert req.inner_path == f"/app/network/pair?token={S.hex()}"


def test_relay_pair_link_dispatch_does_not_touch_direct_decoder_or_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = encode_pair_window_link(S, CA_FP_SPKI, relay_origin="https://relay.example")

    def fail_direct(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("relay dispatch should not touch direct path")

    monkeypatch.setattr(join_cli, "_decode_direct_pair_blob", fail_direct)
    monkeypatch.setattr(join_cli, "is_direct_pair_candidate_allowed", fail_direct)

    req = join_cli._parse_pair_request(link, None)

    assert isinstance(req, join_cli.RelayPairRequest)


def test_verify_relay_pair_accepts_matching_pin_leaf_and_jid(tmp_path: Path) -> None:
    ca = load_or_generate_ca(tmp_path / "ca")
    client_cert, leaf = _signed_leaf(ca)
    spki_der = _spki_der(ca)
    response = _pair_response(
        ca,
        client_cert,
        instance_id=str(jid_from_spki(spki_der)),
    )

    join_cli._verify_relay_pair(
        response,
        leaf,
        hashlib.sha256(spki_der).digest()[:16],
    )


def test_verify_relay_pair_rejects_wrong_spki_pin(tmp_path: Path) -> None:
    ca = load_or_generate_ca(tmp_path / "ca")
    client_cert, leaf = _signed_leaf(ca)
    spki_der = _spki_der(ca)
    response = _pair_response(
        ca,
        client_cert,
        instance_id=str(jid_from_spki(spki_der)),
    )

    with pytest.raises(ValueError, match="CA fingerprint mismatch"):
        join_cli._verify_relay_pair(response, leaf, b"\x00" * 16)


def test_verify_relay_pair_rejects_leaf_from_different_ca(tmp_path: Path) -> None:
    ca = load_or_generate_ca(tmp_path / "ca")
    other_ca = load_or_generate_ca(tmp_path / "other-ca")
    client_cert, _leaf = _signed_leaf(ca)
    _other_cert, other_leaf = _signed_leaf(other_ca)
    spki_der = _spki_der(ca)
    response = _pair_response(
        ca,
        client_cert,
        instance_id=str(jid_from_spki(spki_der)),
    )

    with pytest.raises(ValueError, match="not signed by the pinned CA"):
        join_cli._verify_relay_pair(
            response,
            other_leaf,
            hashlib.sha256(spki_der).digest()[:16],
        )


def test_verify_relay_pair_rejects_wrong_instance_id(tmp_path: Path) -> None:
    ca = load_or_generate_ca(tmp_path / "ca")
    client_cert, leaf = _signed_leaf(ca)
    spki_der = _spki_der(ca)
    response = _pair_response(
        ca,
        client_cert,
        instance_id="00000000-0000-4000-8000-000000000000",
    )

    with pytest.raises(ValueError, match="instance_id does not match"):
        join_cli._verify_relay_pair(
            response,
            leaf,
            hashlib.sha256(spki_der).digest()[:16],
        )


def test_verify_relay_pair_rejects_missing_peer_leaf(tmp_path: Path) -> None:
    ca = load_or_generate_ca(tmp_path / "ca")
    client_cert, _leaf = _signed_leaf(ca)
    spki_der = _spki_der(ca)
    response = _pair_response(
        ca,
        client_cert,
        instance_id=str(jid_from_spki(spki_der)),
    )

    with pytest.raises(ValueError, match="presented no certificate"):
        join_cli._verify_relay_pair(
            response,
            None,
            hashlib.sha256(spki_der).digest()[:16],
        )


def test_join_via_relay_enrolls_then_writes_observer_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    ca = load_or_generate_ca(tmp_path / "ca")
    client_cert, _leaf = _signed_leaf(ca)
    spki_der = _spki_der(ca)
    response = _pair_response(
        ca,
        client_cert,
        instance_id=str(jid_from_spki(spki_der)),
    )
    req = _relay_request()
    enroll_calls: list[tuple[str, ClientIdentity]] = []

    def _record_post_pair(
        actual_req: join_cli.RelayPairRequest,
        body: dict[str, str],
    ) -> join_cli.PairResponse:
        assert actual_req is req
        assert body["device_label"] == "relay-laptop"
        assert "sender_instance_id" not in body
        assert body["csr"].startswith("-----BEGIN CERTIFICATE REQUEST-----")
        return response

    def _record_enroll(relay_endpoint: str, identity: ClientIdentity) -> object:
        enroll_calls.append((relay_endpoint, identity))
        return object()

    monkeypatch.setattr(join_cli, "_post_pair_relay", _record_post_pair)
    monkeypatch.setattr(
        join_cli.Client,
        "enroll_device",
        staticmethod(_record_enroll),
    )

    result = join_cli._join_via_relay(req, "relay-laptop", "")

    assert result == 0
    assert len(enroll_calls) == 1
    relay_endpoint, identity = enroll_calls[0]
    assert relay_endpoint == req.relay_endpoint
    assert identity.home_instance_id == response.instance_id
    assert identity.home_label == "solstone"
    assert identity.client_cert_pem == response.client_cert
    bundle = tmp_path / "xdg" / "solstone-observer" / "spl" / "relay-laptop"
    assert sorted(path.name for path in bundle.iterdir()) == sorted(
        join_cli.BUNDLE_FILES
    )
    peer = json.loads((bundle / "peer.json").read_text("utf-8"))
    assert peer["label"] == "relay-laptop"
    assert peer["instance_id"] == response.instance_id
    assert peer["home_label"] == "solstone"
    assert peer["fingerprint"] == join_cli._ca_fingerprint(
        join_cli._join_chain(response.ca_chain)
    )
    assert peer["local_endpoints"] == response.local_endpoints
    assert peer["role"] == ""


def test_join_via_relay_does_not_write_bundle_when_enroll_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    ca = load_or_generate_ca(tmp_path / "ca")
    client_cert, _leaf = _signed_leaf(ca)
    spki_der = _spki_der(ca)
    response = _pair_response(
        ca,
        client_cert,
        instance_id=str(jid_from_spki(spki_der)),
    )
    req = _relay_request()

    def _reject_enroll(_relay_endpoint: str, _identity: ClientIdentity) -> object:
        raise RuntimeError("denied")

    monkeypatch.setattr(
        join_cli,
        "_post_pair_relay",
        lambda _req, _body: response,
    )
    monkeypatch.setattr(
        join_cli.Client,
        "enroll_device",
        staticmethod(_reject_enroll),
    )

    result = join_cli._join_via_relay(req, "relay-laptop", "")

    assert result == 1
    assert not (
        tmp_path / "xdg" / "solstone-observer" / "spl" / "relay-laptop"
    ).exists()
