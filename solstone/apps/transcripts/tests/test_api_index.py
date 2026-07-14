# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import shutil
from pathlib import Path
from typing import Any

from solstone.apps.transcripts import routes


def _stats_snapshot(journal: Path) -> dict[Path, int]:
    return {
        path: path.stat().st_mtime_ns
        for path in sorted((journal / "chronicle").glob("*/stats.json"))
    }


def _assert_stats_snapshot_unchanged(journal: Path, before: dict[Path, int]) -> None:
    assert _stats_snapshot(journal) == before


def _expected_nonzero_days(journal: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for day_dir in sorted((journal / "chronicle").iterdir()):
        if not day_dir.is_dir():
            continue
        count = routes._day_range_count(day_dir.name, day_dir)
        if count > 0:
            counts[day_dir.name] = count
    return counts


def test_api_index_reports_nonzero_coverage_and_months(
    client: Any,
    journal_copy: Path,
) -> None:
    response = client.get("/app/transcripts/api/index")
    assert response.status_code == 200
    body = response.get_json()

    expected_days = _expected_nonzero_days(journal_copy)
    assert min(expected_days) == "20240101"
    assert max(expected_days) == "20260520"
    assert body["coverage"] == {
        "start": min(expected_days),
        "end": max(expected_days),
    }

    expected_months: dict[str, int] = {}
    for day, count in expected_days.items():
        month = day[:6]
        expected_months[month] = expected_months.get(month, 0) + count
    assert body["months"] == expected_months


def test_api_index_month_totals_match_api_stats(client: Any) -> None:
    response = client.get("/app/transcripts/api/index")
    assert response.status_code == 200
    body = response.get_json()

    for month, total in body["months"].items():
        month_response = client.get(f"/app/transcripts/api/stats/{month}")
        assert month_response.status_code == 200
        assert total == sum(month_response.get_json().values())


def test_api_index_empty_journal(client: Any, journal_copy: Path) -> None:
    chronicle = journal_copy / "chronicle"
    for child in chronicle.iterdir():
        if child.is_dir():
            shutil.rmtree(child)

    response = client.get("/app/transcripts/api/index")
    assert response.status_code == 200
    assert response.get_json() == {"coverage": None, "months": {}}


def test_api_index_is_read_only(client: Any, journal_copy: Path) -> None:
    before = _stats_snapshot(journal_copy)

    response = client.get("/app/transcripts/api/index")

    assert response.status_code == 200
    _assert_stats_snapshot_unchanged(journal_copy, before)
