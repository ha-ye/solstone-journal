# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path

import pytest

from solstone.think.link import join_cli
from tests.link.pairing_harness import pairing_harness


def _args(
    *,
    code: str,
    home: str | None = "http://receiver",
    as_role: str | None = None,
    label: str | None = "laptop",
) -> argparse.Namespace:
    return argparse.Namespace(home=home, code=code, as_role=as_role, label=label)


def _configure_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    return config_home


def test_url_happy_path_posts_to_pair_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterion 1: pair-link joins use the secure listener's framed transport.
    config_home = _configure_home(tmp_path, monkeypatch)
    nonce = "a1b2c3d4e5f607181122334455667788"
    with pairing_harness(tmp_path, monkeypatch) as harness:
        harness.seed_nonce(nonce, "laptop")
        pair_link = harness.pair_link(nonce)

        result = join_cli.main(_args(code=pair_link, home=None))

    assert result == 0
    bundle = config_home / "solstone-observer" / "spl" / "laptop"
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
    assert peer["label"] == "laptop"
    assert peer["home_label"] == "solstone"
    assert peer["fingerprint"].startswith("sha256:")
    assert peer["role"] == ""
