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
        "local_endpoints": [{"host": "127.0.0.1", "port": 7657}],
        "fingerprint": "sha256:client",
    }


def _mock_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any] | bytes,
    *,
    status: int = 200,
    calls: list[tuple[str, dict[str, Any]]] | None = None,
) -> None:
    body = (
        payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    )

    def fake_urlopen(request, **_kwargs):
        if calls is not None:
            calls.append(
                (
                    request.full_url,
                    json.loads(request.data.decode("utf-8")),
                )
            )
        return _FakeResponse(body, status=status)

    monkeypatch.setattr(join_cli.urllib.request, "urlopen", fake_urlopen)


def test_existing_peer_dir_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    bundle = journal / "peers" / "inst-1"
    bundle.mkdir(parents=True)
    existing = bundle / "private.pem"
    existing.write_bytes(b"sentinel")
    _mock_urlopen(monkeypatch, _success_payload(tmp_path))

    result = join_cli.main(_args())

    assert result == 1
    err = capsys.readouterr().err
    assert "Credentials directory already exists with content" in err
    assert str(bundle) in err
    assert existing.read_bytes() == b"sentinel"
    for name in join_cli.BUNDLE_FILES - {"private.pem"}:
        assert not (bundle / name).exists()
    assert not (tmp_path / "xdg" / "solstone-observer" / "spl" / "my-peer").exists()


def test_existing_peer_dir_with_only_ds_store_proceeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    bundle = journal / "peers" / "inst-1"
    bundle.mkdir(parents=True)
    ds_store = bundle / ".DS_Store"
    ds_store.write_text("", encoding="utf-8")
    _mock_urlopen(monkeypatch, _success_payload(tmp_path))

    result = join_cli.main(_args())

    assert result == 0
    for name in join_cli.BUNDLE_FILES:
        assert (bundle / name).exists()
    assert ds_store.exists()
    assert not (tmp_path / "xdg" / "solstone-observer" / "spl" / "my-peer").exists()
