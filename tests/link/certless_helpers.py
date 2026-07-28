# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from solstone.convey.secure_listener.admission import (
    SecureListenerAdmission,
    SecureListenerAdmissionConfig,
)
from solstone.convey.secure_listener.identity import ConveyIdentity
from solstone.convey.secure_listener.wsgi import DispatchResult, dispatch_stream
from solstone.think.link.ca import ca_pin_matches
from solstone.think.link.client import (
    _CONNECT_TIMEOUT_SECONDS,
    _HTTP_TIMEOUT_SECONDS,
    StreamResetError,
    TunnelSession,
    _http_head_bytes,
    _open_pairing_session,
    _parse_http_response,
    _TcpEncryptedTransport,
)
from solstone.think.link.tls import TlsError


@dataclass(frozen=True)
class DispatchResponse:
    result: DispatchResult
    status: int
    headers: dict[str, str]
    body: bytes
    writer: FakeStreamWriter


@dataclass(frozen=True)
class DirectPairCandidate:
    ip: str
    port: int

    @property
    def host(self) -> str:
        return str(self.ip)


@dataclass(frozen=True)
class DirectPairRequest:
    candidates: tuple[DirectPairCandidate, ...]
    path: str
    ca_fingerprint_pin: str
    home: str | None = None


@dataclass(frozen=True)
class PairTarget:
    host: str
    port: int
    path: str


@dataclass(frozen=True)
class PairResponse:
    client_cert: str
    ca_chain: list[str]
    instance_id: str
    home_label: str
    home_attestation: str
    local_endpoints: list[Any]


class FakeStreamWriter:
    stream_id = 1

    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False
        self.reset_called = False
        self.reset_reason: int | None = None
        self.reset_context: str | None = None
        self.drain_context: str | None = None
        self.recv_consumed = 0

    async def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def close(self) -> None:
        self.closed = True

    async def reset(self, reason: int, context: str) -> None:
        self.reset_called = True
        self.reset_reason = reason
        self.reset_context = context
        self.closed = True

    def begin_drain(self, context: str) -> None:
        self.drain_context = context

    def report_recv_consumed(self, n: int) -> None:
        self.recv_consumed += n


def build_csr(label: str) -> tuple[ec.EllipticCurvePrivateKey, bytes, str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label[:64])]))
        .sign(private_key, hashes.SHA256())
    )
    private_key_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode("ascii")
    return private_key, private_key_pem, csr_pem


def framed_target(url: str) -> tuple[str, int, str]:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host:
        raise ValueError("Pair-link target missing host.")
    port = parsed.port
    if port is None:
        raise ValueError("Pair-link target missing explicit port.")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return host, port, path


def post_pair_framed(
    req: DirectPairRequest,
    body: dict[str, str],
    private_key: ec.EllipticCurvePrivateKey,
) -> PairResponse:
    try:
        return asyncio.run(_pair_exchange(req, body, private_key))
    except StreamResetError as exc:
        raise ValueError(
            "Pairing stream reset or closed before a response was received."
        ) from exc
    except TlsError as exc:
        raise ValueError(f"Pairing request TLS failed: {exc}") from exc
    except (ConnectionError, OSError) as exc:
        raise ValueError(f"Pairing request failed: {exc}") from exc


def parse_pair_response(payload: Any) -> PairResponse:
    if not isinstance(payload, dict):
        raise ValueError("Pair response was not a JSON object")
    client_cert = _required_str(payload, "client_cert")
    ca_chain = payload.get("ca_chain")
    if not isinstance(ca_chain, list) or not ca_chain:
        raise ValueError("Pair response missing ca_chain")
    if not all(isinstance(item, str) and item for item in ca_chain):
        raise ValueError("Pair response field ca_chain is invalid")
    instance_id = _required_str(payload, "instance_id")
    home_attestation = _required_str(payload, "home_attestation")
    home_label = payload.get("home_label")
    local_endpoints = payload.get("local_endpoints")
    return PairResponse(
        client_cert=client_cert,
        ca_chain=ca_chain,
        instance_id=instance_id,
        home_label=home_label if isinstance(home_label, str) else "",
        home_attestation=home_attestation,
        local_endpoints=local_endpoints if isinstance(local_endpoints, list) else [],
    )


async def _pair_exchange(
    req: DirectPairRequest,
    body: dict[str, str],
    private_key: ec.EllipticCurvePrivateKey,
) -> PairResponse:
    body_bytes = json.dumps(body).encode("utf-8")
    last_error: str | None = None
    for target in _dedupe_targets(_dial_targets(req)):
        try:
            session = await _open_ready_pairing_session(
                target,
                req.ca_fingerprint_pin,
            )
        except Exception as exc:  # noqa: BLE001 - normalized per-candidate error.
            last_error = _pre_request_error_message(target, exc)
            continue
        try:
            status, _headers, body_bytes_response = await _committed_pair_request(
                session,
                target,
                body_bytes,
            )
            response = _parse_pair_http_response(status, body_bytes_response)
            returned_ca = _verify_returned_ca(response, req.ca_fingerprint_pin)
            _validate_returned_client_cert(response, private_key, returned_ca)
            return response
        finally:
            await session.close()
    raise ValueError(last_error or "Could not connect to the pairing listener.")


