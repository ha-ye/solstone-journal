# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization

from solstone.apps.network.routes import _build_pair_link
from solstone.think.link import join_cli
from solstone.think.link.ca import generate_ca

PAIR_LINK = _build_pair_link("10.0.0.42", 7657, "a" * 32, "b" * 64)


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
    before_listing = sorted(path.name for path in bundle.parent.iterdir())
    _mock_post_pair(monkeypatch, _pair_response(tmp_path))

    result = join_cli.main(_args())

    assert result == 1
    err = capsys.readouterr().err
    assert "Credentials path already exists" in err
    assert str(bundle) in err
    assert existing.read_bytes() == b"sentinel"
    assert sorted(path.name for path in bundle.parent.iterdir()) == before_listing
    for name in join_cli.BUNDLE_FILES - {"private.pem"}:
        assert not (bundle / name).exists()
    assert not (tmp_path / "xdg" / "solstone-observer" / "spl" / "my-peer").exists()


def test_existing_peer_dir_with_only_ds_store_refuses(
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
    _mock_post_pair(monkeypatch, _pair_response(tmp_path))

    result = join_cli.main(_args())

    assert result == 1
    assert ds_store.exists()
    assert sorted(path.name for path in bundle.iterdir()) == [".DS_Store"]
    assert not (tmp_path / "xdg" / "solstone-observer" / "spl" / "my-peer").exists()


def test_existing_empty_peer_dir_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    bundle = journal / "peers" / "inst-1"
    bundle.mkdir(parents=True)
    _mock_post_pair(monkeypatch, _pair_response(tmp_path))

    result = join_cli.main(_args())

    assert result == 1
    assert list(bundle.iterdir()) == []


def test_existing_peer_symlink_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    bundle = journal / "peers" / "inst-1"
    bundle.parent.mkdir(parents=True)
    bundle.symlink_to(tmp_path / "missing")
    _mock_post_pair(monkeypatch, _pair_response(tmp_path))

    result = join_cli.main(_args())

    assert result == 1
    assert bundle.is_symlink()
