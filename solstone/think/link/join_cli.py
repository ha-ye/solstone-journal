# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Caller-side `sol link join` implementation.

The direct pair-link forms (0x04 and 0x05) decode to an ordered set of local
candidate endpoints that share one nonce and one CA-cert-DER pin. The whole
embedded set must satisfy `direct_admission` before key generation or dialing;
`--home`, when supplied, is only the operator-chosen dial override after that
structural and policy check and may legitimately name a host instead of an
IPv4 address. The selected target receives one framed mTLS POST to
`/app/network/pair?token=<nonce>`. The request invocation is the commit point:
once it starts, no later candidate is tried.

The relay pair-window form (0x06) dials the relay, then runs the same inner
pairing request through the pinned TLS tunnel.

Role-less linked-system credentials are written under
`$XDG_CONFIG_HOME/solstone-observer/spl/<label>/` when XDG_CONFIG_HOME is set,
otherwise `~/.config/solstone-observer/spl/<label>/`.

Peer credentials are written under `<journal_root>/peers/<instance_id>/`,
where `instance_id` is the receiver instance_id returned by the pair response,
not the local `--label`. Label-to-instance_id resolution for
`journal transfer send --to <label>` is a follow-on lode that will walk
`peer.json` files.

Both layouts contain `private.pem`, `cert.pem`, `chain.pem`,
`home_attestation.jwt`, and `peer.json`. `peer.json` fields are deterministic:
`label`, `paired_at`, `instance_id`, `home_label`, `fingerprint`,
`local_endpoints`, and `role`; role is `peer` or `""` for role-less linked
systems. `peer` is provenance, not a behavioral authorization role: pairing a
peer provisions a journal-content source, records the sender `instance_id`, and
leaves durable in-data provenance through per-segment `sender_instance_id` /
`sender_fingerprint` and identity-derived source directories.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from websockets.exceptions import InvalidStatus

from solstone.apps.network.crockford32 import decode as crockford_decode
from solstone.apps.network.relay_link import decode_pair_window_link, derive_rk
from solstone.think.link.auth import is_peer
from solstone.think.link.ca import ca_pin_matches
from solstone.think.link.client import (
    _CONNECT_TIMEOUT_SECONDS,
    _HTTP_TIMEOUT_SECONDS,
    Client,
    ClientIdentity,
    StreamResetError,
    TunnelSession,
    _open_pairing_session,
    _TcpEncryptedTransport,
    _to_ws,
    _WsEncryptedTransport,
)
from solstone.think.link.direct_admission import is_direct_pair_candidate_allowed
from solstone.think.link.mark import jid_from_spki
from solstone.think.link.observer_paths import observer_bundle_dir
from solstone.think.link.paths import DEFAULT_RELAY_URL, LinkState
from solstone.think.link.tls import TlsError
from solstone.think.utils import get_journal

VALID_ROLES = {"", "phone", "observer", "peer"}
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
DEFAULT_CLIENT_LABEL = "linked-system"
BUNDLE_FILES = {
    "private.pem",
    "cert.pem",
    "chain.pem",
    "home_attestation.jwt",
    "peer.json",
}
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,256}$")


@dataclass(frozen=True)
class DirectPairCandidate:
    ip: ipaddress.IPv4Address
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
class RelayPairRequest:
    relay_endpoint: str
    rk: bytes
    s: bytes
    ca_fp_spki: bytes
    inner_path: str


@dataclass(frozen=True)
class PairResponse:
    client_cert: str
    ca_chain: list[str]
    instance_id: str
    home_label: str
    home_attestation: str
    local_endpoints: list[Any]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", help="Receiver base URL")
    parser.add_argument("--code", required=True, help="pair-link URL")
    parser.add_argument("--as", dest="as_role", help="Optional tag to join as")
    parser.add_argument(
        "--label",
        required=False,
        default=None,
        help="Local credentials label (defaults to this machine's hostname)",
    )


