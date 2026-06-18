# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest

from solstone.convey import create_app


@pytest.fixture
def stats_client(tmp_path, monkeypatch):
    def _create():
        journal = tmp_path / "journal"
        config_dir = journal / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
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
        return journal, app.test_client()

    return _create


def test_stats_read_failure_returns_500_envelope(stats_client):
    journal, client = stats_client()
    (journal / "stats.json").write_text("{ not valid json", encoding="utf-8")

    response = client.get("/app/stats/api/stats")

    assert response.status_code == 500
    body = response.get_json()
    assert body["reason_code"] == "file_read_failed"
    assert "error" in body
    assert "detail" in body
    assert "generators" not in body


def test_stats_success_returns_payload_with_generators(stats_client):
    stats = {"days": {}, "totals": {}}
    journal, client = stats_client()
    (journal / "stats.json").write_text(json.dumps(stats), encoding="utf-8")

    response = client.get("/app/stats/api/stats")

    assert response.status_code == 200
    body = response.get_json()
    assert "stats" in body
    assert "generators" in body
    assert "error" not in body
    assert body["stats"] == stats