def _dial_targets(req: DirectPairRequest) -> tuple[PairTarget, ...]:
    if req.home is not None:
        host, port, path = framed_target(f"{req.home}{req.path}")
        return (PairTarget(host=host, port=port, path=path),)
    return tuple(
        PairTarget(host=candidate.host, port=candidate.port, path=req.path)
        for candidate in req.candidates
    )


def _dedupe_targets(targets: tuple[PairTarget, ...]) -> tuple[PairTarget, ...]:
    seen: set[tuple[str, int]] = set()
    deduped: list[PairTarget] = []
    for target in targets:
        key = (target.host, target.port)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(target)
    return tuple(deduped)


async def _open_ready_pairing_session(
    target: PairTarget,
    ca_fingerprint_pin: str,
) -> TunnelSession:
    session = None
    transport = None
    writer = None
    ready = False

    async def open_session() -> TunnelSession:
        nonlocal session, transport, writer, ready
        try:
            reader, writer = await asyncio.open_connection(target.host, target.port)
            transport = _TcpEncryptedTransport(reader, writer)
            session = await _open_pairing_session(transport)
            _verify_direct_ready(session, ca_fingerprint_pin)
            ready = True
            return session
        finally:
            if not ready:
                await _close_unready_session(session, transport, writer)

    return await asyncio.wait_for(open_session(), timeout=_CONNECT_TIMEOUT_SECONDS)


async def _close_unready_session(
    session: TunnelSession | None,
    transport: Any,
    writer: Any,
) -> None:
    if session is not None:
        with contextlib.suppress(Exception):
            await session.close()
        return
    if transport is not None:
        with contextlib.suppress(Exception):
            await transport.close()
        return
    if writer is not None:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _verify_direct_ready(
    session: TunnelSession,
    ca_fingerprint_pin: str,
) -> None:
    chain = tuple(session.peer_certificate_chain())
    if not chain:
        raise ValueError(
            "Pairing TLS peer presented no certificate to verify against the pinned CA."
        )
    ca_cert = _find_pinned_ca_in_presented_chain(chain, ca_fingerprint_pin)
    if ca_cert is None:
        raise ValueError("Pairing TLS peer did not match the pair-link.")
    _verify_leaf_signed_by_pinned_ca(chain[0], ca_cert)


def _find_pinned_ca_in_presented_chain(
    chain: tuple[x509.Certificate, ...],
    ca_fingerprint_pin: str,
) -> x509.Certificate | None:
    for cert in chain:
        der = cert.public_bytes(serialization.Encoding.DER)
        if ca_pin_matches(
            f"sha256:{hashlib.sha256(der).hexdigest()}", ca_fingerprint_pin
        ):
            return cert
    return None


def _pre_request_error_message(target: PairTarget, exc: Exception) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return f"Timed out connecting to {target.host}:{target.port}."
    if isinstance(exc, TlsError):
        return f"TLS handshake with {target.host}:{target.port} failed: {exc}"
    if isinstance(exc, (ConnectionError, OSError)):
        return f"Could not connect to {target.host}:{target.port}: {exc}"
    return str(exc)


