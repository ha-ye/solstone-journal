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

import pytest

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def observer_app():
    """Build the route registry once; per-test fixtures still isolate journals."""
    from solstone.convey import create_app

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
    return app


@pytest.fixture
def observer_env(tmp_path, monkeypatch, observer_app):
    """Create a temporary journal for observer app testing.

    Returns a factory function that sets up the environment and returns
    the Flask test client along with the journal path.
    """

    from solstone.convey import state

    original_journal_root = state.journal_root

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
        client = observer_app.test_client()

        class Env:
            def __init__(self):
                self.journal = journal
                self.client = client
                self.app = observer_app

        return Env()

    yield _create
    state.journal_root = original_journal_root
