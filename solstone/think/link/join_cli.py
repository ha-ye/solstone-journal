# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Caller-side `sol link join` implementation.

The pair-link URL form decodes the embedded nonce and posts to
`/app/link/pair?token=<nonce>` over the framed mTLS listener.

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
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from solstone.apps.link.crockford32 import decode as crockford_decode
from solstone.think.link.auth import is_peer
from solstone.think.link.ca import ca_pin_matches
from solstone.think.link.client import (
    _CONNECT_TIMEOUT_SECONDS,
    StreamResetError,
    _open_pairing_session,
    _TcpEncryptedTransport,
)
from solstone.think.link.observer_paths import observer_bundle_dir
from solstone.think.link.paths import LinkState
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
class PairRequest:
    url: str
    body_base: dict[str, str]
    ca_fingerprint_pin: str | None = None


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

    if is_peer(as_role):
        private_key_pem, csr_pem = _build_csr(label)
        body = {
            **pair_request.body_base,
            "csr": csr_pem,
            "device_label": label,
        }
        body["sender_instance_id"] = LinkState.load_or_create().instance_id
        try:
            response = _post_pair(pair_request, body)
        except ValueError as exc:
            return _fail(str(exc), code=1)
        instance_id_error = _validate_instance_id(response.instance_id)
        if instance_id_error is not None:
            return _fail(instance_id_error, code=1)
        bundle_dir = _peer_dir(response.instance_id)
        existing_error = _existing_dir_error(bundle_dir)
        if existing_error is not None:
            return _fail(existing_error, code=1)
    else:
        bundle_dir = observer_bundle_dir(label)
        existing_error = _existing_dir_error(bundle_dir)
        if existing_error is not None:
            return _fail(existing_error, code=1)

        private_key_pem, csr_pem = _build_csr(label)
        body = {
            **pair_request.body_base,
            "csr": csr_pem,
            "device_label": label,
        }
        try:
            response = _post_pair(pair_request, body)
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
    created_dir = not bundle_dir.exists()
    try:
        _write_bundle(bundle_dir, files, created_dir=created_dir)
    except OSError as exc:
        return _fail(str(exc), code=1)

    suffix = " as peer" if is_peer(as_role) else ""
    print(f"Linked {label}{suffix}.")
    print(f"Credentials: {bundle_dir}")
    return 0


def _parse_pair_request(code: str, home: str | None) -> PairRequest:
    from solstone.apps.link.copy import PAIR_LINK_HOST, PAIR_LINK_PATH

    if code.startswith(f"https://{PAIR_LINK_HOST}{PAIR_LINK_PATH}#"):
        return _parse_pair_link(code, home)
    raise ValueError(
        f"Pair code did not match an accepted form. Use a pair-link like "
        f"https://{PAIR_LINK_HOST}{PAIR_LINK_PATH}#... from 'sol call link pair'."
    )


