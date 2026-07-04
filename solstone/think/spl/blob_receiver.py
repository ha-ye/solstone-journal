# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Browser blob receiver for SPL relay tunnels."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import io
import json
import logging
import re
import tarfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solstone.observe.protocol import OBSERVER_HANDLE_HEADER
from solstone.think.convey_client import ConveyClient
from solstone.think.link.auth import AuthorizedClients, ClientEntry
from solstone.think.link.paths import LinkState, authorized_clients_path
from solstone.think.link.upload_key import load_upload_key
from solstone.think.spl.hpke import open_auth
from solstone.think.spl.ws_buffer import BufferedWsReader

log = logging.getLogger(__name__)

OFFER_LEN = 67
READY_LEN = 6
ACK_LEN = 38
ENC_LEN = 65
MAX_CT_LEN = 80 * 1024 * 1024
MAX_ENTRIES = 64
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024

_OFFER_MAGIC = b"SBO1"
_READY_MAGIC = b"SBR1"
_ACK_MAGIC = b"SBA1"
_VERSION = 0x01
_KEM_ID = 0x0010
_KDF_ID = 0x0001
_AEAD_ID = 0x0002
_DAY_RE = re.compile(r"^\d{8}$")
_SEGMENT_RE = re.compile(r"^\d{6}_\d+$")
_AUTHORIZED_CLIENTS: AuthorizedClients | None = None
_AUTHORIZED_CLIENTS_PATH: Path | None = None

BlobIngestPost = Callable[
    [str, str, str, dict[str, Any], list[tuple[str, bytes, str]], str],
    Awaitable[dict[str, Any]] | dict[str, Any],
]


@dataclass(frozen=True)
class Offer:
    header: bytes
    sender_fp: bytes
    blob_id: bytes
    ct_len: int

    @property
    def sender_fingerprint(self) -> str:
        return "sha256:" + self.sender_fp.hex()

    @property
    def blob_id_hex(self) -> str:
        return self.blob_id.hex()


@dataclass(frozen=True)
class UnpackedBlob:
    day: str
    segment: str
    host: str
    meta: dict[str, Any]
    files: list[tuple[str, bytes, str]]


async def receive_blob(
    reader: BufferedWsReader,
    ws: Any,
    *,
    ingest_post: BlobIngestPost | None = None,
) -> None:
    header = await reader.read_exactly(OFFER_LEN)
    try:
        offer = _parse_offer(header)
    except ValueError:
        if header[:4] == _OFFER_MAGIC:
            await _send_ready(ws, 0x01)
        await _close_ws(ws)
        return
    entry = _authorized_browser(offer.sender_fingerprint)
    if entry is None:
        log.info(
            "blob offer rejected: blob_id=%s sender_fp=%s reason=unauthorized",
            offer.blob_id_hex,
            offer.sender_fingerprint,
        )
        await _send_ready(ws, 0x01)
        await _close_ws(ws)
        return
    if not entry.pubkey_spki or not entry.observer_handle:
        log.info(
            "blob offer rejected: blob_id=%s sender_fp=%s reason=incomplete-ledger",
            offer.blob_id_hex,
            offer.sender_fingerprint,
        )
        await _send_ready(ws, 0x01)
        await _close_ws(ws)
        return

    await _send_ready(ws, 0x00)
    enc = await reader.read_exactly(ENC_LEN)
    ct = await reader.read_exactly(offer.ct_len)
    info = _blob_info(offer.sender_fp)
    try:
        opened = open_auth(
            enc,
            load_upload_key().private_key,
            info,
            bytes.fromhex(entry.pubkey_spki),
            ct,
            offer.header,
        )
    except Exception as exc:
        log.warning(
            "blob open failed: blob_id=%s sender_fp=%s ct_len=%d type=%s",
            offer.blob_id_hex,
            offer.sender_fingerprint,
            offer.ct_len,
            type(exc).__name__,
        )
        await _close_ws(ws)
        return

    try:
        unpacked = _safe_unpack(opened.plaintext)
        post = ingest_post or _default_ingest_post
        response = post(
            unpacked.day,
            unpacked.segment,
            unpacked.host,
            unpacked.meta,
            unpacked.files,
            entry.observer_handle,
        )
        if hasattr(response, "__await__"):
            response = await response
        status = _ack_status(response)
    except Exception as exc:
        log.warning(
            "blob ingest failed: blob_id=%s sender_fp=%s type=%s",
            offer.blob_id_hex,
            offer.sender_fingerprint,
            type(exc).__name__,
        )
        await _close_ws(ws)
        return
    k_ack = opened.export(b"spl-blob-ack-v1", 32)
    await ws.send(_ack(offer.blob_id, status, k_ack))
    log.info(
        "blob accepted: blob_id=%s sender_fp=%s status=%#04x files=%d",
        offer.blob_id_hex,
        offer.sender_fingerprint,
        status,
        len(unpacked.files),
    )
    await _close_ws(ws)


