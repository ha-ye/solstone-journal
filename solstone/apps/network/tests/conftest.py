# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-contained fixtures for network app tests."""

from __future__ import annotations

import json

import pytest

from solstone.think.link.local_endpoints import LocalEndpoint


class _FakePairWindowHandle:
    def __init__(self, opened: bool) -> None:
        self._opened = opened
        self.cancelled = False

    def wait_open(self, timeout: float | None = None) -> bool:
        return self._opened

    def cancel(self) -> None:
        self.cancelled = True


class _StubWatcher:
    def __init__(self, endpoints: list[LocalEndpoint]) -> None:
        self._endpoints = endpoints

    def snapshot(self) -> list[LocalEndpoint]:
        return list(self._endpoints)


@pytest.fixture
def link_env(tmp_path, monkeypatch):
    """Create a temporary journal for network app testing."""

    def _create(
        *,
        posture: str | None = None,
        service_token: str | None = None,
        provision: bool = True,
        local_endpoints: list[LocalEndpoint] | None = None,
        pair_window_opens: bool = True,
    ):
        journal = tmp_path / "journal"
        journal.mkdir(exist_ok=True)

        config_dir = journal / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "journal.json"
        config = {
            "setup": {"completed_at": 1700000000000},
        }
        if posture is not None:
            config["link"] = {"posture": posture}
        config_file.write_text(
            json.dumps(config, indent=2),
        )

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
        if provision:
            from solstone.think.link.paths import LinkState

            LinkState.load_or_create()
        if service_token is not None:
            from solstone.think.link.paths import save_service_token

            save_service_token(service_token)

        from solstone.convey import create_app

        app = create_app(journal=str(journal))
        client = app.test_client()
        endpoints = (
            [LocalEndpoint(ip="192.168.1.50", port=7657, scope="lan")]
            if local_endpoints is None
            else list(local_endpoints)
        )
        from solstone.apps.network import routes as link_routes

        monkeypatch.setattr(
            link_routes,
            "get_interface_watcher",
            lambda: _StubWatcher(endpoints),
        )
        pair_window_calls: list[dict] = []
        pair_window_handles: list[_FakePairWindowHandle] = []

        def _record_start_pair_window(**kwargs: object) -> _FakePairWindowHandle:
            pair_window_calls.append(kwargs)
            handle = _FakePairWindowHandle(pair_window_opens)
            pair_window_handles.append(handle)
            return handle

        monkeypatch.setattr(
            link_routes,
            "start_pair_window",
            _record_start_pair_window,
        )

        class Env:
            def __init__(self):
                self.journal = journal
                self.client = client
                self.app = app
                self.pair_window_calls = pair_window_calls
                self.pair_window_handles = pair_window_handles

        return Env()

    return _create
