# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse

import pytest

from solstone.apps.link.routes import _build_pair_link
from solstone.think.link import join_cli

PAIR_LINK = _build_pair_link("192.0.2.42", 7657, "a" * 32, "b" * 64)


def _args(label: str | None) -> argparse.Namespace:
    return argparse.Namespace(
        home="http://receiver",
        code=PAIR_LINK,
        as_role=None,
        label=label,
    )


@pytest.mark.parametrize(
    "label",
    ["", "a" * 81, "a/b", "a\\b", "a..b", ".hidden", "foo bar", "foo*", "foo!"],
)
def test_invalid_labels_exit_2_without_writing(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
) -> None:
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    calls = []
    monkeypatch.setattr(join_cli, "_post_pair", lambda *a, **k: calls.append(a))

    result = join_cli.main(_args(label))

    assert result == 2
    assert calls == []
    assert not (config_home / "solstone-observer" / "spl").exists()


@pytest.mark.parametrize(
    "label",
    ["laptop", "my-laptop", "my_laptop", "laptop.v2", "a", "a" * 80],
)
def test_valid_labels_reach_pair_stage(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    calls = []

    def fake_post_pair(*args, **_kwargs):
        calls.append(args)
        raise ValueError("stop")

    monkeypatch.setattr(join_cli, "_post_pair", fake_post_pair)

    result = join_cli.main(_args(label))

    assert result == 1
    assert len(calls) == 1


def test_explicit_valid_label_is_sent_verbatim(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    calls = []

    def fake_post_pair(_pair_request, body):
        calls.append(body)
        raise ValueError("stop")

    monkeypatch.setattr(join_cli, "_post_pair", fake_post_pair)

    result = join_cli.main(_args("Laptop.v2"))

    assert result == 1
    assert len(calls) == 1
    assert calls[0]["device_label"] == "Laptop.v2"
