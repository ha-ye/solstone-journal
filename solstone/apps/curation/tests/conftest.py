# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-contained fixtures for curation app tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def curation_env(tmp_path, monkeypatch):
    """Create a temporary journal and Flask client for curation app testing."""

    def _create():
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
        monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")
        import solstone.think.utils as think_utils

        think_utils._journal_path_cache = None

        from solstone.convey import create_app

        app = create_app(journal=str(journal))
        client = app.test_client()

        class Env:
            def __init__(self) -> None:
                self.journal = Path(journal)
                self.client = client
                self.app = app

        return Env()

    return _create
