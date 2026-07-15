# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from collections import namedtuple
from pathlib import Path

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


@pytest.fixture
def sol_env(tmp_path, monkeypatch):
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
    Env = namedtuple("Env", ["journal", "client"])
    return Env(journal, app.test_client())


def _write_talent_runs(
    journal: Path,
    day: str,
    entries: list[dict[str, object]],
) -> None:
    talents_dir = journal / "talents"
    talents_dir.mkdir(parents=True, exist_ok=True)
    (talents_dir / f"{day}.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries),
        encoding="utf-8",
    )


def _talents_snapshot(journal: Path) -> dict[Path, int]:
    talents_dir = journal / "talents"
    if not talents_dir.exists():
        return {}
    return {path: path.stat().st_mtime_ns for path in sorted(talents_dir.rglob("*"))}


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


def test_api_index_reports_nonzero_coverage_and_months(sol_env):
    _write_talent_runs(
        sol_env.journal,
        "20260304",
        [{"facet": "work"}, {"facet": "work"}, {"facet": "personal"}],
    )
    _write_talent_runs(sol_env.journal, "20260305", [{"name": "unfaceted"}])
    _write_talent_runs(sol_env.journal, "20260401", [{"facet": "work"}])

    response = sol_env.client.get("/app/sol/api/index")

    assert response.status_code == 200
    assert response.get_json() == {
        "coverage": {"start": "20260304", "end": "20260401"},
        "months": {"202603": 4, "202604": 1},
    }


def test_api_index_month_totals_match_api_stats(sol_env):
    _write_talent_runs(
        sol_env.journal,
        "20260304",
        [{"facet": "work"}, {"facet": "personal"}, {"name": "unfaceted"}],
    )

    response = sol_env.client.get("/app/sol/api/index")

    assert response.status_code == 200
    body = response.get_json()
    for month, total in body["months"].items():
        month_response = sol_env.client.get(f"/app/sol/api/stats/{month}")
        assert month_response.status_code == 200
        assert total == sum(
            sum(day.values()) for day in month_response.get_json().values()
        )


def test_api_index_empty_journal(sol_env):
    response = sol_env.client.get("/app/sol/api/index")

    assert response.status_code == 200
    assert response.get_json() == {"coverage": None, "months": {}}


def test_api_index_is_read_only(sol_env):
    _write_talent_runs(sol_env.journal, "20260304", [{"facet": "work"}])
    before = _talents_snapshot(sol_env.journal)

    response = sol_env.client.get("/app/sol/api/index")

    assert response.status_code == 200
    assert _talents_snapshot(sol_env.journal) == before