def main(args: argparse.Namespace) -> int:
    as_role = args.as_role or ""
    if as_role not in VALID_ROLES:
        return _fail("invalid role; expected one of: phone, observer, peer", code=2)

    if args.label is not None:
        label = str(args.label)
        label_error = _label_error(label)
        if label_error is not None:
            return _fail(label_error, code=2)
    else:
        label = _hostname_client_label()

    try:
        pair_request = _parse_pair_request(str(args.code).strip(), args.home)
    except ValueError as exc:
        return _fail(str(exc), code=1)

    if isinstance(pair_request, RelayPairRequest):
        return _join_via_relay(pair_request, label, as_role)

    if is_peer(as_role):
        private_key, private_key_pem, csr_pem = _build_csr(label)
        body = {
            "csr": csr_pem,
            "device_label": label,
        }
        body["sender_instance_id"] = LinkState.load_or_create().instance_id
        try:
            response = _post_pair(pair_request, body, private_key)
        except ValueError as exc:
            return _fail(str(exc), code=1)
        instance_id_error = _validate_instance_id(response.instance_id)
        if instance_id_error is not None:
            return _fail(instance_id_error, code=1)
        bundle_dir = _peer_dir(response.instance_id)
        existing_error = _existing_path_error(bundle_dir)
        if existing_error is not None:
            return _fail(existing_error, code=1)
    else:
        bundle_dir = observer_bundle_dir(label)
        existing_error = _existing_path_error(bundle_dir)
        if existing_error is not None:
            return _fail(existing_error, code=1)

        private_key, private_key_pem, csr_pem = _build_csr(label)
        body = {
            "csr": csr_pem,
            "device_label": label,
        }
        try:
            response = _post_pair(pair_request, body, private_key)
        except ValueError as exc:
            return _fail(str(exc), code=1)

    chain_pem = _join_chain(response.ca_chain)
    try:
        ca_fp = _ca_fingerprint(chain_pem)
    except ValueError as exc:
        return _fail(str(exc), code=1)

    peer = {
        "label": label,
        "paired_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instance_id": response.instance_id,
        "home_label": response.home_label,
        "fingerprint": ca_fp,
        "local_endpoints": response.local_endpoints,
        "role": "peer" if is_peer(as_role) else "",
    }
    files = {
        "private.pem": private_key_pem,
        "cert.pem": response.client_cert.encode("utf-8"),
        "chain.pem": chain_pem.encode("utf-8"),
        "home_attestation.jwt": response.home_attestation.encode("utf-8"),
        "peer.json": (json.dumps(peer, indent=2) + "\n").encode("utf-8"),
    }
    try:
        _publish_bundle_atomic(bundle_dir, files)
    except OSError as exc:
        return _fail(str(exc), code=1)

    suffix = " as peer" if is_peer(as_role) else ""
    print(f"Linked {label}{suffix}.")
    print(f"Credentials: {bundle_dir}")
    return 0


def _parse_pair_request(
    code: str,
    home: str | None,
) -> DirectPairRequest | RelayPairRequest:
    from solstone.apps.network.copy import PAIR_LINK_HOST, PAIR_LINK_PATH

    if code.startswith(f"https://{PAIR_LINK_HOST}{PAIR_LINK_PATH}#"):
        return _parse_pair_link(code, home)
    raise ValueError(
        f"Pair code did not match an accepted form. Use a pair-link like "
        f"https://{PAIR_LINK_HOST}{PAIR_LINK_PATH}#... from 'sol call link pair'."
    )


def _parse_pair_link(
    pair_link: str, home: str | None
) -> DirectPairRequest | RelayPairRequest:

    parsed = urllib.parse.urlparse(pair_link)
    fragment = parsed.fragment
    try:
        blob = crockford_decode(fragment)
    except ValueError as exc:
        raise ValueError(_malformed_pair_link_message()) from exc
    if not blob:
        raise ValueError(_malformed_pair_link_message())
    if blob[0] == 0x06:
        return _parse_relay_pair_link(pair_link)
    candidates, nonce_hex, ca_fingerprint_pin = _decode_direct_pair_blob(blob)
    _check_direct_candidate_policy(candidates)
    return DirectPairRequest(
        candidates=candidates,
        path=_direct_pair_path(nonce_hex),
        ca_fingerprint_pin=ca_fingerprint_pin,
        home=home.rstrip("/") if home else None,
    )


def _malformed_pair_link_message() -> str:
    from solstone.apps.network.copy import PAIR_LINK_HOST, PAIR_LINK_PATH

    return (
        f"Malformed pair-link. Use the full "
        f"https://{PAIR_LINK_HOST}{PAIR_LINK_PATH}#... value from the pairing "
        f"output."
    )


