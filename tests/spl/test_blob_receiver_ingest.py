# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import gzip
import hmac
import io
import json
import tarfile
import uuid
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from solstone.observe.protocol import OBSERVER_HANDLE_HEADER
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.paths import LinkState, authorized_clients_path
from solstone.think.link.upload_key import load_or_generate_upload_key
from solstone.think.spl import blob_receiver, hpke
from solstone.think.spl.ws_buffer import BufferedWsReader
from tests._baseline_harness import mark_setup_complete


class BlobWs:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames = list(frames)
        self.sent: list[bytes] = []
        self.closed = False

    async def recv(self) -> bytes:
        if not self.frames:
            from websockets.exceptions import ConnectionClosed

            raise ConnectionClosed(None, None)
        return self.frames.pop(0)

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_blob_receiver_ingests_and_acknowledges_duplicate_via_real_observer(
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
    ext_private = ec.generate_private_key(ec.SECP256R1())
    ext_spki = ext_private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    sender_fp = hashes.Hash(hashes.SHA256())
    sender_fp.update(ext_spki)
    sender_fp_bytes = sender_fp.finalize()

    register = client.post(
        "/app/observer/register",
        json={
            "platform": "browser",
            "hostname": "browserhost",
            "stream_type": "browser",
            "version": "spl-browser-blob-v1",
        },
    )
    assert register.status_code == 200
    observer_handle = register.get_json()["key"]
    handles_seen: list[str] = []
    AuthorizedClients(authorized_clients_path()).add_browser(
        fingerprint="sha256:" + sender_fp_bytes.hex(),
        device_label="browserhost",
        instance_id=state.instance_id,
        pubkey_spki=ext_spki.hex(),
        observer_handle=observer_handle,
    )

    async def ingest_post(
        day: str,
        segment: str,
        host: str,
        meta: dict[str, Any],
        files: list[tuple[str, bytes, str]],
        observer_handle_arg: str,
    ) -> dict[str, Any]:
        handles_seen.append(observer_handle_arg)
        response = client.post(
            "/app/observer/ingest",
            headers={OBSERVER_HANDLE_HEADER: observer_handle_arg},
            data={
                "day": day,
                "segment": segment,
                "host": host,
                "platform": "browser",
                "meta": json.dumps(meta, separators=(",", ":")),
                "files": [
                    (io.BytesIO(content), filename)
                    for filename, content, _content_type in files
                ],
            },
        )
        assert response.status_code == 200
        return response.get_json()

    first_ws, first_k_ack, first_blob_id = _sealed_blob_ws(
        state.instance_id,
        home_key.public_spki_der,
        ext_private,
        sender_fp_bytes,
        b'{"event":"first"}\n',
    )

    await blob_receiver.receive_blob(
        BufferedWsReader(first_ws), first_ws, ingest_post=ingest_post
    )

    assert first_ws.sent[0] == b"SBR1\x01\x00"
    assert first_ws.sent[1][0:6] == b"SBA1\x01\x00"
    assert first_ws.sent[1][6:22] == first_blob_id
    assert first_ws.sent[1][22:38] == _ack_tag(first_k_ack, 0x00, first_blob_id)
    stored = (
        journal
        / "chronicle"
        / "20260704"
        / "browserhost.browser"
        / "120000_300"
        / "browser_browserhost.jsonl"
    )
    assert stored.read_bytes() == b'{"event":"first"}\n'

    second_ws, second_k_ack, second_blob_id = _sealed_blob_ws(
        state.instance_id,
        home_key.public_spki_der,
        ext_private,
        sender_fp_bytes,
        b'{"event":"first"}\n',
    )

    await blob_receiver.receive_blob(
        BufferedWsReader(second_ws), second_ws, ingest_post=ingest_post
    )

    assert second_ws.sent[1][0:6] == b"SBA1\x01\x01"
    assert second_ws.sent[1][22:38] == _ack_tag(second_k_ack, 0x01, second_blob_id)
    assert (
        len(
            list((journal / "chronicle" / "20260704" / "browserhost.browser").iterdir())
        )
        == 1
    )

    distinct_ws, distinct_k_ack, distinct_blob_id = _sealed_blob_ws(
        state.instance_id,
        home_key.public_spki_der,
        ext_private,
        sender_fp_bytes,
        b'{"event":"second"}\n',
        segment="120500_300",
    )

    await blob_receiver.receive_blob(
        BufferedWsReader(distinct_ws), distinct_ws, ingest_post=ingest_post
    )

    assert distinct_ws.sent[1][0:6] == b"SBA1\x01\x00"
    assert distinct_ws.sent[1][22:38] == _ack_tag(
        distinct_k_ack, 0x00, distinct_blob_id
    )
    assert (
        journal
        / "chronicle"
        / "20260704"
        / "browserhost.browser"
        / "120500_300"
        / "browser_browserhost.jsonl"
    ).read_bytes() == b'{"event":"second"}\n'
    assert handles_seen == [observer_handle, observer_handle, observer_handle]

    AuthorizedClients(authorized_clients_path()).remove(
        "sha256:" + sender_fp_bytes.hex()
    )
    rejected_ws, _k_ack, _blob_id = _sealed_blob_ws(
        state.instance_id,
        home_key.public_spki_der,
        ext_private,
        sender_fp_bytes,
        b'{"event":"third"}\n',
    )

    await blob_receiver.receive_blob(
        BufferedWsReader(rejected_ws), rejected_ws, ingest_post=ingest_post
    )

    assert rejected_ws.sent == [b"SBR1\x01\x01"]

    late_private = ec.generate_private_key(ec.SECP256R1())
    late_spki = late_private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    late_sender_fp = hashes.Hash(hashes.SHA256())
    late_sender_fp.update(late_spki)
    late_sender_fp_bytes = late_sender_fp.finalize()
    late_fingerprint = "sha256:" + late_sender_fp_bytes.hex()
    live_store = blob_receiver._authorized_store()
    assert live_store.get(late_fingerprint) is None
    late_register = client.post(
        "/app/observer/register",
        json={
            "platform": "browser",
            "hostname": "latebrowser",
            "stream_type": "browser",
            "version": "spl-browser-blob-v1",
        },
    )
    assert late_register.status_code == 200
    late_handle = late_register.get_json()["key"]
    AuthorizedClients(authorized_clients_path()).add_browser(
        fingerprint=late_fingerprint,
        device_label="latebrowser",
        instance_id=state.instance_id,
        pubkey_spki=late_spki.hex(),
        observer_handle=late_handle,
    )
    late_ws, late_k_ack, late_blob_id = _sealed_blob_ws(
        state.instance_id,
        home_key.public_spki_der,
        late_private,
        late_sender_fp_bytes,
        b'{"event":"late"}\n',
        host="latebrowser",
        segment="121000_300",
    )

    await blob_receiver.receive_blob(
        BufferedWsReader(late_ws), late_ws, ingest_post=ingest_post
    )

    assert late_ws.sent[0] == b"SBR1\x01\x00"
    assert late_ws.sent[1][0:6] == b"SBA1\x01\x00"
    assert late_ws.sent[1][22:38] == _ack_tag(late_k_ack, 0x00, late_blob_id)
    assert handles_seen[-1] == late_handle


@pytest.mark.asyncio
async def test_default_ingest_post_uses_loopback_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        def upload(self, path: str, **kwargs: Any) -> dict[str, Any]:
            calls.append((path, kwargs))
            return {"status": "ok"}

    monkeypatch.setattr(blob_receiver, "ConveyClient", lambda: FakeClient())

    response = await blob_receiver._default_ingest_post(
        "20260704",
        "120000_300",
        "browserhost",
        {"source": "default"},
        [("browser_browserhost.jsonl", b'{"event":"default"}\n', "application/jsonl")],
        "browserhost.browser",
    )

    assert response == {"status": "ok"}
    assert calls == [
        (
            "/app/observer/ingest",
            {
                "files": [
                    (
                        "files",
                        (
                            "browser_browserhost.jsonl",
                            b'{"event":"default"}\n',
                            "application/jsonl",
                        ),
                    )
                ],
                "data": {
                    "day": "20260704",
                    "segment": "120000_300",
                    "host": "browserhost",
                    "platform": "browser",
                    "meta": '{"source":"default"}',
                },
                "headers": {OBSERVER_HANDLE_HEADER: "browserhost.browser"},
            },
        )
    ]


def _sealed_blob_ws(
    instance_id: str,
    home_spki: bytes,
    ext_private: ec.EllipticCurvePrivateKey,
    sender_fp: bytes,
    file_content: bytes,
    *,
    host: str = "browserhost",
    segment: str = "120000_300",
) -> tuple[BlobWs, bytes, bytes]:
    blob_id = uuid.uuid4().bytes
    plaintext = _tar_payload(file_content, host=host, segment=segment)
    info = b"spl-blob-v1" + uuid.UUID(instance_id).bytes + sender_fp
    suite = hpke._suite()
    from pyhpke import KEMKey

    home_public = serialization.load_der_public_key(home_spki)
    offer = _offer(sender_fp, blob_id, len(plaintext) + 16)
    enc, ctx = suite.create_sender_context(
        KEMKey.from_pyca_cryptography_key(home_public),
        info=info,
        sks=KEMKey.from_pyca_cryptography_key(ext_private),
    )
    ct = ctx.seal(plaintext, offer)
    assert len(ct) == len(plaintext) + 16
    return BlobWs([offer + enc + ct]), ctx.export(b"spl-blob-ack-v1", 32), blob_id


def _offer(sender_fp: bytes, blob_id: bytes, ct_len: int) -> bytes:
    return (
        b"SBO1"
        + b"\x01"
        + (0x0010).to_bytes(2, "big")
        + (0x0001).to_bytes(2, "big")
        + (0x0002).to_bytes(2, "big")
        + sender_fp
        + blob_id
        + ct_len.to_bytes(8, "big")
    )


def _tar_payload(
    file_content: bytes,
    *,
    host: str,
    segment: str,
) -> bytes:
    blob_json = json.dumps(
        {
            "v": 1,
            "day": "20260704",
            "segment": segment,
            "host": host,
            "meta": {"source": "test"},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb") as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            _add_tar_file(tar, "blob.json", blob_json)
            _add_tar_file(tar, f"browser_{host}.jsonl", file_content)
    return out.getvalue()


def _add_tar_file(tar: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    tar.addfile(info, io.BytesIO(content))


def _ack_tag(k_ack: bytes, status: int, blob_id: bytes) -> bytes:
    return hmac.new(
        k_ack,
        b"spl-blob-ack" + bytes([status]) + blob_id,
        "sha256",
    ).digest()[:16]
