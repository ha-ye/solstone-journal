# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
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

from solstone.apps.observer.prune import run_prune
from solstone.apps.observer.utils import (
    append_history_record,
    observer_filename_prefix,
    save_observer,
)
from solstone.observe.protocol import OBSERVER_HANDLE_HEADER
from solstone.think.link.auth import AuthorizedClients
from solstone.think.link.paths import LinkState, authorized_clients_path
from solstone.think.link.upload_key import load_or_generate_upload_key
from solstone.think.spl import blob_receiver, hpke
from solstone.think.spl.admission import BlobAdmissionGate
from solstone.think.spl.health import REASON_RELAY_ADMISSION_SATURATED
from solstone.think.spl.ws_buffer import BufferedWsReader
from solstone.think.streams import write_segment_stream
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


class BlockingBlobWs(BlobWs):
    def __init__(self, frames: list[bytes]) -> None:
        super().__init__(frames)
        self.release = asyncio.Event()

    async def recv(self) -> bytes:
        if self.frames:
            return self.frames.pop(0)
        await self.release.wait()
        from websockets.exceptions import ConnectionClosed

        raise ConnectionClosed(None, None)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class AdvancingBlobWs(BlobWs):
    def __init__(
        self,
        frames: list[bytes],
        clock: FakeClock,
        advances: list[float],
    ) -> None:
        super().__init__(frames)
        self.clock = clock
        self.advances = list(advances)

    async def recv(self) -> bytes:
        if self.frames:
            if self.advances:
                self.clock.advance(self.advances.pop(0))
            return self.frames.pop(0)
        from websockets.exceptions import ConnectionClosed

        raise ConnectionClosed(None, None)


def _browser_register_hostname(host: str, sender_fp: bytes) -> str:
    return f"{host}-{sender_fp.hex()[:12]}"


def _setup_browser_receiver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    host: str = "browserhost",
) -> dict[str, Any]:
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
    sender_fingerprint = "sha256:" + sender_fp_bytes.hex()
    browser_hostname = _browser_register_hostname(host, sender_fp_bytes)

    store = AuthorizedClients(authorized_clients_path())
    store.add_browser(
        fingerprint=sender_fingerprint,
        device_label=host,
        instance_id=state.instance_id,
        pubkey_spki=ext_spki.hex(),
        observer_handle=None,
    )
    register = client.post(
        "/app/observer/register",
        json={
            "platform": "browser",
            "hostname": browser_hostname,
            "stream_type": "browser",
            "version": "spl-browser-blob-v1",
        },
    )
    assert register.status_code == 200
    observer_handle = register.get_json()["key"]
    assert store.attach_observer_handle(sender_fingerprint, observer_handle) is True

    responses: list[dict[str, Any]] = []

    async def ingest_post(
        day: str,
        segment: str,
        got_host: str,
        meta: dict[str, Any],
        files: list[tuple[str, bytes, str]],
        observer_handle_arg: str,
    ) -> dict[str, Any]:
        response = client.post(
            "/app/observer/ingest",
            headers={OBSERVER_HANDLE_HEADER: observer_handle_arg},
            data={
                "day": day,
                "segment": segment,
                "host": got_host,
                "platform": "browser",
                "meta": json.dumps(meta, separators=(",", ":")),
                "files": [
                    (io.BytesIO(content), filename)
                    for filename, content, _content_type in files
                ],
            },
        )
        assert response.status_code == 200
        body = response.get_json()
        responses.append(body)
        return body

    return {
        "journal": journal,
        "client": client,
        "state": state,
        "home_spki": home_key.public_spki_der,
        "ext_private": ext_private,
        "sender_fp_bytes": sender_fp_bytes,
        "sender_fp": sender_fingerprint,
        "observer_handle": observer_handle,
        "observer_stream": f"{browser_hostname}.browser",
        "ingest_post": ingest_post,
        "responses": responses,
    }


def _add_authorized_browser_sender(
    *,
    state: LinkState,
    observer_handle: str,
    host: str = "browserhost",
) -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    private = ec.generate_private_key(ec.SECP256R1())
    spki = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    sender_fp = hashes.Hash(hashes.SHA256())
    sender_fp.update(spki)
    sender_fp_bytes = sender_fp.finalize()
    AuthorizedClients(authorized_clients_path()).add_browser(
        fingerprint="sha256:" + sender_fp_bytes.hex(),
        device_label=host,
        instance_id=state.instance_id,
        pubkey_spki=spki.hex(),
        observer_handle=observer_handle,
    )
    return private, sender_fp_bytes