def _decode_direct_pair_blob(
    blob: bytes,
) -> tuple[tuple[DirectPairCandidate, ...], str, str]:
    if len(blob) < 2 or blob[0] not in {0x04, 0x05} or blob[1] != 0x01:
        raise ValueError(_malformed_pair_link_message())

    version = blob[0]
    if version == 0x04:
        if len(blob) != 40:
            raise ValueError(_malformed_pair_link_message())
        port = int.from_bytes(blob[6:8], "big")
        nonce_hex = blob[8:24].hex()
        ca_fingerprint_pin = blob[24:40].hex()
        return (
            (DirectPairCandidate(ipaddress.IPv4Address(blob[2:6]), port),),
            nonce_hex,
            ca_fingerprint_pin,
        )

    if len(blob) < 5:
        raise ValueError(_malformed_pair_link_message())
    count = blob[2]
    if count < 1 or count > 4:
        raise ValueError(_malformed_pair_link_message())
    expected_len = 37 + 4 * count
    if len(blob) != expected_len:
        raise ValueError(_malformed_pair_link_message())
    port = int.from_bytes(blob[3:5], "big")
    address_start = 5
    address_end = address_start + 4 * count
    candidates = tuple(
        DirectPairCandidate(ipaddress.IPv4Address(blob[offset : offset + 4]), port)
        for offset in range(address_start, address_end, 4)
    )
    nonce_hex = blob[address_end : address_end + 16].hex()
    ca_fingerprint_pin = blob[address_end + 16 : address_end + 32].hex()
    return candidates, nonce_hex, ca_fingerprint_pin


def _check_direct_candidate_policy(
    candidates: tuple[DirectPairCandidate, ...],
) -> None:
    if all(is_direct_pair_candidate_allowed(candidate.ip) for candidate in candidates):
        return
    raise ValueError(
        "Pair-link points at an address outside the local network this joiner will dial."
    )


def _direct_pair_path(nonce_hex: str) -> str:
    return f"/app/network/pair?token={nonce_hex}"


def _parse_relay_pair_link(pair_link: str) -> RelayPairRequest:
    parsed = decode_pair_window_link(pair_link)
    relay_endpoint = parsed.relay_origin or DEFAULT_RELAY_URL
    return RelayPairRequest(
        relay_endpoint=relay_endpoint,
        rk=derive_rk(parsed.s),
        s=parsed.s,
        ca_fp_spki=parsed.ca_fp_spki,
        inner_path=f"/app/network/pair?token={parsed.s.hex()}",
    )


def _label_error(label: str) -> str | None:
    if not label:
        return "--label must not be empty"
    if len(label) > 80:
        return "--label must be 80 characters or fewer"
    if "/" in label or "\\" in label:
        return "--label must not contain path separators"
    if ".." in label:
        return "--label must not contain '..'"
    if label.startswith("."):
        return "--label must not start with '.'"
    if not LABEL_RE.fullmatch(label):
        return "--label may contain only letters, numbers, '-', '_', and '.'"
    return None


def _sanitize_client_label(raw: str) -> str:
    if not re.search(r"[A-Za-z0-9_.-]", raw):
        return ""
    label = re.sub(r"[^A-Za-z0-9_.-]", "-", raw)
    label = re.sub(r"\.{2,}", "-", label)
    label = label.lstrip(".")[:80]
    if not label or _label_error(label) is not None:
        return ""
    return label


def _hostname_client_label() -> str:
    try:
        raw = socket.gethostname()
    except OSError:
        raw = ""
    return _sanitize_client_label(raw) or DEFAULT_CLIENT_LABEL


def _validate_instance_id(value: str) -> str | None:
    if not _INSTANCE_ID_RE.fullmatch(value):
        return f"bad instance_id from receiver: {value!r}"
    return None


def _peer_dir(instance_id: str) -> Path:
    return Path(get_journal()) / "peers" / instance_id


def _existing_path_error(bundle_dir: Path) -> str | None:
    if not os.path.lexists(bundle_dir):
        return None
    return (
        f"Credentials path already exists: {bundle_dir}. "
        "Remove it and rerun if re-pairing."
    )


def _build_csr(label: str) -> tuple[ec.EllipticCurvePrivateKey, bytes, str]:
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


def _post_pair(
    pair_request: DirectPairRequest,
    body: dict[str, str],
    private_key: ec.EllipticCurvePrivateKey,
) -> PairResponse:
    return _post_pair_framed(pair_request, body, private_key)


