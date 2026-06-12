# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Fixtures for services app tests."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from solstone.apps.services import routes as services_routes


@dataclass
class Env:
    journal: Path
    client: object
    app: object


@pytest.fixture(autouse=True)
def clear_services_registry():
    services_routes._clear_registry()
    yield
    services_routes._clear_registry()


@pytest.fixture
def services_env(tmp_path, monkeypatch):
    def _create() -> Env:
        journal = tmp_path / "journal"
        journal.mkdir(exist_ok=True)
        config_dir = journal / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "journal.json").write_text(
            json.dumps(
                {
                    "convey": {"trust_localhost": True},
                    "setup": {"completed_at": 1700000000000},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

        from solstone.convey import create_app

        app = create_app(journal=str(journal))
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
            session.permanent = True
        return Env(journal=journal, client=client, app=app)

    return _create


def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


@pytest.fixture
def wait_until_helper():
    return wait_until