def _write_browser_segment(
    journal: Path,
    *,
    day: str,
    stream: str,
    segment: str,
    seq: int,
    prev: str | None,
    filename: str,
    content: bytes,
) -> Path:
    seg_dir = journal / "chronicle" / day / stream / segment
    seg_dir.mkdir(parents=True)
    write_segment_stream(seg_dir, stream, day if prev else None, prev, seq)
    (seg_dir / filename).write_bytes(content)
    (seg_dir / "ingest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "requested_segment": segment,
                "files": {
                    filename: {
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return seg_dir


def _append_browser_upload_history(
    *,
    prefix: str,
    day: str,
    stream: str,
    segment: str,
    filename: str,
    content: bytes,
) -> None:
    append_history_record(
        prefix,
        day,
        {
            "ts": 1,
            "segment": segment,
            "stream": stream,
            "files": [
                {
                    "submitted": filename,
                    "written": filename,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "disposition": "written",
                }
            ],
        },
    )


def _save_browser_observer(handle: str, name: str, fingerprint: str) -> None:
    assert save_observer(
        {
            "key": handle,
            "name": name,
            "created_at": 1,
            "enabled": True,
            "device_binding": {"device": fingerprint, "kind": "browser"},
            "stats": {"segments_received": 0, "bytes_received": 0},
        }
    )


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
    sender_fingerprint = "sha256:" + sender_fp_bytes.hex()
    browser_hostname = _browser_register_hostname("browserhost", sender_fp_bytes)

    store = AuthorizedClients(authorized_clients_path())
    store.add_browser(
        fingerprint=sender_fingerprint,
        device_label="browserhost",
        instance_id=state.instance_id,
        pubkey_spki=ext_spki.hex(),
        observer_handle=None,
    )
    register = client.post(
        "/app/observer/register",
        json={
            "platform": "browser",
            "hostname": browser_hostname,
            "stream_type": "browser",
            "version": "spl-browser-blob-v1",
        },
    )
    assert register.status_code == 200
    observer_handle = register.get_json()["key"]
    handles_seen: list[str] = []
    assert store.attach_observer_handle(sender_fingerprint, observer_handle) is True

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

    first_payload = _browser_payload("first", 1)
    second_payload = _browser_payload("second", 2)
    third_payload = _browser_payload("third", 3)
    late_payload = _browser_payload("late", 4)

    first_ws, first_k_ack, first_blob_id = _sealed_blob_ws(
        state.instance_id,
        home_key.public_spki_der,
        ext_private,
        sender_fp_bytes,
        first_payload,
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
        / f"{browser_hostname}.browser"
        / "120000_300"
        / "browser_browserhost.jsonl"
    )
    assert stored.read_bytes() == first_payload

    second_ws, second_k_ack, second_blob_id = _sealed_blob_ws(
        state.instance_id,
        home_key.public_spki_der,
        ext_private,
        sender_fp_bytes,
        first_payload,
    )

    await blob_receiver.receive_blob(
        BufferedWsReader(second_ws), second_ws, ingest_post=ingest_post
    )

    assert second_ws.sent[1][0:6] == b"SBA1\x01\x01"
    assert second_ws.sent[1][22:38] == _ack_tag(second_k_ack, 0x01, second_blob_id)
    assert (
        len(
            list(
                (
                    journal / "chronicle" / "20260704" / f"{browser_hostname}.browser"
                ).iterdir()
            )
        )
        == 1
    )

    distinct_ws, distinct_k_ack, distinct_blob_id = _sealed_blob_ws(
        state.instance_id,
        home_key.public_spki_der,
        ext_private,
        sender_fp_bytes,
        second_payload,
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
        / f"{browser_hostname}.browser"
        / "120500_300"
        / "browser_browserhost.jsonl"
    ).read_bytes() == second_payload
    assert handles_seen == [observer_handle, observer_handle, observer_handle]

    AuthorizedClients(authorized_clients_path()).remove(sender_fingerprint)
    rejected_ws, _k_ack, _blob_id = _sealed_blob_ws(
        state.instance_id,
        home_key.public_spki_der,
        ext_private,
        sender_fp_bytes,
        third_payload,
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
    late_hostname = _browser_register_hostname("latebrowser", late_sender_fp_bytes)
    AuthorizedClients(authorized_clients_path()).add_browser(
        fingerprint=late_fingerprint,
        device_label="latebrowser",
        instance_id=state.instance_id,
        pubkey_spki=late_spki.hex(),
        observer_handle=None,
    )
    late_register = client.post(
        "/app/observer/register",
        json={
            "platform": "browser",
            "hostname": late_hostname,
            "stream_type": "browser",
            "version": "spl-browser-blob-v1",
        },
    )
    assert late_register.status_code == 200
    late_handle = late_register.get_json()["key"]
    assert (
        AuthorizedClients(authorized_clients_path()).attach_observer_handle(
            late_fingerprint,
            late_handle,
        )
        is True
    )
    late_ws, late_k_ack, late_blob_id = _sealed_blob_ws(
        state.instance_id,
        home_key.public_spki_der,
        late_private,
        late_sender_fp_bytes,
        late_payload,
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
async def test_browser_blob_refuses_missing_wrong_kind_or_mismatched_handle(
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
    sender_fingerprint = "sha256:" + sender_fp_bytes.hex()

    async def route_ingest_post(
        day: str,
        segment: str,
        host: str,
        meta: dict[str, Any],
        files: list[tuple[str, bytes, str]],
        observer_handle_arg: str,
    ) -> dict[str, Any]:
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
        return response.get_json()

    missing_ws, _missing_k_ack, _missing_blob_id = _sealed_blob_ws(
        state.instance_id,
        home_key.public_spki_der,
        ext_private,
        sender_fp_bytes,
        _browser_payload("missing", 21),
    )
    await blob_receiver.receive_blob(
        BufferedWsReader(missing_ws),
        missing_ws,
        ingest_post=route_ingest_post,
    )
    assert missing_ws.sent == [b"SBR1\x01\x01"]

    AuthorizedClients(authorized_clients_path()).add(
        sender_fingerprint,
        "browserhost",
        state.instance_id,
    )
    wrong_kind_ws, _wrong_kind_k_ack, _wrong_kind_blob_id = _sealed_blob_ws(
        state.instance_id,
        home_key.public_spki_der,
        ext_private,
        sender_fp_bytes,
        _browser_payload("wrong-kind", 22),
        segment="120500_300",
    )
    await blob_receiver.receive_blob(
        BufferedWsReader(wrong_kind_ws),
        wrong_kind_ws,
        ingest_post=route_ingest_post,
    )
    assert wrong_kind_ws.sent == [b"SBR1\x01\x01"]

    intended_handle = "intended-browser-handle"
    wrong_handle = "wrong-browser-handle"
    _save_browser_observer(intended_handle, "intended.browser", sender_fingerprint)
    _save_browser_observer(
        wrong_handle,
        "wrong.browser",
        "sha256:" + ("f" * 64),
    )
    AuthorizedClients(authorized_clients_path()).add_browser(
        fingerprint=sender_fingerprint,
        device_label="browserhost",
        instance_id=state.instance_id,
        pubkey_spki=ext_spki.hex(),
        observer_handle=wrong_handle,
    )
    mismatched_ws, _mismatched_k_ack, _mismatched_blob_id = _sealed_blob_ws(
        state.instance_id,
        home_key.public_spki_der,
        ext_private,
        sender_fp_bytes,
        _browser_payload("mismatch", 23),
        segment="121000_300",
    )
    await blob_receiver.receive_blob(
        BufferedWsReader(mismatched_ws),
        mismatched_ws,
        ingest_post=route_ingest_post,
    )
    assert mismatched_ws.sent == [b"SBR1\x01\x00"]
    assert mismatched_ws.closed is True
    assert not (
        journal
        / "chronicle"
        / "20260704"
        / "wrong.browser"
        / "121000_300"
        / "browser_browserhost.jsonl"
    ).exists()


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


@pytest.mark.asyncio
async def test_blob_offer_timeout_sends_no_ready_or_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _setup_browser_receiver(tmp_path, monkeypatch)
    gate = BlobAdmissionGate(global_ceiling=10, sender_ceiling=1)
    ws = BlockingBlobWs([b"SBO1"])

    await blob_receiver.receive_blob(
        BufferedWsReader(ws),
        ws,
        gate=gate,
        offer_deadline_s=0.01,
    )

    assert ws.sent == []
    assert ws.closed is True
    assert gate.global_count == 0
    assert gate.sender_count(harness["sender_fp"]) == 0
    assert gate.active_senders() == 0


@pytest.mark.asyncio
async def test_blob_enc_timeout_sends_ready_only_and_releases_sender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _setup_browser_receiver(tmp_path, monkeypatch)
    offer, _enc, _ct, _k_ack, _blob_id = _sealed_blob_parts(
        harness["state"].instance_id,
        harness["home_spki"],
        harness["ext_private"],
        harness["sender_fp_bytes"],
        _browser_payload("enc-timeout", 10),
    )
    gate = BlobAdmissionGate(global_ceiling=10, sender_ceiling=1)
    ws = BlockingBlobWs([offer])

    await blob_receiver.receive_blob(
        BufferedWsReader(ws),
        ws,
        gate=gate,
        ingest_post=harness["ingest_post"],
        enc_deadline_s=0.01,
    )

    assert ws.sent == [b"SBR1\x01\x00"]
    assert ws.closed is True
    assert gate.sender_count(harness["sender_fp"]) == 0
    assert gate.active_senders() == 0


@pytest.mark.asyncio
async def test_blob_ct_timeout_sends_ready_only_and_releases_sender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _setup_browser_receiver(tmp_path, monkeypatch)
    offer, enc, ct, _k_ack, _blob_id = _sealed_blob_parts(
        harness["state"].instance_id,
        harness["home_spki"],
        harness["ext_private"],
        harness["sender_fp_bytes"],
        _browser_payload("ct-timeout", 11),
    )
    gate = BlobAdmissionGate(global_ceiling=10, sender_ceiling=1)
    ws = BlockingBlobWs([offer + enc + ct[:1]])

    await blob_receiver.receive_blob(
        BufferedWsReader(ws),
        ws,
        gate=gate,
        ingest_post=harness["ingest_post"],
        ct_deadline_s=0.01,
    )

    assert ws.sent == [b"SBR1\x01\x00"]
    assert ws.closed is True
    assert gate.sender_count(harness["sender_fp"]) == 0
    assert gate.active_senders() == 0


@pytest.mark.asyncio
async def test_blob_ct_progress_timeout_with_drip_feed_releases_sender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _setup_browser_receiver(tmp_path, monkeypatch)
    offer, enc, ct, _k_ack, _blob_id = _sealed_blob_parts(
        harness["state"].instance_id,
        harness["home_spki"],
        harness["ext_private"],
        harness["sender_fp_bytes"],
        _browser_payload("ct-progress-timeout", 12),
    )
    clock = FakeClock()
    gate = BlobAdmissionGate(global_ceiling=10, sender_ceiling=1)
    ws = AdvancingBlobWs(
        [offer + enc, ct[:1], ct[1:2]],
        clock,
        advances=[0.0, 1.1, 1.1],
    )

    await blob_receiver.receive_blob(
        BufferedWsReader(ws),
        ws,
        gate=gate,
        ingest_post=harness["ingest_post"],
        ct_deadline_s=100.0,
        ct_window_s=1.0,
        ct_min_bytes_per_window=2,
        time_source=clock,
    )

    assert ws.sent == [b"SBR1\x01\x00"]
    assert ws.closed is True
    assert gate.sender_count(harness["sender_fp"]) == 0
    assert gate.active_senders() == 0


@pytest.mark.asyncio
async def test_blob_cancel_mid_ciphertext_releases_sender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _setup_browser_receiver(tmp_path, monkeypatch)
    offer, enc, ct, _k_ack, _blob_id = _sealed_blob_parts(
        harness["state"].instance_id,
        harness["home_spki"],
        harness["ext_private"],
        harness["sender_fp_bytes"],
        _browser_payload("ct-cancel", 18),
    )
    gate = BlobAdmissionGate(global_ceiling=10, sender_ceiling=1)
    ws = BlockingBlobWs([offer + enc + ct[:1]])
    task = asyncio.create_task(
        blob_receiver.receive_blob(
            BufferedWsReader(ws),
            ws,
            gate=gate,
            ingest_post=harness["ingest_post"],
            ct_deadline_s=30.0,
        )
    )
    try:
        for _ in range(100):
            if ws.sent and gate.sender_count(harness["sender_fp"]) == 1:
                break
            await asyncio.sleep(0)
        assert ws.sent == [b"SBR1\x01\x00"]
        assert gate.sender_count(harness["sender_fp"]) == 1

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert gate.sender_count(harness["sender_fp"]) == 0
    assert gate.active_senders() == 0


@pytest.mark.asyncio
async def test_blob_per_sender_cap_rejects_without_ready_and_prunes_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _setup_browser_receiver(tmp_path, monkeypatch)
    gate = BlobAdmissionGate(global_ceiling=10, sender_ceiling=1)
    events: list[tuple[str, dict[str, Any]]] = []
    offer, enc, ct, _k_ack, _blob_id = _sealed_blob_parts(
        harness["state"].instance_id,
        harness["home_spki"],
        harness["ext_private"],
        harness["sender_fp_bytes"],
        _browser_payload("held-a", 13),
        segment="121000_300",
    )
    held_ws = BlockingBlobWs([offer + enc + ct[:1]])
    held_task = asyncio.create_task(
        blob_receiver.receive_blob(
            BufferedWsReader(held_ws),
            held_ws,
            gate=gate,
            ingest_post=harness["ingest_post"],
            ct_deadline_s=30.0,
        )
    )
    for _ in range(100):
        if held_ws.sent and gate.sender_count(harness["sender_fp"]) == 1:
            break
        await asyncio.sleep(0)
    assert held_ws.sent == [b"SBR1\x01\x00"]
    assert gate.sender_count(harness["sender_fp"]) == 1

    rejected_ws, _k_ack, _blob_id = _sealed_blob_ws(
        harness["state"].instance_id,
        harness["home_spki"],
        harness["ext_private"],
        harness["sender_fp_bytes"],
        _browser_payload("rejected-a", 14),
        segment="121500_300",
    )
    await blob_receiver.receive_blob(
        BufferedWsReader(rejected_ws),
        rejected_ws,
        gate=gate,
        emit=lambda event, fields: events.append((event, dict(fields))),
        ingest_post=harness["ingest_post"],
    )
    assert rejected_ws.sent == []
    assert rejected_ws.closed is True
    assert events == [
        (
            "admission_saturated",
            {"reason": REASON_RELAY_ADMISSION_SATURATED, "count": 1},
        )
    ]
    serialized_events = json.dumps(events)
    assert harness["sender_fp"] not in serialized_events
    assert "rejected-a" not in serialized_events

    sender_b_private, sender_b_fp = _add_authorized_browser_sender(
        state=harness["state"],
        observer_handle=harness["observer_handle"],
    )
    sender_b_ws, sender_b_k_ack, sender_b_blob_id = _sealed_blob_ws(
        harness["state"].instance_id,
        harness["home_spki"],
        sender_b_private,
        sender_b_fp,
        _browser_payload("sender-b", 15),
        segment="122000_300",
    )
    await blob_receiver.receive_blob(
        BufferedWsReader(sender_b_ws),
        sender_b_ws,
        gate=gate,
        ingest_post=harness["ingest_post"],
    )
    assert sender_b_ws.sent[0] == b"SBR1\x01\x00"
    assert sender_b_ws.sent[1][0:6] == b"SBA1\x01\x00"
    assert sender_b_ws.sent[1][22:38] == _ack_tag(
        sender_b_k_ack, 0x00, sender_b_blob_id
    )

    held_ws.release.set()
    await held_task
    assert gate.sender_count(harness["sender_fp"]) == 0
    assert gate.active_senders() == 0

    accepted_ws, accepted_k_ack, accepted_blob_id = _sealed_blob_ws(
        harness["state"].instance_id,
        harness["home_spki"],
        harness["ext_private"],
        harness["sender_fp_bytes"],
        _browser_payload("accepted-a", 16),
        segment="122500_300",
    )
    await blob_receiver.receive_blob(
        BufferedWsReader(accepted_ws),
        accepted_ws,
        gate=gate,
        ingest_post=harness["ingest_post"],
    )
    assert accepted_ws.sent[0] == b"SBR1\x01\x00"
    assert accepted_ws.sent[1][0:6] == b"SBA1\x01\x00"
    assert accepted_ws.sent[1][22:38] == _ack_tag(
        accepted_k_ack, 0x00, accepted_blob_id
    )
    assert gate.sender_count(harness["sender_fp"]) == 0
    assert gate.active_senders() == 0


@pytest.mark.asyncio
async def test_lost_ack_retry_resolves_duplicate_via_prune_survivor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _setup_browser_receiver(tmp_path, monkeypatch)
    day = "20260704"
    stream = harness["observer_stream"]
    filename = "browser_browserhost.jsonl"
    content = _browser_payload("lost-ack", 17)
    prefix = observer_filename_prefix({"key": harness["observer_handle"]})

    canonical_dir = _write_browser_segment(
        harness["journal"],
        day=day,
        stream=stream,
        segment="120000_300",
        seq=1,
        prev=None,
        filename=filename,
        content=content,
    )
    redundant_dir = _write_browser_segment(
        harness["journal"],
        day=day,
        stream=stream,
        segment="120000_301",
        seq=2,
        prev="120000_300",
        filename=filename,
        content=content,
    )
    for segment in ("120000_300", "120000_301"):
        _append_browser_upload_history(
            prefix=prefix,
            day=day,
            stream=stream,
            segment=segment,
            filename=filename,
            content=content,
        )

    result = run_prune(days=[day], stream=stream, execute=True)

    assert result.refusals == []
    assert [candidate.analysis.segment for candidate in result.deleted] == [
        "120000_301"
    ]
    assert not redundant_dir.exists()
    assert (canonical_dir / filename).read_bytes() == content
    assert (canonical_dir / "ingest.json").exists()

    retry_ws, retry_k_ack, blob_id_b3 = _sealed_blob_ws(
        harness["state"].instance_id,
        harness["home_spki"],
        harness["ext_private"],
        harness["sender_fp_bytes"],
        content,
        segment="120000_301",
    )
    # This fresh B3 was not present in the direct-written/pruned setup; the retry
    # still dedupes by content identity rather than blob id.
    await blob_receiver.receive_blob(
        BufferedWsReader(retry_ws),
        retry_ws,
        ingest_post=harness["ingest_post"],
    )

    assert retry_ws.sent[0] == b"SBR1\x01\x00"
    assert retry_ws.sent[1][0:6] == b"SBA1\x01\x01"
    assert retry_ws.sent[1][6:22] == blob_id_b3
    assert retry_ws.sent[1][22:38] == _ack_tag(retry_k_ack, 0x01, blob_id_b3)
    assert harness["responses"][-1]["status"] == "duplicate"
    assert harness["responses"][-1]["existing_segment"] == "120000_300"
    assert not redundant_dir.exists()

    reused_ws, reused_k_ack, reused_blob_id = _sealed_blob_ws(
        harness["state"].instance_id,
        harness["home_spki"],
        harness["ext_private"],
        harness["sender_fp_bytes"],
        content,
        segment="120000_301",
        blob_id=blob_id_b3,
    )
    await blob_receiver.receive_blob(
        BufferedWsReader(reused_ws),
        reused_ws,
        ingest_post=harness["ingest_post"],
    )

    assert reused_blob_id == blob_id_b3
    assert reused_ws.sent[0] == b"SBR1\x01\x00"
    assert reused_ws.sent[1][0:6] == b"SBA1\x01\x01"
    assert reused_ws.sent[1][6:22] == blob_id_b3
    assert reused_ws.sent[1][22:38] == _ack_tag(reused_k_ack, 0x01, blob_id_b3)
    assert harness["responses"][-1]["status"] == "duplicate"
    assert harness["responses"][-1]["existing_segment"] == "120000_300"
    assert not redundant_dir.exists()


def _sealed_blob_ws(
    instance_id: str,
    home_spki: bytes,
    ext_private: ec.EllipticCurvePrivateKey,
    sender_fp: bytes,
    file_content: bytes,
    *,
    host: str = "browserhost",
    segment: str = "120000_300",
    blob_id: bytes | None = None,
) -> tuple[BlobWs, bytes, bytes]:
    offer, enc, ct, k_ack, blob_id = _sealed_blob_parts(
        instance_id,
        home_spki,
        ext_private,
        sender_fp,
        file_content,
        host=host,
        segment=segment,
        blob_id=blob_id,
    )
    return BlobWs([offer + enc + ct]), k_ack, blob_id


def _sealed_blob_parts(
    instance_id: str,
    home_spki: bytes,
    ext_private: ec.EllipticCurvePrivateKey,
    sender_fp: bytes,
    file_content: bytes,
    *,
    host: str = "browserhost",
    segment: str = "120000_300",
    blob_id: bytes | None = None,
) -> tuple[bytes, bytes, bytes, bytes, bytes]:
    blob_id = blob_id or uuid.uuid4().bytes
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
    return offer, enc, ct, ctx.export(b"spl-blob-ack-v1", 32), blob_id


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


def _browser_payload(text: str, ts: int) -> bytes:
    return (
        json.dumps(
            {
                "t": "delta",
                "ts": ts,
                "op": "add",
                "block": {"type": "text", "text": text},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _ack_tag(k_ack: bytes, status: int, blob_id: bytes) -> bytes:
    return hmac.new(
        k_ack,
        b"spl-blob-ack" + bytes([status]) + blob_id,
        "sha256",
    ).digest()[:16]