def _join_via_relay(req: RelayPairRequest, label: str, as_role: str) -> int:
    # Keep the relay orchestration isolated instead of refactoring the working
    # direct path: both tails intentionally mirror the same bundle contract.
    _private_key, private_key_pem, csr_pem = _build_csr(label)
    body: dict[str, str] = {"csr": csr_pem, "device_label": label}
    if is_peer(as_role):
        body["sender_instance_id"] = LinkState.load_or_create().instance_id

    try:
        response = _post_pair_relay(req, body)
    except ValueError as exc:
        return _fail(str(exc), code=1)

    if is_peer(as_role):
        instance_id_error = _validate_instance_id(response.instance_id)
        if instance_id_error is not None:
            return _fail(instance_id_error, code=1)
        bundle_dir = _peer_dir(response.instance_id)
    else:
        bundle_dir = observer_bundle_dir(label)
    existing_error = _existing_path_error(bundle_dir)
    if existing_error is not None:
        return _fail(existing_error, code=1)

    chain_pem = _join_chain(response.ca_chain)
    try:
        ca_fp = _ca_fingerprint(chain_pem)
    except ValueError as exc:
        return _fail(str(exc), code=1)

    identity = ClientIdentity(
        private_key_pem=private_key_pem.decode("ascii"),
        client_cert_pem=response.client_cert,
        ca_chain_pem=chain_pem,
        fingerprint=ca_fp,
        home_instance_id=response.instance_id,
        home_label=response.home_label,
        home_attestation=response.home_attestation,
        local_endpoints=tuple(response.local_endpoints),
    )
    try:
        Client.enroll_device(req.relay_endpoint, identity)
    except Exception as exc:  # noqa: BLE001 - fail loudly on any enroll rejection.
        return _fail(f"Relay rejected device enrollment: {exc}", code=1)

    peer = {
        "label": label,
        "paired_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "instance_id": response.instance_id,
        "home_label": response.home_label,
        "fingerprint": ca_fp,
        "local_endpoints": response.local_endpoints,
        "role": "peer" if is_peer(as_role) else "",
    }
    files = {
        "private.pem": private_key_pem,
        "cert.pem": response.client_cert.encode("utf-8"),
        "chain.pem": chain_pem.encode("utf-8"),
        "home_attestation.jwt": response.home_attestation.encode("utf-8"),
        "peer.json": (json.dumps(peer, indent=2) + "\n").encode("utf-8"),
    }
    try:
        _publish_bundle_atomic(bundle_dir, files)
    except OSError as exc:
        return _fail(str(exc), code=1)

    suffix = " as peer" if is_peer(as_role) else ""
    print(f"Linked {label}{suffix}.")
    print(f"Credentials: {bundle_dir}")
    return 0


def _post_pair_relay(req: RelayPairRequest, body: dict[str, str]) -> PairResponse:
    try:
        return asyncio.run(_pair_exchange_relay(req, body))
    except StreamResetError as exc:
        raise ValueError(
            "Pairing stream reset or closed before a response was received."
        ) from exc
    except TlsError as exc:
        raise ValueError(f"Inner TLS handshake failed: {exc}") from exc
    except InvalidStatus as exc:
        raise ValueError(
            "The pairing window is closed or was already used (relay declined the dial)."
        ) from exc
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise ValueError("Timed out dialing the relay pairing window.") from exc
    except (ConnectionError, OSError) as exc:
        raise ValueError(f"Could not reach the relay: {exc}") from exc


