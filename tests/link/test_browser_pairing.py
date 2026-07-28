# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from solstone.think.link import browser_pairing
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.browser_pairing import PAIR_LABEL, register_browser
from solstone.think.link.ca import load_or_generate_ca
from solstone.think.link.nonces import NonceStore
from solstone.think.link.paths import (
    LinkState,
    authorized_clients_path,
    ca_dir,
    nonces_path,
)
from solstone.think.link.upload_key import load_or_generate_upload_key
from solstone.think.spl import hpke
from solstone.think.spl.ws_buffer import BufferedWsReader
from tests._baseline_harness import mark_setup_complete


class PairWs:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames = list(frames)
        self.sent: list[bytes] = []
        self.closed = False

    async def recv(self) -> bytes:
        if not self.frames:
            from websockets.exceptions import ConnectionClosed

            raise ConnectionClosed(None, None)
        return self.frames.pop(0)

    async def send(self, data: bytes, *, urgent: bool = False) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_browser_pairing_registers_observer_and_returns_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = tmp_path / "journal"
    journal.mkdir()
    mark_setup_complete(journal)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    from solstone.convey import create_app

    app = create_app(journal=str(journal))
    client = app.test_client()
    state = LinkState.load_or_create()
    home_key = load_or_generate_upload_key()
    ca = load_or_generate_ca(ca_dir())
    s = b"12345678"
    NonceStore(nonces_path()).add(s.hex(), "Browser Label")
    ext_private = ec.generate_private_key(ec.SECP256R1())
    ext_spki = ext_private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    msg3 = _pair_msg3(home_key.public_spki_der, state.instance_id, s, ext_spki)
    ws = PairWs([b"SBP1\x01" + len(msg3).to_bytes(4, "big") + msg3])
    register_payloads: list[dict[str, Any]] = []

    async def register_post(payload: dict[str, Any]) -> dict[str, Any]:
        register_payloads.append(payload)
        response = client.post("/app/observer/register", json=payload)
        assert response.status_code == 200
        return response.get_json()

    await register_browser(BufferedWsReader(ws), ws, register_post=register_post)

    assert ws.closed is True
    assert len(ws.sent) == 2
    msg2 = _read_framed(ws.sent[0])
    signed_identity = json.loads(msg2.decode("utf-8"))
    sig_raw = _b64u_decode(signed_identity["sig"])
    assert len(sig_raw) == 64
    r = int.from_bytes(sig_raw[:32], "big")
    sig_der = encode_dss_signature(r, int.from_bytes(sig_raw[32:], "big"))
    ca.cert.public_key().verify(
        sig_der,
        PAIR_LABEL + home_key.public_spki_der + uuid_bytes(state.instance_id),
        ec.ECDSA(hashes.SHA256()),
    )
    assert _b64u_decode(signed_identity["pkH_spki"]) == home_key.public_spki_der
    assert signed_identity["instance_id"] == state.instance_id
    # msg2 must carry the CA public SPKI so a browser holding only the 0x06 link's
    # 16-byte ca_fp_spki pin can verify the signature above. The transmitted key
    # must equal the CA's SPKI DER and hash to the link pin.
    ca_spki = _b64u_decode(signed_identity["ca_spki"])
    assert ca_spki == ca.public_spki_der()
    assert _sha256(ca_spki)[:16] == bytes.fromhex(ca.spki_fingerprint_sha256())[:16]
    # And the signature must verify using only the transmitted ca_spki (what the
    # extension actually does — it never has the CA cert, only this SPKI).
    serialization.load_der_public_key(ca_spki).verify(
        sig_der,
        PAIR_LABEL + home_key.public_spki_der + uuid_bytes(state.instance_id),
        ec.ECDSA(hashes.SHA256()),
    )

    entry = AuthorizedClients(authorized_clients_path()).get(
        "sha256:" + _sha256(ext_spki).hex()
    )
    assert entry is not None
    assert entry.kind == "browser"
    assert entry.device_label == "Browser Label"
    assert entry.pubkey_spki == ext_spki.hex()
    assert entry.observer_handle
    assert register_payloads[0]["label"] == "Browser Label"
    assert register_payloads[0]["hostname"].startswith("browser-label-")
    assert NonceStore(nonces_path()).consume(s.hex()) is None

    msg4 = _read_framed(ws.sent[1])
    opened = hpke.open_base(
        msg4[:65],
        ext_private,
        uuid_bytes(state.instance_id),
        msg4[65:],
        b"",
    )
    reply = json.loads(opened.decode("utf-8"))
    assert reply["instance_id"] == state.instance_id
    attestation_payload = _jwt_payload(reply["home_attestation"])
    assert attestation_payload["device_fp"] == "sha256:" + _sha256(ext_spki).hex()

    replay_ws = PairWs([b"SBP1\x01" + len(msg3).to_bytes(4, "big") + msg3])
    await register_browser(
        BufferedWsReader(replay_ws),
        replay_ws,
        register_post=register_post,
    )

    assert len(replay_ws.sent) == 1
    assert replay_ws.closed is True


@pytest.mark.asyncio
async def test_default_register_post_uses_loopback_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    payload = {
        "platform": "browser",
        "hostname": "browser-label-abc123",
        "stream_type": "browser",
        "version": "spl-browser-blob-v1",
        "label": "Browser Label",
    }

    class FakeClient:
        def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            calls.append((method, path, kwargs))
            return {"key": "browser-label.browser"}

    monkeypatch.setattr(browser_pairing, "ConveyClient", lambda: FakeClient())

    response = await browser_pairing._default_register_post(payload)

    assert response == {"key": "browser-label.browser"}
    assert calls == [
        (
            "POST",
            "/app/observer/register",
            {"json": payload},
        )
    ]


def _pair_msg3(
    home_spki: bytes,
    instance_id: str,
    s: bytes,
    ext_spki: bytes,
) -> bytes:
    plaintext = json.dumps(
        {
            "S": _b64u(s),
            "ext_pub_spki": _b64u(ext_spki),
            "device_label": "Browser Self Label",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    sealed = hpke.seal_base(home_spki, uuid_bytes(instance_id), plaintext, b"")
    return sealed.enc + sealed.ciphertext


def _read_framed(frame: bytes) -> bytes:
    length = int.from_bytes(frame[:4], "big")
    payload = frame[4:]
    assert len(payload) == length
    return payload


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sha256(data: bytes) -> bytes:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(data)
    return digest.finalize()


def uuid_bytes(instance_id: str) -> bytes:
    import uuid

    return uuid.UUID(instance_id).bytes


def _jwt_payload(token: str) -> dict[str, Any]:
    _header, payload, _sig = token.split(".")
    return json.loads(_b64u_decode(payload).decode("utf-8"))
