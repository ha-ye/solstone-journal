# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import sys
from pathlib import Path

from solstone.convey import create_app

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests._baseline_harness import make_test_client


def test_activities_day_serves_spa_shell(activities_env):
    journal, _facet, day, _day_path = activities_env(None)
    client = create_app(journal=str(journal)).test_client()

    response = client.get(f"/app/activities/{day}")

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_activities_index_redirects_to_spa_shell(activities_env):
    journal, _facet, _day, _day_path = activities_env(None)
    client = create_app(journal=str(journal)).test_client()

    response = client.get("/app/activities/", follow_redirects=True)

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_activities_day_guard_still_404s(activities_env):
    journal, _facet, _day, _day_path = activities_env(None)
    client = create_app(journal=str(journal)).test_client()

    response = client.get("/app/activities/notaday")

    assert response.status_code == 404


def _facet_snapshot(journal: Path) -> dict[Path, int]:
    facets = journal / "facets"
    if not facets.exists():
        return {}
    return {path: path.stat().st_mtime_ns for path in sorted(facets.rglob("*"))}


def test_api_index_reports_nonzero_coverage_and_months(activities_env):
    journal, _facet, _day, _day_path = activities_env(
        [{"id": "a1", "activity": "coding", "title": "Coding"}],
        day="20240101",
        facet="work",
    )
    activities_env(
        [{"id": "a2", "activity": "meeting", "title": "Meeting"}],
        day="20240101",
        facet="personal",
    )
    activities_env(
        [{"id": "a3", "activity": "coding", "title": "Later"}],
        day="20240203",
        facet="work",
    )
    client = create_app(journal=str(journal)).test_client()

    response = client.get("/app/activities/api/index")

    assert response.status_code == 200
    assert response.get_json() == {
        "coverage": {"start": "20240101", "end": "20240203"},
        "months": {"202401": 2, "202402": 1},
    }


def test_api_index_month_totals_match_api_stats(activities_env):
    journal, _facet, _day, _day_path = activities_env(
        [
            {"id": "a1", "activity": "coding", "title": "Coding"},
            {"id": "a2", "activity": "meeting", "title": "Meeting"},
        ],
        day="20240101",
        facet="work",
    )
    client = create_app(journal=str(journal)).test_client()

    response = client.get("/app/activities/api/index")

    assert response.status_code == 200
    body = response.get_json()
    for month, total in body["months"].items():
        month_response = client.get(f"/app/activities/api/stats/{month}")
        assert month_response.status_code == 200
        assert total == sum(
            sum(day.values()) for day in month_response.get_json().values()
        )


def test_api_index_empty_journal(activities_env):
    journal, _facet, _day, _day_path = activities_env(None)
    client = create_app(journal=str(journal)).test_client()

    response = client.get("/app/activities/api/index")

    assert response.status_code == 200
    assert response.get_json() == {"coverage": None, "months": {}}


def test_api_index_skips_invalid_activity_month(activities_env):
    journal, facet, _day, _day_path = activities_env(None)
    stray_file = journal / "facets" / facet / "activities" / "20269901.jsonl"
    stray_file.write_text(
        '{"id": "stray", "activity": "coding", "title": "Stray"}\n',
        encoding="utf-8",
    )
    client = create_app(journal=str(journal)).test_client()

    response = client.get("/app/activities/api/index")

    assert response.status_code == 200
    assert "202699" not in response.get_json()["months"]


def test_api_index_is_read_only(activities_env):
    journal, _facet, _day, _day_path = activities_env(
        [{"id": "a1", "activity": "coding", "title": "Coding"}]
    )
    before = _facet_snapshot(journal)
    client = create_app(journal=str(journal)).test_client()

    response = client.get("/app/activities/api/index")

    assert response.status_code == 200
    assert _facet_snapshot(journal) == before


def test_activities_colors_literal_path_resolves(activities_env):
    journal, _facet, _day, _day_path = activities_env(None)
    app = create_app(journal=str(journal))
    adapter = app.url_map.bind("localhost")

    endpoint, _args = adapter.match("/static/colors.js", method="GET")

    assert endpoint


def test_day_activities_returns_collection_envelope(activities_env):
    journal, _facet, day, _day_path = activities_env(
        [
            {
                "id": "coding_090000_300",
                "activity": "coding",
                "title": "Focused coding",
                "segments": ["090000_300"],
                "created_at": 1,
            }
        ]
    )
    client = create_app(journal=str(journal)).test_client()

    response = client.get(f"/app/activities/api/day/{day}/activities")

    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {"items", "total"}
    assert len(payload["items"]) == 1
    assert payload["total"] == len(payload["items"])


def test_day_activities_empty_day_returns_empty_envelope(activities_env):
    journal, _facet, day, _day_path = activities_env(None)
    client = create_app(journal=str(journal)).test_client()

    response = client.get(f"/app/activities/api/day/{day}/activities")

    assert response.status_code == 200
    assert response.get_json() == {"items": [], "total": 0}


def test_create_record_rejects_empty_title(activities_env):
    journal, facet, day, _day_path = activities_env(None)
    client = make_test_client(journal)

    response = client.post(
        f"/app/activities/api/day/{day}/records?facet={facet}",
        json={"title": "", "activity": "meeting"},
    )

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "activity_invalid"


def test_create_record_rejects_invalid_source(activities_env):
    journal, facet, day, _day_path = activities_env(None)
    client = make_test_client(journal)

    response = client.post(
        f"/app/activities/api/day/{day}/records?facet={facet}",
        json={"title": "Valid", "activity": "meeting", "source": "calendar"},
    )

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "activity_invalid"
