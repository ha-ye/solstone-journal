# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

from solstone.think.user_config import (
    config_path,
    default_journal,
    read_user_config,
    write_user_config,
)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("home_value", ["~", "~/", "~/x", "~~", "~x"])
def test_home_rejects_tilde_leading_expanded_values(monkeypatch, home_value):
    monkeypatch.setenv("HOME", home_value)

    for resolve in (default_journal, config_path):
        with pytest.raises(RuntimeError, match="tilde-leading"):
            resolve()


@pytest.mark.parametrize(
    ("home_value", "expected_journal", "expected_config"),
    [
        ("./~", "~/journal", Path("~/.config/solstone/config.toml")),
        ("x~", "x~/journal", Path("x~/.config/solstone/config.toml")),
    ],
)
def test_home_allows_non_tilde_leading_edges(
    monkeypatch, home_value, expected_journal, expected_config
):
    monkeypatch.setenv("HOME", home_value)

    assert default_journal() == expected_journal
    assert config_path() == expected_config


def test_default_journal_returns_home_journal(fake_home):
    assert default_journal() == str(fake_home / "journal")


def test_config_path_returns_user_dot_config(fake_home):
    assert config_path() == fake_home / ".config" / "solstone" / "config.toml"


def test_read_missing_file_returns_empty_dict(fake_home):
    assert read_user_config() == {}


def test_read_malformed_toml_returns_empty_dict(fake_home):
    cfg = config_path()
    cfg.parent.mkdir(parents=True)
    cfg.write_text("this is not = toml", encoding="utf-8")

    assert read_user_config() == {}


def test_read_missing_journal_key_returns_empty_dict_keys(fake_home):
    cfg = config_path()
    cfg.parent.mkdir(parents=True)
    cfg.write_text('other = "x"\n', encoding="utf-8")

    assert read_user_config() == {"other": "x"}


def test_read_drops_non_string_values(fake_home):
    cfg = config_path()
    cfg.parent.mkdir(parents=True)
    cfg.write_text('journal = 123\nname = "ok"\n', encoding="utf-8")

    assert read_user_config() == {"name": "ok"}


def test_write_then_read_roundtrip_plain_path(fake_home):
    write_user_config(journal="/tmp/x")

    assert read_user_config() == {"journal": "/tmp/x"}


def test_write_then_read_roundtrip_path_with_spaces(fake_home):
    write_user_config(journal="/tmp/some path/journal")

    assert read_user_config() == {"journal": "/tmp/some path/journal"}


def test_write_then_read_roundtrip_path_with_quotes_and_backslashes(fake_home):
    journal = '/tmp/with"quote/and\\backslash'

    write_user_config(journal=journal)

    assert read_user_config() == {"journal": journal}


def test_write_creates_parent_directory(fake_home):
    write_user_config(journal="/x")

    assert config_path().parent.is_dir()


def test_write_atomic_no_tmp_left_behind(fake_home):
    write_user_config(journal="/x")

    leftovers = [
        path
        for path in config_path().parent.iterdir()
        if path.name.startswith(".tmp_config")
    ]
    assert leftovers == []


def test_write_returns_config_path(fake_home):
    assert write_user_config(journal="/x") == config_path()
