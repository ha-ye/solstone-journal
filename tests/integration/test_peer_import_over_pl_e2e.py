# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import requests

import solstone.think.utils as think_utils
from solstone.think.link.client import (
    Client,
    ClientIdentity,
    EnrolledDevice,
    _build_csr,
)
from solstone.think.link.paths import LinkState
from tests.link.live_helpers import running_convey_server

pytestmark = pytest.mark.integration


def test_peer_segment_import_over_pl_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender_journal = tmp_path / "sender-journal"
    sender_journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(sender_journal))
    think_utils._journal_path_cache = None
    sender_instance_id = LinkState.load_or_create().instance_id

    tmp_journal = tmp_path / "journal"
    tmp_journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_journal))
    think_utils._journal_path_cache = None

    with running_convey_server(tmp_journal) as base_url:
        identity = _pair_peer(base_url, "pytest-peer", sender_instance_id)
        prefix = identity.fingerprint.replace("sha256:", "")[:16]
        body, content_type = _multipart_segment_body()

        status, _headers, response_body = asyncio.run(
            _post_over_pl(
                identity,
                f"/app/import/journal/{prefix}/ingest/segments",
                body,
                content_type,
            )
        )

    assert status == 200
    payload = json.loads(response_body.decode("utf-8"))
    assert payload["segments_received"] == 1
    state_path = tmp_journal / "imports" / prefix / "segments" / "state.json"
    log_path = tmp_journal / "imports" / prefix / "segments" / "log.jsonl"
    state_data = json.loads(state_path.read_text("utf-8"))
    log_entry = json.loads(log_path.read_text("utf-8").splitlines()[0])
    assert (
        state_data["20260520"]["pytest-peer/120000_300"]["sender_fingerprint"]
        == identity.fingerprint
    )
    assert (
        state_data["20260520"]["pytest-peer/120000_300"]["sender_instance_id"]
        == sender_instance_id
    )
    assert log_entry["sender_fingerprint"] == identity.fingerprint
    assert log_entry["sender_instance_id"] == sender_instance_id


def _pair_peer(base_url: str, label: str, sender_instance_id: str) -> ClientIdentity:
    start = requests.post(
        f"{base_url}/app/link/pair-start",
        json={"device_label": label, "role": "peer"},
        timeout=10,
    )
    start.raise_for_status()
    private_key_pem, csr_pem = _build_csr(label)
    paired = requests.post(
        f"{base_url}/app/link/pair",
        json={
            "nonce": start.json()["nonce"],
            "csr": csr_pem,
            "device_label": label,
            "sender_instance_id": sender_instance_id,
        },
        timeout=10,
    )
    paired.raise_for_status()
    payload = paired.json()
    return ClientIdentity(
        private_key_pem=private_key_pem,
        client_cert_pem=payload["client_cert"],
        ca_chain_pem="".join(payload["ca_chain"]),
        fingerprint=payload["fingerprint"],
        home_instance_id=payload["instance_id"],
        home_label=payload["home_label"],
        home_attestation=payload["home_attestation"],
        local_endpoints=tuple(payload.get("local_endpoints", [])),
    )


async def _post_over_pl(
    identity: ClientIdentity,
    path: str,
    body: bytes,
    content_type: str,
) -> tuple[int, dict[str, str], bytes]:
    enrolled = EnrolledDevice(device_token="", identity=identity)
    async with await Client.dial_direct("127.0.0.1", enrolled, port=7657) as session:
        return await session.request(
            "POST",
            path,
            headers={"content-type": content_type},
            body=body,
        )


def _multipart_segment_body() -> tuple[bytes, str]:
    boundary = "solstonepytestboundary"
    metadata = json.dumps(
        {
            "segments": [
                {
                    "day": "20260520",
                    "stream": "pytest-peer",
                    "segment_key": "120000_300",
                    "files": ["audio.flac"],
                }
            ]
        }
    )
    parts = [
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="metadata"\r\n\r\n'
            f"{metadata}\r\n"
        ).encode("utf-8"),
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files_0"; filename="audio.flac"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        + b"peer over pl"
        + b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
