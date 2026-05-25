# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Self-contained fixtures for tokens app tests."""

from __future__ import annotations

import json
from collections import namedtuple

import pytest


@pytest.fixture
def tokens_env(tmp_path, monkeypatch):
    """Create a temporary journal for tokens app testing."""

    def _create(token_logs: dict[str, list[dict]]):
        journal = tmp_path / "journal"
        journal.mkdir(exist_ok=True)

        config_dir = journal / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "journal.json"
        config_file.write_text(
            json.dumps(
                {
                    "convey": {"trust_localhost": True},
                    "setup": {"completed_at": 1700000000000},
                },
                indent=2,
            )
        )

        tokens_dir = journal / "tokens"
        tokens_dir.mkdir(parents=True, exist_ok=True)
        for day, entries in token_logs.items():
            lines = [json.dumps(entry) for entry in entries]
            (tokens_dir / f"{day}.jsonl").write_text(
                "\n".join(lines) + ("\n" if lines else ""),
                encoding="utf-8",
            )

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

        from solstone.convey import create_app

        app = create_app(journal=str(journal))
        client = app.test_client()

        Env = namedtuple("Env", ["journal", "client", "app"])
        return Env(journal, client, app)

    return _create
