# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Browser public-key registration over an SPL pair-window tunnel."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import load_der_public_key

from solstone.think.convey_client import ConveyClient
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.ca import load_or_generate_ca, mint_attestation
from solstone.think.link.nonces import NonceStore
from solstone.think.link.paths import (
    LinkState,
    authorized_clients_path,
    ca_dir,
    nonces_path,
)
from solstone.think.link.upload_key import load_or_generate_upload_key
from solstone.think.spl.hpke import open_base, seal_base

log = logging.getLogger(__name__)

PAIR_HELLO = b"SBP1\x01"
PAIR_LABEL = b"spl-pair-browser-v1"
MAX_PAIR_MESSAGE = 1024 * 1024

RegisterPost = Callable[[dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


async def register_browser(
    reader: Any,
    ws: Any,
    *,
    register_post: RegisterPost | None = None,
) -> None:
    try:
        hello = await reader.read_exactly(len(PAIR_HELLO))
        if hello != PAIR_HELLO:
            raise ValueError("browser pair hello mismatch")

        state = LinkState.load_or_create()
        instance_id_16 = uuid.UUID(state.instance_id).bytes
        upload_key = load_or_generate_upload_key()
        ca = load_or_generate_ca(ca_dir())
        sig_der = ca.private_key.sign(
            PAIR_LABEL + upload_key.public_spki_der + instance_id_16,
            ec.ECDSA(hashes.SHA256()),
        )
        r, s = decode_dss_signature(sig_der)
        sig_raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        await _send_framed(
            ws,
            _json_bytes(
                {
                    "pkH_spki": _b64u(upload_key.public_spki_der),
                    "ca_spki": _b64u(ca.public_spki_der()),
                    "instance_id": state.instance_id,
                    "sig": _b64u(sig_raw),
                }
            ),
        )

        msg3 = await _read_framed(reader)
        if len(msg3) < 65:
            raise ValueError("browser pair msg3 too short")
        plaintext = open_base(
            msg3[:65],
            upload_key.private_key,
            instance_id_16,
            msg3[65:],
            b"",
        )
        inner = _load_json_object(plaintext)
        s_bytes = _require_b64u(inner, "S")
        ext_pub_spki = _require_b64u(inner, "ext_pub_spki")
        device_label = inner.get("device_label")
        if len(s_bytes) != 8:
            raise ValueError("browser pair nonce must be 8 bytes")
        if not isinstance(device_label, str):
            raise ValueError("browser pair device_label must be a string")
        _validate_ext_pubkey(ext_pub_spki)

        consumed = NonceStore(nonces_path()).consume(s_bytes.hex())
        if consumed is None:
            log.info("browser pair rejected: reason=nonce-unavailable")
            await _close_ws(ws)
            return

        ledger_label = consumed.device_label or device_label
        sender_fp = hashlib.sha256(ext_pub_spki).digest()
        fingerprint = "sha256:" + sender_fp.hex()
        observer_handle = await _register_observer(
            register_post or _default_register_post,
            ledger_label,
            sender_fp,
        )
        AuthorizedClients(authorized_clients_path()).add_browser(
            fingerprint=fingerprint,
            device_label=ledger_label,
            instance_id=state.instance_id,
            pubkey_spki=ext_pub_spki.hex(),
            observer_handle=observer_handle,
        )
        attestation = mint_attestation(ca, state.instance_id, fingerprint)
        reply = seal_base(
            ext_pub_spki,
            instance_id_16,
            _json_bytes(
                {
                    "instance_id": state.instance_id,
                    "home_attestation": attestation,
                }
            ),
            b"",
        )
        await _send_framed(ws, reply.enc + reply.ciphertext)
        log.info("browser pair completed: sender_fp=%s", fingerprint)
    except Exception as exc:
        log.warning("browser pair failed: type=%s", type(exc).__name__)
    finally:
        await _close_ws(ws)


async def _read_framed(reader: Any) -> bytes:
    raw_len = await reader.read_exactly(4)
    length = int.from_bytes(raw_len, "big")
    if length > MAX_PAIR_MESSAGE:
        raise ValueError("browser pair message too large")
    return await reader.read_exactly(length)


async def _send_framed(ws: Any, payload: bytes) -> None:
    if len(payload) > MAX_PAIR_MESSAGE:
        raise ValueError("browser pair message too large")
    await ws.send(len(payload).to_bytes(4, "big") + payload)


async def _register_observer(
    register_post: RegisterPost,
    device_label: str,
    sender_fp: bytes,
) -> str:
    payload = {
        "platform": "browser",
        "hostname": _browser_hostname(device_label, sender_fp),
        "stream_type": "browser",
        "version": "spl-browser-blob-v1",
        "label": device_label,
    }
    response = register_post(payload)
    if hasattr(response, "__await__"):
        response = await response
    key = response.get("key")
    if not isinstance(key, str) or not key:
        raise ValueError("observer register response missing key")
    return key


async def _default_register_post(payload: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(
        lambda: ConveyClient().request("POST", "/app/observer/register", json=payload)
    )


def _browser_hostname(device_label: str, sender_fp: bytes) -> str:
    base = device_label.strip().lower()
    base = re.sub(r"[\s/\\]+", "-", base)
    base = re.sub(r"[^a-z0-9._-]+", "-", base).strip(".-_")
    if not base:
        base = "browser"
    return f"{base}-{sender_fp.hex()[:12]}"


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _load_json_object(payload: bytes) -> dict[str, Any]:
    raw = json.loads(payload.decode("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("browser pair plaintext must be a JSON object")
    return raw


def _require_b64u(payload: dict[str, Any], field: str) -> bytes:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ValueError(f"browser pair {field} must be a string")
    return _b64u_decode(value)


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _validate_ext_pubkey(spki_der: bytes) -> None:
    public_key = load_der_public_key(spki_der)
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise ValueError("browser pair public key must be EC P-256")
    if not isinstance(public_key.curve, ec.SECP256R1):
        raise ValueError("browser pair public key must be EC P-256")


async def _close_ws(ws: Any) -> None:
    close = getattr(ws, "close", None)
    if close is not None:
        result = close()
        if hasattr(result, "__await__"):
            await result
