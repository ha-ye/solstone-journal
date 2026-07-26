# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from solstone.apps.search import routes
from solstone.convey import create_app
from solstone.convey.icons import lucide_svg


def _counts_payload(counts: dict[str, Any]) -> dict[str, Any]:
    return {
        "facets": {},
        "agents": {},
        "days": {},
        "streams": {},
        "total": 0,
        "relaxed": False,
        **counts,
    }


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


def _stub_search(
    monkeypatch,
    counts: dict[str, Any] | list[dict[str, Any]],
    *,
    coverage: dict[str, str] | None = None,
    results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    recorded: dict[str, Any] = {"search_counts": [], "search_journal": []}
    count_payloads = counts if isinstance(counts, list) else [counts]

    def fake_search_journal(*_args, **kwargs):
        recorded["limit"] = kwargs.get("limit")
        recorded["offset"] = kwargs.get("offset")
        recorded["search_journal"].append(kwargs)
        return 0, results or []

    def fake_counts(*_args, **kwargs):
        recorded["search_counts"].append(kwargs)
        index = min(len(recorded["search_counts"]) - 1, len(count_payloads) - 1)
        return _counts_payload(count_payloads[index])

    monkeypatch.setattr(routes, "search_journal", fake_search_journal)
    monkeypatch.setattr(routes, "search_counts", fake_counts)
    monkeypatch.setattr(routes, "get_corpus_day_coverage", lambda: coverage)
    return recorded


def test_agent_label_humanises_unmapped_ids_and_handles_empty():
    assert routes._agent_label("flow") == "Flow"
    assert routes._agent_label("_todos_todo") == "Todos Todo"
    assert routes._agent_label("morning_briefing") == "Morning Briefing"
    assert routes._agent_label("") == ""
    assert routes._agent_label(None) == ""


def test_search_routes_no_longer_use_agent_title():
    text = Path(routes.__file__).read_text(encoding="utf-8")

    assert "agent.title()" not in text


def test_unmapped_talent_payload_keeps_raw_name_and_humanises_label(
    search_client, monkeypatch
):
    _stub_search(
        monkeypatch,
        {
            "agents": {"_todos_todo": 4},
            "days": {"20260304": 2},
            "total": 2,
        },
    )

    response = search_client.get("/app/search/api/search?q=test")

    assert response.status_code == 200
    payload = response.get_json()
    assert [
        {key: talent[key] for key in ("name", "label", "icon", "icon_svg", "count")}
        for talent in payload["talents"]
    ] == [
        {
            "name": "_todos_todo",
            "label": "Todos Todo",
            "icon": "file-text",
            "icon_svg": lucide_svg("file-text"),
            "count": 4,
        }
    ]


def test_search_payload_resolves_agent_icon_svg(search_client, monkeypatch):
    _stub_search(
        monkeypatch,
        {
            "agents": {"flow": 4},
            "days": {"20260304": 1},
            "total": 1,
        },
        results=[
            {
                "id": "result-1",
                "text": "flow result",
                "metadata": {
                    "day": "20260304",
                    "agent": "flow",
                    "facet": "",
                    "path": "20260304/talents/flow.md",
                    "idx": 0,
                },
                "score": 1.0,
            }
        ],
    )

    response = search_client.get("/app/search/api/search?q=test")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["talents"][0]["icon"] == "activity"
    assert payload["talents"][0]["icon_svg"] == lucide_svg("activity")
    result = payload["days"][0]["results"][0]
    assert result["agent_icon"] == "activity"
    assert result["agent_icon_svg"] == lucide_svg("activity")
