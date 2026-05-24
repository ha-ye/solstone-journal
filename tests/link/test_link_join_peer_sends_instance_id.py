# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization

from solstone.think.link import join_cli
from solstone.think.link.ca import generate_ca
from solstone.think.link.paths import LinkState


class _FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        home="http://receiver",
        code="ABCD-EFGH",
        as_role="peer",
        label="my-peer",
    )


def _success_payload(tmp_path: Path) -> dict[str, Any]:
    ca = generate_ca(tmp_path / "ca")
    ca_pem = ca.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    return {
        "client_cert": "-----BEGIN CERTIFICATE-----\nclient\n-----END CERTIFICATE-----\n",
        "ca_chain": [ca_pem],
        "instance_id": "inst-1",
        "home_label": "solstone",
        "home_attestation": "header.payload.signature",
        "local_endpoints": [],
        "fingerprint": "sha256:client",
    }


def _mock_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    calls: list[tuple[str, dict[str, Any]]],
) -> None:
    body = json.dumps(payload).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        calls.append(
            (
                request.full_url,
                json.loads(request.data.decode("utf-8")),
            )
        )
        return _FakeResponse(body)

    monkeypatch.setattr(join_cli.urllib.request, "urlopen", fake_urlopen)


def test_peer_join_posts_sender_instance_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    expected_instance_id = LinkState.load_or_create().instance_id
    calls: list[tuple[str, dict[str, Any]]] = []
    _mock_urlopen(monkeypatch, _success_payload(tmp_path), calls)

    result = join_cli.main(_args())

    assert result == 0
    assert len(calls) == 1
    url, body = calls[0]
    assert url == "http://receiver/app/link/by-code"
    assert body["code"] == "ABCDEFGH"
    assert body["device_label"] == "my-peer"
    assert body["sender_instance_id"] == expected_instance_id
    assert isinstance(body["csr"], str)
