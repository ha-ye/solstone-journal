# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.think.app_supervised import (
    FLAG,
    PARENT_FD_ENV,
    SELECTOR_ENV,
    is_app_supervised,
    resolve_parent_fd,
)


def test_is_app_supervised_uses_cli_flag_or_env_with_or_semantics(monkeypatch):
    monkeypatch.delenv(SELECTOR_ENV, raising=False)
    assert is_app_supervised(["journal", "start"]) is False

    assert is_app_supervised(["journal", "start", FLAG]) is True

    monkeypatch.setenv(SELECTOR_ENV, "1")
    assert is_app_supervised(["journal", "start"]) is True

    monkeypatch.setenv(SELECTOR_ENV, "0")
    assert is_app_supervised(["journal", "start"]) is False
    assert is_app_supervised(["journal", "start", FLAG]) is True


def test_resolve_parent_fd_defaults_to_stdin_and_ignores_bad_env(monkeypatch):
    monkeypatch.delenv(PARENT_FD_ENV, raising=False)
    assert resolve_parent_fd() == 0

    monkeypatch.setenv(PARENT_FD_ENV, "7")
    assert resolve_parent_fd() == 7

    monkeypatch.setenv(PARENT_FD_ENV, "garbage")
    assert resolve_parent_fd() == 0
