# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Socket-backed RA-TLS channel establishment for SPP confidential transport."""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from OpenSSL import SSL

from solstone.think.services.spp_attest.composite import CompositeVerdict
from solstone.think.services.spp_attest.ratls.contract import (
    EXPORTER_BYTES,
    EXPORTER_LABEL,
    EXPORTER_PROOF_MEDIA_TYPE,
    EXPORTER_PROOF_PATH,
    PREFACE_MAGIC,
    exporter_context,
)
from solstone.think.services.spp_attest.ratls.verify import (
    RatlsVerificationError,
    VerifiedCertificateEvidence,
    verify_certificate_evidence,
    verify_exporter_proof,
)
from solstone.think.services.spp_attest.snp import Policy

MAX_PROOF_RESPONSE_HEADERS = 16 * 1024
MAX_PROOF_RESPONSE_BYTES = 8 * 1024 * 1024


class RatlsChannelError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"confidential attestation rejected ({reason_code})")


@dataclass(frozen=True, slots=True)
class RatlsEndpoint:
    host: str
    port: int
    server_name: bytes = b"spp-engine"


@dataclass(slots=True)
class AttestedChannel:
    raw_socket: socket.socket
    tls: SSL.Connection
    verified: VerifiedCertificateEvidence
    last_used_monotonic: float

    @property
    def verdict(self) -> CompositeVerdict:
        return self.verified.verdict

    def close(self) -> None:
        try:
            self.tls.shutdown()
        except Exception:
            pass
        try:
            self.tls.close()
        except Exception:
            pass
        try:
            self.raw_socket.close()
        except Exception:
            pass


def _tls_context() -> SSL.Context:
    context = SSL.Context(SSL.TLS_CLIENT_METHOD)
    context.set_min_proto_version(SSL.TLS1_3_VERSION)
    context.set_max_proto_version(SSL.TLS1_3_VERSION)
    context.set_verify(SSL.VERIFY_NONE, lambda *_args: True)
    return context


def _recv_proof_response(connection: SSL.Connection) -> bytes:
    data = bytearray()
    marker = b"\r\n\r\n"
    while marker not in data:
        if len(data) >= MAX_PROOF_RESPONSE_HEADERS:
            raise RatlsChannelError("proof_http_failed")
        chunk = connection.recv(min(4096, MAX_PROOF_RESPONSE_HEADERS - len(data)))
        if not chunk:
            raise RatlsChannelError("proof_http_failed")
        data.extend(chunk)

    head, body = bytes(data).split(marker, 1)
    lines = head.split(b"\r\n")
    if not lines or lines[0] != b"HTTP/1.1 200 OK":
        raise RatlsChannelError("proof_http_failed")
    content_length: int | None = None
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if not separator:
            raise RatlsChannelError("proof_http_failed")
        lowered = name.strip().lower()
        if (
            lowered == b"content-type"
            and value.strip() != EXPORTER_PROOF_MEDIA_TYPE.encode("ascii")
        ):
            raise RatlsChannelError("proof_http_failed")
        if lowered == b"content-length":
            try:
                content_length = int(value.strip())
            except ValueError as exc:
                raise RatlsChannelError("proof_http_failed") from exc
    if content_length is None or content_length > MAX_PROOF_RESPONSE_BYTES:
        raise RatlsChannelError("proof_http_failed")
    while len(body) < content_length:
        chunk = connection.recv(min(65536, content_length - len(body)))
        if not chunk:
            raise RatlsChannelError("proof_http_failed")
        body += chunk
    return body[:content_length]


def establish_attested_channel(
    endpoint: RatlsEndpoint,
    *,
    owner_nonce: bytes,
    nvattest_dir: Path,
    now: datetime,
    roots_dir: Path | None = None,
    policy: Policy | None = None,
    quote_verifier: Callable[..., None] | None = None,
    composite_verifier: Callable[..., CompositeVerdict],
    socket_timeout_s: float = 30.0,
    monotonic_now: Callable[[], float],
) -> AttestedChannel:
    raw: socket.socket | None = None
    connection: SSL.Connection | None = None
    try:
        raw = socket.create_connection(
            (endpoint.host, endpoint.port), timeout=socket_timeout_s
        )
        raw.sendall(PREFACE_MAGIC + owner_nonce)
        connection = SSL.Connection(_tls_context(), raw)
        connection.setblocking(1)
        connection.set_connect_state()
        connection.set_tlsext_host_name(endpoint.server_name)
        connection.do_handshake()

        peer = connection.get_peer_certificate()
        if peer is None:
            raise RatlsChannelError("tls_handshake_failed")
        certificate_der = peer.to_cryptography().public_bytes(
            serialization.Encoding.DER
        )
        verified = verify_certificate_evidence(
            certificate_der=certificate_der,
            owner_nonce=owner_nonce,
            now=now,
            nvattest_dir=nvattest_dir,
            roots_dir=roots_dir,
            policy=policy,
            quote_verifier=quote_verifier,
            composite_verifier=composite_verifier,
        )

        tls_exporter = connection.export_keying_material(
            EXPORTER_LABEL,
            EXPORTER_BYTES,
            exporter_context(owner_nonce, verified.tls_spki_der),
        )
        request = (
            f"GET {EXPORTER_PROOF_PATH} HTTP/1.1\r\n"
            "Host: spp-engine\r\n"
            "Content-Length: 0\r\n\r\n"
        ).encode("ascii")
        connection.sendall(request)
        proof_der = _recv_proof_response(connection)
        verify_exporter_proof(
            proof_der=proof_der,
            evidence=verified.evidence,
            tls_exporter=tls_exporter,
            owner_nonce=owner_nonce,
        )
        raw.settimeout(None)
        return AttestedChannel(
            raw_socket=raw,
            tls=connection,
            verified=verified,
            last_used_monotonic=monotonic_now(),
        )
    except RatlsVerificationError:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        elif raw is not None:
            raw.close()
        raise
    except RatlsChannelError:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        elif raw is not None:
            raw.close()
        raise
    except (OSError, SSL.Error) as exc:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        elif raw is not None:
            raw.close()
        raise RatlsChannelError("gateway_unreachable") from exc