def _parse_pair_link(pair_link: str, home: str | None) -> PairRequest:
    from solstone.apps.link.copy import PAIR_LINK_HOST, PAIR_LINK_PATH

    parsed = urllib.parse.urlparse(pair_link)
    fragment = parsed.fragment
    try:
        blob = crockford_decode(fragment)
    except ValueError as exc:
        raise ValueError(
            f"Malformed pair-link. Use the full "
            f"https://{PAIR_LINK_HOST}{PAIR_LINK_PATH}#... value from the pairing "
            f"output."
        ) from exc
    if len(blob) != 40 or blob[0] != 0x04 or blob[1] != 0x01:
        raise ValueError(
            f"Malformed pair-link. Use the full "
            f"https://{PAIR_LINK_HOST}{PAIR_LINK_PATH}#... value from the pairing "
            f"output."
        )
    ipv4 = str(ipaddress.IPv4Address(blob[2:6]))
    port = int.from_bytes(blob[6:8], "big")
    nonce_hex = blob[8:24].hex()
    # blob[24:40] is the first 16 bytes of the home CA cert's DER SHA-256,
    # embedded to pin the home to the joining device. Carry it so the pairing
    # exchange can verify the home is who the pair-link claims (fail closed).
    ca_fingerprint_pin = blob[24:40].hex()
    base_url = home.rstrip("/") if home else f"https://{ipv4}:{port}"
    return PairRequest(
        url=f"{base_url}/app/link/pair?token={nonce_hex}",
        body_base={},
        ca_fingerprint_pin=ca_fingerprint_pin,
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


def _existing_dir_error(bundle_dir: Path) -> str | None:
    if not bundle_dir.exists():
        return None
    for entry in bundle_dir.iterdir():
        name = entry.name
        if (
            not name.startswith(".")
            or name in BUNDLE_FILES
            or name.lstrip(".") in BUNDLE_FILES
        ):
            return (
                f"Credentials directory already exists with content: {bundle_dir}. "
                f"Remove with 'rm -rf {bundle_dir}' and rerun if re-pairing."
            )
    return None


def _build_csr(label: str) -> tuple[bytes, str]:
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
    return private_key_pem, csr_pem


def _post_pair(pair_request: PairRequest, body: dict[str, str]) -> PairResponse:
    return _post_pair_framed(
        pair_request.url,
        body,
        ca_fingerprint_pin=pair_request.ca_fingerprint_pin,
    )


def _post_pair_framed(
    url: str,
    body: dict[str, str],
    *,
    ca_fingerprint_pin: str | None = None,
) -> PairResponse:
    host, port, path = _framed_target(url)
    try:
        return asyncio.run(_pair_exchange(host, port, path, body, ca_fingerprint_pin))
    except StreamResetError as exc:
        raise ValueError(
            "Pairing stream reset or closed before a response was received."
        ) from exc
    except TlsError as exc:
        raise ValueError(f"TLS handshake with {host}:{port} failed: {exc}") from exc
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise ValueError(f"Timed out connecting to {host}:{port}.") from exc
    except (ConnectionError, OSError) as exc:
        raise ValueError(f"Could not connect to {host}:{port}: {exc}") from exc


def _framed_target(url: str) -> tuple[str, int, str]:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host:
        raise ValueError(f"Pair-link target missing host: {url}")
    port = parsed.port
    if port is None:
        raise ValueError(f"Pair-link target missing explicit port: {url}")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return host, port, path


async def _pair_exchange(
    host: str,
    port: int,
    path: str,
    body: dict[str, str],
    ca_fingerprint_pin: str | None,
) -> PairResponse:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port),
        timeout=_CONNECT_TIMEOUT_SECONDS,
    )
    session = await _open_pairing_session(_TcpEncryptedTransport(reader, writer))
    try:
        status, _headers, body_bytes = await session.request(
            "POST",
            path,
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
        if ca_fingerprint_pin is not None:
            chain_pem = _join_chain(response.ca_chain)
            ca_fingerprint = _ca_fingerprint(chain_pem)
            # 1. The CA chain the home returns must match the fingerprint pinned
            #    in the pair-link (the embedded 16-byte prefix), or this is not
            #    the home the pair-link came from. Fail closed.
            if not ca_pin_matches(ca_fingerprint, ca_fingerprint_pin):
                raise ValueError(
                    f"CA fingerprint mismatch: got {ca_fingerprint}, "
                    f"expected prefix {ca_fingerprint_pin}"
                )
            # 2. Defense in depth: bind the *live* TLS peer to the pinned CA. A
            #    relay that echoes the real CA chain in the response body but
            #    terminates TLS with its own key cannot pass — it has no CA
            #    private key to sign a leaf the pinned CA would vouch for.
            peer_leaf = session.peer_certificate()
            if peer_leaf is None:
                raise ValueError(
                    "Pairing TLS peer presented no certificate to verify "
                    "against the pinned CA."
                )
            ca_cert = x509.load_pem_x509_certificate(
                _first_cert_pem(chain_pem).encode("ascii")
            )
            _verify_leaf_signed_by_pinned_ca(peer_leaf, ca_cert)
        return response
    finally:
        await session.close()


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


def _write_bundle(
    bundle_dir: Path,
    files: dict[str, bytes],
    *,
    created_dir: bool,
) -> None:
    written: list[Path] = []
    try:
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_dir.chmod(0o700)
        for name, content in files.items():
            path = bundle_dir / name
            _write_bytes(path, content)
            written.append(path)
    except OSError:
        for path in written:
            try:
                path.unlink()
            except OSError:
                pass
        if created_dir:
            try:
                bundle_dir.rmdir()
            except OSError:
                pass
        raise


def _write_bytes(path: Path, content: bytes) -> None:
    try:
        with open(path, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        path.chmod(0o600)
    except OSError as exc:
        raise OSError(f"failed to write {path}: {exc}") from exc


def _fail(message: str, *, code: int) -> int:
    print(message, file=sys.stderr)
    return code
