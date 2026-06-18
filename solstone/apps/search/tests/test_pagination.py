# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from typing import Any

import pytest

from solstone.apps.search import routes
from solstone.convey import create_app


@pytest.fixture
def search_client(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    config_dir = journal / "config"
    config_dir.mkdir(parents=True)
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

    app = create_app(journal=str(journal))
    return app.test_client()


def _stub_search(monkeypatch, counts: dict[str, Any]) -> dict[str, int | None]:
    recorded: dict[str, int | None] = {}

    def fake_search_journal(*_args, **kwargs):
        recorded["limit"] = kwargs.get("limit")
        recorded["offset"] = kwargs.get("offset")
        return 0, []

    def fake_counts(*_args, **_kwargs):
        return counts

    monkeypatch.setattr(routes, "search_journal", fake_search_journal)
    monkeypatch.setattr(routes, "search_counts", fake_counts)
    return recorded


def test_day_results_non_numeric_limit_is_200_and_defaults(search_client, monkeypatch):
    recorded = _stub_search(monkeypatch, {"total": 0})

    response = search_client.get(
        "/app/search/api/day_results?q=x&day=20260304&limit=abc"
    )

    assert response.status_code == 200
    assert recorded["limit"] == 20


def test_day_results_high_limit_clamped(search_client, monkeypatch):
    recorded = _stub_search(monkeypatch, {"total": 0})

    response = search_client.get(
        "/app/search/api/day_results?q=x&day=20260304&limit=100000"
    )

    assert response.status_code == 200
    assert recorded["limit"] == 100


def test_day_results_lower_bound(search_client, monkeypatch):
    recorded = _stub_search(monkeypatch, {"total": 0})

    response = search_client.get("/app/search/api/day_results?q=x&day=20260304&limit=0")

    assert response.status_code == 200
    assert recorded["limit"] == 1

    response = search_client.get(
        "/app/search/api/day_results?q=x&day=20260304&offset=-5"
    )

    assert response.status_code == 200
    assert recorded["offset"] == 0


def test_search_non_numeric_limit_is_200(search_client, monkeypatch):
    recorded = _stub_search(
        monkeypatch,
        {"facets": [], "agents": [], "days": [("20260304", 3)], "total": 3},
    )

    response = search_client.get("/app/search/api/search?q=test&limit=abc")

    assert response.status_code == 200
    assert recorded["limit"] == 5
