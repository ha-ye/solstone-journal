# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

WORKSPACE_PATH = Path(__file__).resolve().parents[1] / "workspace.html"


@pytest.fixture
def app(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True)
    (journal / "config" / "journal.json").write_text(
        json.dumps({"setup": {"completed_at": 1700000000000}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    from solstone.convey import create_app

    app = create_app(journal=str(journal))
    app.config.update(TESTING=True)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_support_index_serves_injected_spa_shell(client):
    response = client.get("/app/support/")

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_support_static_literal_path_resolves(app):
    adapter = app.url_map.bind("localhost")

    endpoint, _args = adapter.match("/app/support/static/support.js", method="GET")

    assert endpoint
