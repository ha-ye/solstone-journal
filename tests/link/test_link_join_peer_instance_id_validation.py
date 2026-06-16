# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
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


def _pair_response(
    tmp_path: Path, *, instance_id: str = "inst-1"
) -> join_cli.PairResponse:
    ca = generate_ca(tmp_path / "ca")
    ca_pem = ca.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    return join_cli.PairResponse(
        client_cert="-----BEGIN CERTIFICATE-----\nclient\n-----END CERTIFICATE-----\n",
        ca_chain=[ca_pem],
        instance_id=instance_id,
        home_label="solstone",
        home_attestation="header.payload.signature",
        local_endpoints=[{"host": "127.0.0.1", "port": 7657}],
    )


def _mock_post_pair(
    monkeypatch: pytest.MonkeyPatch, response: join_cli.PairResponse
) -> None:
    monkeypatch.setattr(join_cli, "_post_pair", lambda *_args, **_kwargs: response)


def _set_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def _observer_dir(tmp_path: Path) -> Path:
    return tmp_path / "xdg" / "solstone-observer" / "spl" / "my-peer"


def test_traversal_instance_id_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_env(tmp_path, monkeypatch)
    _mock_post_pair(monkeypatch, _pair_response(tmp_path, instance_id="../escape"))

    result = join_cli.main(_args())

    assert result == 1
    assert "bad instance_id from receiver" in capsys.readouterr().err
    assert not (tmp_path / "journal" / "peers").exists()
    assert not _observer_dir(tmp_path).exists()


def test_oversize_instance_id_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_env(tmp_path, monkeypatch)
    _mock_post_pair(monkeypatch, _pair_response(tmp_path, instance_id="x" * 257))

    result = join_cli.main(_args())

    assert result == 1
    assert "bad instance_id from receiver" in capsys.readouterr().err
    assert not (tmp_path / "journal" / "peers").exists()
    assert not _observer_dir(tmp_path).exists()


def test_trailing_newline_instance_id_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_env(tmp_path, monkeypatch)
    _mock_post_pair(monkeypatch, _pair_response(tmp_path, instance_id="abc123\n"))

    result = join_cli.main(_args())

    assert result == 1
    assert "bad instance_id from receiver" in capsys.readouterr().err
    assert not (tmp_path / "journal" / "peers").exists()
    assert not _observer_dir(tmp_path).exists()


def test_alnum_dash_instance_id_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(tmp_path, monkeypatch)
    _mock_post_pair(monkeypatch, _pair_response(tmp_path, instance_id="abc123-def-456"))

    result = join_cli.main(_args())

    assert result == 0
    bundle = tmp_path / "journal" / "peers" / "abc123-def-456"
    for name in join_cli.BUNDLE_FILES:
        assert (bundle / name).exists()
    assert not _observer_dir(tmp_path).exists()
