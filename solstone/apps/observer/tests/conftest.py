# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-contained fixtures for observer app tests.

These fixtures do not depend on root conftest.py fixtures. The repo-root path
bootstrap below lets app-only test runs load common test harness helpers.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_PL_FINGERPRINT = "sha256:" + ("c" * 64)


@pytest.fixture(scope="module")
def observer_app():
    """Build the route registry once; per-test fixtures still isolate journals."""
    from solstone.convey import create_app
    from solstone.think.voice.runtime import stop_all_voice_runtime

    previous = os.environ.get("SOLSTONE_DISABLE_CONVEY_SIDE_RUNTIMES")
    os.environ["SOLSTONE_DISABLE_CONVEY_SIDE_RUNTIMES"] = "1"
    try:
        app = create_app()
    finally:
        if previous is None:
            os.environ.pop("SOLSTONE_DISABLE_CONVEY_SIDE_RUNTIMES", None)
        else:
            os.environ["SOLSTONE_DISABLE_CONVEY_SIDE_RUNTIMES"] = previous
    app.config["TESTING"] = True
    yield app
    stop_all_voice_runtime()


@pytest.fixture
def observer_env(tmp_path, monkeypatch, observer_app):
    """Create a temporary journal for observer app testing.

    Returns a factory function that sets up the environment and returns
    the Flask test client along with the journal path.
    """

    from solstone.convey import root as convey_root
    from solstone.convey import state
    from solstone.convey.secure_listener import ConveyIdentity
    from solstone.observe.protocol import OBSERVER_HANDLE_HEADER
    from solstone.think.link.auth import AuthorizedClients
    from solstone.think.link.paths import authorized_clients_path

    original_journal_root = state.journal_root

    def _pl_identity() -> ConveyIdentity:
        return ConveyIdentity(
            mode="pl-via-spl",
            fingerprint=TEST_PL_FINGERPRINT,
            device_label="pl-observer",
            paired_at="2026-05-20T00:00:00Z",
            session_id="session-1",
        )

    class BoundObserverClient:
        def __init__(self, client):
            self._client = client

        def __getattr__(self, name: str) -> Any:
            return getattr(self._client, name)

        def _request_kwargs(self, path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
            adjusted = dict(kwargs)
            overrides = dict(adjusted.pop("environ_overrides", {}) or {})
            headers = adjusted.get("headers")
            if (
                "pl.identity" not in overrides
                and path.startswith("/app/observer/")
                and path != "/app/observer/register"
                and isinstance(headers, dict)
                and ("Authorization" in headers or OBSERVER_HANDLE_HEADER in headers)
            ):
                overrides["pl.identity"] = _pl_identity()
            adjusted["environ_overrides"] = overrides
            return adjusted

        def _bind_created_observer(self, response) -> None:
            if response.status_code != 200:
                return
            data = response.get_json(silent=True)
            key = data.get("key") if isinstance(data, dict) else None
            if not isinstance(key, str) or not key:
                return
            from solstone.apps.observer.utils import load_observer, save_observer

            observer = load_observer(key)
            if observer is None:
                return
            observer["device_binding"] = {
                "device": TEST_PL_FINGERPRINT,
                "kind": "cert",
            }
            assert save_observer(observer)

        def post(self, path: str, *args: Any, **kwargs: Any):
            response = self._client.post(
                path, *args, **self._request_kwargs(path, kwargs)
            )
            if path == "/app/observer/api/create":
                self._bind_created_observer(response)
            return response

        def get(self, path: str, *args: Any, **kwargs: Any):
            return self._client.get(path, *args, **self._request_kwargs(path, kwargs))

        def delete(self, path: str, *args: Any, **kwargs: Any):
            return self._client.delete(
                path, *args, **self._request_kwargs(path, kwargs)
            )

    def _create():
        journal = tmp_path / "journal"
        journal.mkdir()

        config_dir = journal / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "journal.json"
        config_file.write_text(
            json.dumps(
                {
                    "setup": {"completed_at": 1700000000000},
                },
                indent=2,
            )
        )

        # Set environment
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
        state.journal_root = str(journal)
        authorized = AuthorizedClients(authorized_clients_path())
        authorized.add(
            TEST_PL_FINGERPRINT,
            "pl-observer",
            "instance-1",
            paired_at="2026-05-20T00:00:00Z",
        )
        monkeypatch.setattr(convey_root, "get_authorized_clients", lambda: authorized)
        client = BoundObserverClient(observer_app.test_client())

        class Env:
            def __init__(self):
                self.journal = journal
                self.client = client
                self.app = observer_app

        return Env()

    yield _create
    state.journal_root = original_journal_root
