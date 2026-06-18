# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse

import pytest

from solstone.apps.link.routes import _build_pair_link
from solstone.think.link import join_cli

PAIR_LINK = _build_pair_link("192.0.2.42", 7657, "a" * 32, "b" * 64)


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


def test_existing_ds_store_only_proceeds_to_next_stage(
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
    assert len(calls) == 1


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
