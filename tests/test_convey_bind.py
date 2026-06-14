# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from unittest.mock import patch

from solstone.convey.cli import _resolve_bind_host


def test_resolve_bind_host_returns_localhost():
    assert _resolve_bind_host() == "127.0.0.1"


def test_resolve_bind_host_ignores_stale_network_access_flag():
    with patch(
        "solstone.think.utils.get_config",
        return_value={"convey": {"allow_network_access": True}},
    ):
        assert _resolve_bind_host() == "127.0.0.1"