def _parse_offer(header: bytes) -> Offer:
    if len(header) != OFFER_LEN:
        raise ValueError("blob offer header length mismatch")
    if header[0:4] != _OFFER_MAGIC:
        raise ValueError("blob offer magic mismatch")
    version = header[4]
    kem_id = int.from_bytes(header[5:7], "big")
    kdf_id = int.from_bytes(header[7:9], "big")
    aead_id = int.from_bytes(header[9:11], "big")
    if (version, kem_id, kdf_id, aead_id) != (
        _VERSION,
        _KEM_ID,
        _KDF_ID,
        _AEAD_ID,
    ):
        raise ValueError("unsupported blob HPKE suite")
    ct_len = int.from_bytes(header[59:67], "big")
    if ct_len > MAX_CT_LEN:
        raise ValueError("blob ciphertext too large")
    return Offer(
        header=header,
        sender_fp=header[11:43],
        blob_id=header[43:59],
        ct_len=ct_len,
    )


def _authorized_browser(fingerprint: str) -> ClientEntry | None:
    store = _authorized_store()
    store.reload_if_stale()
    entry = store.get(fingerprint)
    if entry is None or entry.kind != "browser":
        return None
    return entry


def _authorized_store() -> AuthorizedClients:
    global _AUTHORIZED_CLIENTS, _AUTHORIZED_CLIENTS_PATH

    path = authorized_clients_path()
    if _AUTHORIZED_CLIENTS is None or _AUTHORIZED_CLIENTS_PATH != path:
        _AUTHORIZED_CLIENTS = AuthorizedClients(path)
        _AUTHORIZED_CLIENTS_PATH = path
    return _AUTHORIZED_CLIENTS


def _blob_info(sender_fp: bytes) -> bytes:
    state = LinkState.load_or_create()
    instance_id = uuid.UUID(state.instance_id).bytes
    return b"spl-blob-v1" + instance_id + sender_fp


def _safe_unpack(payload: bytes) -> UnpackedBlob:
    blob_meta: dict[str, Any] | None = None
    files: list[tuple[str, bytes, str]] = []
    entry_count = 0
    total_bytes = 0
    with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as gz:
        with tarfile.open(fileobj=gz, mode="r|") as tar:
            for member in tar:
                entry_count += 1
                if entry_count > MAX_ENTRIES:
                    raise ValueError("blob archive has too many entries")
                _validate_member(member)
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise ValueError(
                        f"blob archive entry cannot be read: {member.name}"
                    )
                content = _read_bounded(extracted, member.name)
                total_bytes += len(content)
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError("blob archive exceeds total size cap")
                if member.name == "blob.json":
                    if blob_meta is not None:
                        raise ValueError(
                            "blob archive contains multiple blob.json entries"
                        )
                    blob_meta = _parse_blob_json(content)
                else:
                    files.append((member.name, content, _content_type(member.name)))
    if blob_meta is None:
        raise ValueError("blob archive missing blob.json")
    if not files:
        raise ValueError("blob archive contains no segment files")
    return UnpackedBlob(
        day=blob_meta["day"],
        segment=blob_meta["segment"],
        host=blob_meta["host"],
        meta=blob_meta["meta"],
        files=files,
    )


