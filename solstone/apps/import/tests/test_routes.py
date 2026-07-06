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


def test_import_index_serves_injected_spa_shell(client):
    response = client.get("/app/import/")

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_import_detail_serves_spa_shell_even_when_missing(client):
    response = client.get("/app/import/missing-import")

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_import_missing_detail_api_still_returns_not_found(client):
    response = client.get("/app/import/api/missing-import")

    assert response.status_code == 404
    assert response.get_json()["reason_code"] == "import_not_found"


def test_import_detail_api_path_resolves(client):
    adapter = client.application.url_map.bind("localhost")

    endpoint, _args = adapter.match("/app/import/api/missing-import", method="GET")

    assert endpoint == "app:import.import_detail_api"