async def _committed_pair_request(
    session: TunnelSession,
    target: PairTarget,
    body_bytes: bytes,
) -> tuple[int, dict[str, str], bytes]:
    try:
        return await asyncio.wait_for(
            session.request(
                "POST",
                target.path,
                headers={"content-type": "application/json"},
                body=body_bytes,
            ),
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise ValueError(
            f"Timed out waiting for the pairing response from {target.host}:{target.port}."
        ) from exc


def _parse_pair_http_response(status: int, body_bytes: bytes) -> PairResponse:
    if status != 200:
        raise ValueError(
            f"Pairing failed (HTTP {status}): the pairing window is closed "
            "or the code was already used."
        )
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Pair response was not valid JSON") from exc
    return parse_pair_response(payload)


def _verify_returned_ca(
    response: PairResponse,
    ca_fingerprint_pin: str,
) -> x509.Certificate:
    chain_pem = _join_chain(response.ca_chain)
    cert = x509.load_pem_x509_certificate(_first_cert_pem(chain_pem).encode("ascii"))
    der = cert.public_bytes(serialization.Encoding.DER)
    fingerprint = f"sha256:{hashlib.sha256(der).hexdigest()}"
    if not ca_pin_matches(fingerprint, ca_fingerprint_pin):
        raise ValueError(
            "CA fingerprint mismatch: the pinned CA does not match the pair-link."
        )
    return cert


def _validate_returned_client_cert(
    response: PairResponse,
    private_key: ec.EllipticCurvePrivateKey,
    ca_cert: x509.Certificate,
) -> None:
    try:
        client_cert = x509.load_pem_x509_certificate(
            response.client_cert.encode("ascii")
        )
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("Pair response client certificate is invalid.") from exc
    try:
        _verify_leaf_signed_by_pinned_ca(client_cert, ca_cert)
    except ValueError as exc:
        raise ValueError(
            "Pair response client certificate is not signed by the pinned CA."
        ) from exc
    if _public_key_spki(client_cert.public_key()) != _public_key_spki(
        private_key.public_key()
    ):
        raise ValueError(
            "Pair response client certificate does not match the generated key."
        )


def _verify_leaf_signed_by_pinned_ca(
    leaf: x509.Certificate,
    ca_cert: x509.Certificate,
) -> None:
    public_key = ca_cert.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise ValueError(
            "Pinned CA uses an unexpected key type; refusing to trust the pairing peer."
        )
    try:
        public_key.verify(
            leaf.signature,
            leaf.tbs_certificate_bytes,
            ec.ECDSA(leaf.signature_hash_algorithm),
        )
    except InvalidSignature as exc:
        raise ValueError(
            "Pairing TLS peer certificate is not signed by the pinned CA "
            "(possible man-in-the-middle during pairing)."
        ) from exc


def _public_key_spki(public_key: Any) -> bytes:
    return public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _join_chain(ca_chain: list[str]) -> str:
    return "".join(cert if cert.endswith("\n") else f"{cert}\n" for cert in ca_chain)


def _first_cert_pem(chain_pem: str) -> str:
    marker = "-----BEGIN CERTIFICATE-----"
    start = chain_pem.find(marker)
    if start < 0:
        raise ValueError("CA chain contained no certificate")
    end_marker = "-----END CERTIFICATE-----"
    end = chain_pem.find(end_marker, start)
    if end < 0:
        raise ValueError("CA chain contained an unterminated certificate")
    return chain_pem[start : end + len(end_marker)] + "\n"


def _required_str(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Pair response missing {field}")
    return value


def make_convey_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    link: dict[str, Any] | None = None,
) -> tuple[Any, Path]:
    journal = tmp_path / "journal"
    journal.mkdir()
    write_config(journal, link=link)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    import solstone.convey as convey
    import solstone.convey.chat as convey_chat
    import solstone.think.link.runtime as link_runtime
    import solstone.think.push.runtime as push_runtime
    import solstone.think.voice.runtime as voice_runtime

    monkeypatch.setattr(convey_chat, "start_chat_runtime", lambda _app: None)
    monkeypatch.setattr(link_runtime, "start_link_runtime", lambda _app: None)
    monkeypatch.setattr(push_runtime, "start_push_runtime", lambda _app: None)
    monkeypatch.setattr(voice_runtime, "start_voice_runtime", lambda _app: None)

    app = convey.create_app(journal=str(journal))
    return app, journal


def write_config(
    journal: Path,
    *,
    link: dict[str, Any] | None = None,
) -> None:
    config: dict[str, Any] = {
        "setup": {"completed_at": 1700000000000},
    }
    if link is not None:
        config["link"] = link
    config_path = journal / "config" / "journal.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def certless_identity(
    mode: Literal["pl-via-spl", "pl-direct"] = "pl-via-spl",
) -> ConveyIdentity:
    return ConveyIdentity(
        mode=mode,
        fingerprint=None,
        device_label=None,
        paired_at=None,
        session_id="test-certless",
    )


def pl_identity(fingerprint: str) -> ConveyIdentity:
    return ConveyIdentity(
        mode="pl-via-spl",
        fingerprint=fingerprint,
        device_label="phone",
        paired_at="2026-05-29T00:00:00Z",
        session_id="test-pl",
    )


async def dispatch_request(
    app: Any,
    identity: ConveyIdentity,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> DispatchResponse:
    request_bytes = (
        _http_head_bytes(
            method,
            path,
            headers=headers,
            content_length=len(body),
        )
        + body
    )
    reader = asyncio.StreamReader()
    reader.feed_data(request_bytes)
    reader.feed_eof()
    writer = FakeStreamWriter()
    loop = asyncio.get_running_loop()
    admission = SecureListenerAdmission(
        SecureListenerAdmissionConfig(
            capacity=1,
            streaming_capacity=1,
            refuse_when_full=False,
        )
    )
    try:
        result = await dispatch_stream(app, identity, reader, writer, loop, admission)
    finally:
        await asyncio.to_thread(admission.shutdown, wait=True, cancel_futures=True)
    status, response_headers, response_body = _parse_http_response(bytes(writer.data))
    return DispatchResponse(
        result=result,
        status=status,
        headers=response_headers,
        body=response_body,
        writer=writer,
    )
