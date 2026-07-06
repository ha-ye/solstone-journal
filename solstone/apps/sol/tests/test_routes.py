# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
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
    return app.test_client()


def test_sol_day_serves_spa_shell(client):
    response = client.get("/app/sol/20260304")

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_sol_index_redirects_to_spa_shell(client):
    response = client.get("/app/sol/", follow_redirects=True)

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_sol_day_guard_still_404s(client):
    response = client.get("/app/sol/notaday")

    assert response.status_code == 404