async def _pair_exchange_relay(
    req: RelayPairRequest,
    body: dict[str, str],
) -> PairResponse:
    ws_url = _to_ws(req.relay_endpoint.rstrip("/")) + "/session/pair-dial"
    async with websockets.connect(
        ws_url,
        additional_headers={"Sec-Pair-Key": req.rk.hex()},
        max_size=None,
    ) as ws:
        session = await _open_pairing_session(_WsEncryptedTransport(ws))
        try:
            status, _headers, body_bytes = await session.request(
                "POST",
                req.inner_path,
                headers={"content-type": "application/json"},
                body=json.dumps(body).encode("utf-8"),
            )
            if status != 200:
                raise ValueError(
                    f"Pairing failed (HTTP {status}): the pairing window is closed "
                    "or the code was already used."
                )
            try:
                payload = json.loads(body_bytes.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("Pair response was not valid JSON") from exc
            response = _parse_pair_response(payload)
            _verify_relay_pair(response, session.peer_certificate(), req.ca_fp_spki)
            return response
        finally:
            await session.close()


def _verify_relay_pair(
    response: PairResponse,
    peer_leaf: x509.Certificate | None,
    ca_fp_spki: bytes,
) -> None:
    """Fail closed unless the pinned CA, live TLS leaf, and jid all agree."""
    chain_pem = _join_chain(response.ca_chain)
    ca_cert, spki_der = _load_ca_cert_and_spki(chain_pem)
    if hashlib.sha256(spki_der).digest()[:16] != ca_fp_spki:
        raise ValueError(
            "CA fingerprint mismatch: the pinned CA does not match the pair-link."
        )
    if peer_leaf is None:
        raise ValueError(
            "Pairing TLS peer presented no certificate to verify against the pinned CA."
        )
    _verify_leaf_signed_by_pinned_ca(peer_leaf, ca_cert)
    expected = str(jid_from_spki(spki_der))
    if response.instance_id != expected:
        raise ValueError(
            "Home instance_id does not match the pinned CA identity "
            f"(got {response.instance_id!r}, expected {expected!r})."
        )


def _load_ca_cert_and_spki(chain_pem: str) -> tuple[x509.Certificate, bytes]:
    cert_pem = _first_cert_pem(chain_pem)
    ca_cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
    spki_der = ca_cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return ca_cert, spki_der


def _post_pair_framed(
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


def _framed_target(url: str) -> tuple[str, int, str]:
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
        host, port, path = _framed_target(f"{req.home}{req.path}")
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
    return _parse_pair_response(payload)


def _verify_returned_ca(
    response: PairResponse,
    ca_fingerprint_pin: str,
) -> x509.Certificate:
    chain_pem = _join_chain(response.ca_chain)
    cert = x509.load_pem_x509_certificate(_first_cert_pem(chain_pem).encode("ascii"))
    der = cert.public_bytes(serialization.Encoding.DER)
    fingerprint = f"sha256:{hashlib.sha256(der).hexdigest()}"
    # Pin response chain position 0 because that is the CA the bundle persists
    # first in chain.pem and fingerprints in peer.json for later trust.
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


def _public_key_spki(public_key: Any) -> bytes:
    return public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _parse_pair_response(payload: Any) -> PairResponse:
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


def _required_str(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Pair response missing {field}")
    return value


def _join_chain(ca_chain: list[str]) -> str:
    return "".join(cert if cert.endswith("\n") else f"{cert}\n" for cert in ca_chain)


def _ca_fingerprint(chain_pem: str) -> str:
    cert_pem = _first_cert_pem(chain_pem)
    cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
    der = cert.public_bytes(serialization.Encoding.DER)
    return f"sha256:{hashlib.sha256(der).hexdigest()}"


def _verify_leaf_signed_by_pinned_ca(
    leaf: x509.Certificate,
    ca_cert: x509.Certificate,
) -> None:
    """Raise unless ``leaf`` carries a valid signature from ``ca_cert``.

    The link stack issues EC P-256 CAs and leaves, so verification is ECDSA.
    Any other key type, or an invalid signature, fails closed.
    """
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


def _first_cert_pem(chain_pem: str) -> str:
    marker = "-----BEGIN CERTIFICATE-----"
    start = chain_pem.find(marker)
    if start < 0:
        raise ValueError("CA chain contained no certificate")
    end_marker = "-----END CERTIFICATE-----"
    end = chain_pem.find(end_marker, start)
    if end < 0:
        raise ValueError("CA chain contained an incomplete certificate")
    end += len(end_marker)
    return chain_pem[start:end] + "\n"


def _publish_bundle_atomic(
    bundle_dir: Path,
    files: dict[str, bytes],
) -> None:
    parent = bundle_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    existing_error = _existing_path_error(bundle_dir)
    if existing_error is not None:
        raise OSError(existing_error)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{bundle_dir.name}.",
            dir=parent,
        )
    )
    try:
        staging.chmod(0o700)
        for name, content in files.items():
            _write_bundle_file(staging / name, content)
        _fsync_directory(staging)
        # A concurrent creator between lexists() and rename() can still win; this
        # join path has no cross-process lock, so the refuse-first check is the
        # intended guard for ordinary reruns.
        os.rename(staging, bundle_dir)
        _fsync_directory(parent)
    except BaseException:
        if os.path.lexists(staging):
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _write_bundle_file(path: Path, content: bytes) -> None:
    try:
        with open(path, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        path.chmod(0o600)
    except OSError as exc:
        raise OSError(f"failed to write {path}: {exc}") from exc


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def _fail(message: str, *, code: int) -> int:
    print(message, file=sys.stderr)
    return code
