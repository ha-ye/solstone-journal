# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from solstone.apps.link.routes import _build_pair_link
from solstone.think.link import join_cli
from solstone.think.link.ca import generate_ca

PAIR_LINK = _build_pair_link("192.0.2.42", 7657, "a" * 32, "b" * 64)


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        home="http://receiver",
        code=PAIR_LINK,
        as_role="peer",
        label="my-peer",
    )


def _pair_response(tmp_path: Path) -> join_cli.PairResponse:
    ca = generate_ca(tmp_path / "ca")
    ca_pem = ca.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    return join_cli.PairResponse(
        client_cert="-----BEGIN CERTIFICATE-----\nclient\n-----END CERTIFICATE-----\n",
        ca_chain=[ca_pem],
        instance_id="inst-1",
        home_label="solstone",
        home_attestation="header.payload.signature",
        local_endpoints=[{"host": "127.0.0.1", "port": 7657}],
    )


def _mock_post_pair(
    monkeypatch: pytest.MonkeyPatch, response: join_cli.PairResponse
) -> None:
    monkeypatch.setattr(join_cli, "_post_pair", lambda *_args, **_kwargs: response)


def test_pair_link_happy_path_writes_peer_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _mock_post_pair(monkeypatch, _pair_response(tmp_path))

    result = join_cli.main(_args())

    assert result == 0
    bundle = tmp_path / "journal" / "peers" / "inst-1"
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
    for name in join_cli.BUNDLE_FILES:
        assert (bundle / name).exists()
        assert stat.S_IMODE((bundle / name).stat().st_mode) == 0o600
    peer = json.loads((bundle / "peer.json").read_text("utf-8"))
    assert list(peer.keys()) == [
        "label",
        "paired_at",
        "instance_id",
        "home_label",
        "fingerprint",
        "local_endpoints",
        "role",
    ]
    assert peer["role"] == "peer"
    assert peer["instance_id"] == "inst-1"
    assert peer["label"] == "my-peer"
    assert not (tmp_path / "xdg" / "solstone-observer" / "spl" / "my-peer").exists()
