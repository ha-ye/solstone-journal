# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from OpenSSL import SSL

from solstone.convey.secure_listener.tls import (
    TlsError,
    build_relaxed_server_context,
    build_server_context,
    drive_tls,
    issue_server_cert,
    new_server,
)
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.ca import LoadedCa, generate_ca, sign_csr
from solstone.think.link.client import (
    ClientIdentity,
    _build_no_cert_client_ctx,
    _build_tls_client_ctx,
    _drive_tls_client,
    _new_tls_client,
)
from solstone.think.link.join_cli import _build_csr
from solstone.think.link.tls import TlsError as ClientTlsError


def test_strict_context_rejects_no_cert(tmp_path: Path) -> None:
    ca, server_cert, server_key, authorized = _server_material(tmp_path)
    server_ctx = build_server_context(ca, server_cert, server_key, authorized)
    client_ctx = _build_no_cert_client_ctx()

    with pytest.raises((TlsError, ClientTlsError)):
        _complete_handshake(server_ctx, client_ctx)


def test_relaxed_context_accepts_no_cert_with_none_fingerprint(tmp_path: Path) -> None:
    ca, server_cert, server_key, authorized = _server_material(tmp_path)
    server_ctx = build_relaxed_server_context(ca, server_cert, server_key, authorized)
    client_ctx = _build_no_cert_client_ctx()

    server = _complete_handshake(server_ctx, client_ctx)

    assert server.peer_fingerprint is None


def test_relaxed_context_keeps_allowlisted_cert_fingerprint(tmp_path: Path) -> None:
    ca, server_cert, server_key, authorized = _server_material(tmp_path)
    private_key_bytes, csr_pem = _build_csr("pytest phone")
    private_key_pem = private_key_bytes.decode("ascii")
    client_cert_pem, fingerprint = sign_csr(ca, csr_pem, "pytest phone")
    authorized.add(fingerprint, "pytest phone", "inst-1")
    server_ctx = build_relaxed_server_context(ca, server_cert, server_key, authorized)
    client_ctx = _build_tls_client_ctx(
        ClientIdentity(
            private_key_pem=private_key_pem,
            client_cert_pem=client_cert_pem,
            ca_chain_pem=ca.cert.public_bytes(serialization.Encoding.PEM).decode(
                "ascii"
            ),
            fingerprint=fingerprint,
            home_instance_id="inst-1",
            home_label="home",
            home_attestation="attest",
        )
    )

    server = _complete_handshake(server_ctx, client_ctx)

    assert server.peer_fingerprint == fingerprint


def test_relaxed_context_rejects_unauthorized_cert(tmp_path: Path) -> None:
    ca, server_cert, server_key, authorized = _server_material(tmp_path)
    private_key_bytes, csr_pem = _build_csr("pytest stranger")
    private_key_pem = private_key_bytes.decode("ascii")
    client_cert_pem, fingerprint = sign_csr(ca, csr_pem, "pytest stranger")
    server_ctx = build_relaxed_server_context(ca, server_cert, server_key, authorized)
    client_ctx = _build_tls_client_ctx(
        ClientIdentity(
            private_key_pem=private_key_pem,
            client_cert_pem=client_cert_pem,
            ca_chain_pem=ca.cert.public_bytes(serialization.Encoding.PEM).decode(
                "ascii"
            ),
            fingerprint=fingerprint,
            home_instance_id="inst-1",
            home_label="home",
            home_attestation="attest",
        )
    )

    with pytest.raises((TlsError, ClientTlsError)):
        _complete_handshake(server_ctx, client_ctx)


def test_tls_drive_transits_frame_larger_than_one_record(tmp_path: Path) -> None:
    """A plaintext frame larger than one TLS record (16 KiB) must transit fully.

    pyOpenSSL's Connection.send() encrypts at most one record per call and
    returns the partial count; the TLS drives must loop (sendall) or a frame
    larger than 16 KiB is silently truncated on the wire and the peer's frame
    decoder blocks forever waiting for the dropped tail. Regression for the
    link-tunnel >16 KiB observer-segment-upload timeout (the partial-write bug).
    """
    ca, server_cert, server_key, authorized = _server_material(tmp_path)
    server_ctx = build_relaxed_server_context(ca, server_cert, server_key, authorized)
    client_ctx = _build_no_cert_client_ctx()

    server = new_server(server_ctx)
    client = _new_tls_client(client_ctx)
    c2s = b""
    s2c = b""
    for _ in range(100):
        client_out, _ = _drive_tls_client(client, inbound=s2c)
        s2c = b""
        c2s += client_out
        server_out, _ = drive_tls(server, inbound=c2s)
        c2s = b""
        s2c += server_out
        if server.handshake_done and client.handshake_done:
            break
    else:
        raise AssertionError("TLS handshake did not complete")

    payload = b"A" * (200 * 1024)  # ~13 TLS records; one send() would carry one

    # client -> server (the observer-upload direction)
    client_out, _ = _drive_tls_client(client, inbound=s2c, plaintext_out=payload)
    s2c = b""
    _, server_in = drive_tls(server, inbound=c2s + client_out)
    c2s = b""
    assert server_in == payload

    # server -> client (the response/download direction)
    server_out, _ = drive_tls(server, inbound=b"", plaintext_out=payload)
    _, client_in = _drive_tls_client(client, inbound=s2c + server_out)
    assert client_in == payload


def _server_material(
    tmp_path: Path,
) -> tuple[LoadedCa, object, bytes, AuthorizedClients]:
    ca = generate_ca(tmp_path / "ca")
    server_cert, server_key = issue_server_cert(ca)
    authorized = AuthorizedClients(tmp_path / "authorized_clients.json")
    return ca, server_cert, server_key, authorized


def _complete_handshake(server_ctx: SSL.Context, client_ctx: SSL.Context):
    server = new_server(server_ctx)
    client = _new_tls_client(client_ctx)
    client_to_server = b""
    server_to_client = b""

    for _ in range(100):
        client_out, _ = _drive_tls_client(client, inbound=server_to_client)
        server_to_client = b""
        client_to_server += client_out

        server_out, _ = drive_tls(server, inbound=client_to_server)
        client_to_server = b""
        server_to_client += server_out

        if server.handshake_done and client.handshake_done:
            return server

    raise AssertionError("TLS handshake did not complete")
