# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Fixtures for backup app tests."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from solstone.apps.backup import routes as backup_routes


@dataclass
class Env:
    journal: Path
    client: Any
    app: Any


@pytest.fixture(autouse=True)
def clear_backup_registry():
    backup_routes._clear_registry()
    yield
    backup_routes._clear_registry()


@pytest.fixture
def backup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def _create() -> Env:
        journal = tmp_path / "journal"
        journal.mkdir(exist_ok=True)
        config_dir = journal / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "journal.json").write_text(
            json.dumps(
                {
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
