# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from solstone.think.services.spp_attest.ratls import channel
from solstone.think.services.spp_attest.ratls.contract import (
    EXPORTER_BYTES,
    EXPORTER_PROOF_MEDIA_TYPE,
    EXPORTER_PROOF_PATH,
    PREFACE_MAGIC,
)
from solstone.think.services.spp_attest.snp import Policy


def test_establish_attested_channel_passes_policy_to_exporter_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_nonce = b"n" * 32
    proof_der = b"proof-der"
    tls_exporter = b"e" * EXPORTER_BYTES
    tls_spki_der = b"tls-spki"
    evidence = object()
    verdict = object()
    policy = Policy(pcr_mode="pin", pcr_pins={"abc"})
    captured_certificate: dict[str, object] = {}
    captured_exporter: dict[str, object] = {}
    connections: list[object] = []

    class FakeRawSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []
            self.timeout = 30.0
            self.closed = False

        def sendall(self, data: bytes) -> None:
            self.sent.append(data)

        def settimeout(self, timeout: float | None) -> None:
            self.timeout = timeout

        def close(self) -> None:
            self.closed = True

    class FakeCertificate:
        def public_bytes(self, _encoding) -> bytes:
            return b"certificate-der"

    class FakePeer:
        def to_cryptography(self) -> FakeCertificate:
            return FakeCertificate()

    class FakeConnection:
        def __init__(self, context, raw) -> None:
            self.context = context
            self.raw = raw
            self.sent: list[bytes] = []
            self.recv_chunks = [
                (
                    b"HTTP/1.1 200 OK\r\n"
                    + f"Content-Type: {EXPORTER_PROOF_MEDIA_TYPE}\r\n".encode("ascii")
                    + f"Content-Length: {len(proof_der)}\r\n\r\n".encode("ascii")
                    + proof_der
                )
            ]
            self.closed = False
            connections.append(self)

        def setblocking(self, _flag: int) -> None:
            pass

        def set_connect_state(self) -> None:
            pass

        def set_tlsext_host_name(self, _name: bytes) -> None:
            pass

        def do_handshake(self) -> None:
            pass

        def get_peer_certificate(self) -> FakePeer:
            return FakePeer()

        def export_keying_material(
            self,
            _label: bytes,
            _size: int,
            _context: bytes,
        ) -> bytes:
            return tls_exporter

        def sendall(self, data: bytes) -> None:
            self.sent.append(data)

        def recv(self, _size: int) -> bytes:
            return self.recv_chunks.pop(0) if self.recv_chunks else b""

        def close(self) -> None:
            self.closed = True

    raw = FakeRawSocket()
    monkeypatch.setattr(
        channel.socket,
        "create_connection",
        lambda _address, timeout: raw,
    )
    monkeypatch.setattr(channel, "_tls_context", lambda: object())
    monkeypatch.setattr(channel.SSL, "Connection", FakeConnection)

    def fake_verify_certificate_evidence(**kwargs):
        captured_certificate.update(kwargs)
        return SimpleNamespace(
            tls_spki_der=tls_spki_der,
            evidence=evidence,
            verdict=verdict,
        )

    def fake_verify_exporter_proof(**kwargs):
        captured_exporter.update(kwargs)

    monkeypatch.setattr(
        channel,
        "verify_certificate_evidence",
        fake_verify_certificate_evidence,
    )
    monkeypatch.setattr(channel, "verify_exporter_proof", fake_verify_exporter_proof)

    established = channel.establish_attested_channel(
        channel.RatlsEndpoint("spp.example.test", 9443),
        owner_nonce=owner_nonce,
        nvattest_dir=tmp_path,
        now=datetime(2026, 7, 24, tzinfo=timezone.utc),
        policy=policy,
        composite_verifier=lambda *_args, **_kwargs: verdict,
        monotonic_now=lambda: 123.0,
        epoch=7,
    )

    assert raw.sent == [PREFACE_MAGIC + owner_nonce]
    assert raw.timeout is None
    assert connections
    assert connections[0].sent == [
        (
            f"GET {EXPORTER_PROOF_PATH} HTTP/1.1\r\n"
            "Host: spp-engine\r\n"
            "Content-Length: 0\r\n\r\n"
        ).encode("ascii")
    ]
    assert captured_certificate["policy"] is policy
    assert captured_exporter == {
        "proof_der": proof_der,
        "evidence": evidence,
        "tls_exporter": tls_exporter,
        "owner_nonce": owner_nonce,
        "policy": policy,
    }
    assert established.verdict is verdict
    assert established.epoch == 7
    assert established.last_used_monotonic == 123.0
