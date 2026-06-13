# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from unittest.mock import MagicMock

from solstone.think import start


def test_start_invokes_supervisor(monkeypatch):
    supervisor = MagicMock()
    monkeypatch.setattr("solstone.think.supervisor.main", supervisor)

    start.main()

    supervisor.assert_called_once_with()


def test_start_exports_only_main_callable():
    callables = {
        name
        for name, value in vars(start).items()
        if callable(value) and not name.startswith("__")
    }

    assert callables == {"main"}