def _validate_member(member: tarfile.TarInfo) -> None:
    name = member.name
    if not member.isfile():
        raise ValueError(f"blob archive entry is not a regular file: {name}")
    if (
        not name
        or name in {".", ".."}
        or name.startswith("/")
        or "\\" in name
        or "/" in name
    ):
        raise ValueError(f"blob archive entry has invalid name: {name}")
    if member.size < 0 or member.size > MAX_FILE_BYTES:
        raise ValueError(f"blob archive entry exceeds per-file cap: {name}")


def _read_bounded(handle: Any, name: str) -> bytes:
    out = bytearray()
    while chunk := handle.read(64 * 1024):
        out.extend(chunk)
        if len(out) > MAX_FILE_BYTES:
            raise ValueError(f"blob archive entry exceeds per-file cap: {name}")
    return bytes(out)


def _parse_blob_json(content: bytes) -> dict[str, Any]:
    raw = json.loads(content.decode("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("blob.json must be an object")
    if raw.get("v") != 1:
        raise ValueError("blob.json has unsupported version")
    day = raw.get("day")
    segment = raw.get("segment")
    host = raw.get("host")
    meta = raw.get("meta")
    if not isinstance(day, str) or not _DAY_RE.fullmatch(day):
        raise ValueError("blob.json day is invalid")
    if not isinstance(segment, str) or not _SEGMENT_RE.fullmatch(segment):
        raise ValueError("blob.json segment is invalid")
    if not isinstance(host, str) or not host:
        raise ValueError("blob.json host is invalid")
    if not isinstance(meta, dict):
        raise ValueError("blob.json meta is invalid")
    return {"day": day, "segment": segment, "host": host, "meta": meta}


def _content_type(name: str) -> str:
    if name.endswith(".jsonl"):
        return "application/jsonl"
    if name.endswith(".json"):
        return "application/json"
    return "application/octet-stream"


async def _default_ingest_post(
    day: str,
    segment: str,
    host: str,
    meta: dict[str, Any],
    files: list[tuple[str, bytes, str]],
    observer_handle: str,
) -> dict[str, Any]:
    def post() -> dict[str, Any]:
        return ConveyClient().upload(
            "/app/observer/ingest",
            files=[("files", file_tuple) for file_tuple in files],
            data={
                "day": day,
                "segment": segment,
                "host": host,
                "platform": "browser",
                "meta": json.dumps(meta, separators=(",", ":")),
            },
            headers={OBSERVER_HANDLE_HEADER: observer_handle},
        )

    return await asyncio.to_thread(post)


def _ack_status(response: dict[str, Any]) -> int:
    status = response.get("status")
    if status == "duplicate":
        return 0x01
    if status in {"ok", "collision"}:
        return 0x00
    raise ValueError(f"unexpected observer ingest status: {status!r}")


def _ack(blob_id: bytes, status: int, k_ack: bytes) -> bytes:
    tag = hmac.new(
        k_ack,
        b"spl-blob-ack" + bytes([status]) + blob_id,
        hashlib.sha256,
    ).digest()[:16]
    return _ACK_MAGIC + bytes([_VERSION, status]) + blob_id + tag


async def _send_ready(ws: Any, status: int) -> None:
    await ws.send(_READY_MAGIC + bytes([_VERSION, status]))


async def _close_ws(ws: Any) -> None:
    close = getattr(ws, "close", None)
    if close is not None:
        result = close()
        if hasattr(result, "__await__"):
            await result
