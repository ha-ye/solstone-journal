# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from solstone.convey import create_app
from solstone.think.indexer.journal import scan_journal


@pytest.fixture
def apostrophe_search_client(tmp_path, monkeypatch):
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

    seeded_day = "20240101"
    talents_dir = journal / "chronicle" / seeded_day / "talents"
    talents_dir.mkdir(parents=True)
    (talents_dir / "flow.md").write_text(
        "# Apostrophe Search\n\n"
        "it's indexed exactly here. "
        "Bob O'Brien said don't panic. "
        "O'Brien brought dogs to the review.\n"
    )

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    yesterday_dir = journal / "chronicle" / yesterday / "talents"
    yesterday_dir.mkdir(parents=True, exist_ok=True)
    (yesterday_dir / "flow.md").write_text(
        "# Yesterday\n\nReviewed yesterday's meeting notes.\n"
    )

    scan_journal(str(journal), full=True)

    app = create_app(journal=str(journal))
    return app.test_client(), seeded_day


def _get_json(response) -> dict[str, Any]:
    payload = response.get_json()
    assert isinstance(payload, dict)
    return payload


def test_search_api_finds_single_apostrophe_term(apostrophe_search_client):
    client, _ = apostrophe_search_client

    response = client.get("/app/search/api/search", query_string={"q": "it's"})

    assert response.status_code == 200
    payload = _get_json(response)
    assert payload["total"] >= 1
    assert payload["days"]


def test_search_api_finds_apostrophe_operator_query(apostrophe_search_client):
    client, _ = apostrophe_search_client

    response = client.get(
        "/app/search/api/search", query_string={"q": "O'Brien AND dogs"}
    )

    assert response.status_code == 200
    payload = _get_json(response)
    assert payload["total"] >= 1
    assert payload["days"]


def test_search_api_finds_temporal_apostrophe_query(apostrophe_search_client):
    client, _ = apostrophe_search_client

    response = client.get(
        "/app/search/api/search", query_string={"q": "yesterday's meeting"}
    )

    assert response.status_code == 200
    payload = _get_json(response)
    assert payload["total"] >= 1
    assert payload["days"]


def test_search_api_apostrophe_only_is_json_not_500(apostrophe_search_client):
    client, _ = apostrophe_search_client

    response = client.get("/app/search/api/search", query_string={"q": "'"})

    assert response.status_code == 200
    assert response.status_code != 500
    payload = _get_json(response)
    assert "total" in payload


def test_day_results_api_handles_apostrophe_query(apostrophe_search_client):
    client, seeded_day = apostrophe_search_client

    response = client.get(
        "/app/search/api/day_results",
        query_string={"q": "it's", "day": seeded_day},
    )

    assert response.status_code == 200
    payload = _get_json(response)
    assert payload["total"] >= 1
    assert payload["results"]
