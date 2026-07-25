# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse

import pytest

from solstone.apps.network.routes import _build_pair_link
from solstone.think.link import join_cli

PAIR_LINK = _build_pair_link("10.0.0.42", 7657, "a" * 32, "b" * 64)


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        home="http://receiver",
        code=PAIR_LINK,
        as_role=None,
        label="laptop",
    )


def _bundle_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    bundle = config_home / "solstone-observer" / "spl" / "laptop"
    bundle.mkdir(parents=True)
    return bundle


def test_existing_bundle_file_refuses_overwrite(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle_dir(tmp_path, monkeypatch)
    existing = bundle / "peer.json"
    existing.write_text("existing", encoding="utf-8")

    result = join_cli.main(_args())

    assert result == 1
    assert existing.read_text("utf-8") == "existing"


def test_existing_bundle_path_file_refuses_before_pair_request(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    bundle = config_home / "solstone-observer" / "spl" / "laptop"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("existing", encoding="utf-8")
    calls = []

    def fake_post_pair(*args, **_kwargs):
        calls.append(args)
        raise ValueError("stop")

    monkeypatch.setattr(join_cli, "_post_pair", fake_post_pair)

    result = join_cli.main(_args())

    assert result == 1
    assert calls == []
    assert bundle.read_text("utf-8") == "existing"


def test_existing_ds_store_only_refuses_before_pair_request(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle_dir(tmp_path, monkeypatch)
    (bundle / ".DS_Store").write_text("", encoding="utf-8")
    calls = []

    def fake_post_pair(*args, **_kwargs):
        calls.append(args)
        raise ValueError("stop")

    monkeypatch.setattr(join_cli, "_post_pair", fake_post_pair)

    result = join_cli.main(_args())

    assert result == 1
    assert calls == []


def test_existing_empty_bundle_dir_refuses_before_pair_request(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bundle_dir(tmp_path, monkeypatch)
    calls = []

    def fake_post_pair(*args, **_kwargs):
        calls.append(args)
        raise ValueError("stop")

    monkeypatch.setattr(join_cli, "_post_pair", fake_post_pair)

    result = join_cli.main(_args())

    assert result == 1
    assert calls == []


def test_existing_dangling_bundle_symlink_refuses_before_pair_request(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    bundle = config_home / "solstone-observer" / "spl" / "laptop"
    bundle.parent.mkdir(parents=True)
    bundle.symlink_to(tmp_path / "missing")
    calls = []

    def fake_post_pair(*args, **_kwargs):
        calls.append(args)
        raise ValueError("stop")

    monkeypatch.setattr(join_cli, "_post_pair", fake_post_pair)

    result = join_cli.main(_args())

    assert result == 1
    assert calls == []
    assert bundle.is_symlink()


def test_existing_non_bundle_file_refuses_overwrite(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle_dir(tmp_path, monkeypatch)
    (bundle / "notes.txt").write_text("", encoding="utf-8")

    result = join_cli.main(_args())

    assert result == 1


def test_existing_hidden_bundle_file_refuses_overwrite(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle_dir(tmp_path, monkeypatch)
    (bundle / ".private.pem").write_text("", encoding="utf-8")

    result = join_cli.main(_args())

    assert result == 1
