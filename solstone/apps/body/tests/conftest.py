# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-contained fixtures for body app tests."""

from __future__ import annotations

import json
import os

import pytest


@pytest.fixture(scope="module")
def body_app():
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
def body_env(tmp_path, monkeypatch, body_app):
    """Create a temporary journal for body app testing."""
    from solstone.convey import state

    original_journal_root = state.journal_root

    def _create():
        journal = tmp_path / "journal"
        journal.mkdir(exist_ok=True)

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

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
        state.journal_root = str(journal)
        client = body_app.test_client()

        class Env:
            def __init__(self):
                self.journal = journal
                self.client = client
                self.app = body_app

        return Env()

    yield _create
    state.journal_root = original_journal_root
