# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

import pytest

from solstone.apps.timeline import routes

from .conftest import seed_segment

DAY = "20260510"
MONTH = "202605"


@pytest.fixture
def empty_timeline_env(tmp_path: Path, monkeypatch):
    journal = tmp_path / "journal"
    journal.mkdir()
    (journal / "chronicle").mkdir()

    facet_dir = journal / "facets" / "work"
    facet_dir.mkdir(parents=True)
    (facet_dir / "facet.json").write_text(
        json.dumps({"title": "Work", "description": "Test facet"}) + "\n",
        encoding="utf-8",
    )
    (journal / "config").mkdir()
    (journal / "config" / "journal.json").write_text(
        json.dumps(
            {
                "setup": {"completed_at": 1700000000000},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return journal


@pytest.fixture
def empty_client(empty_timeline_env):
    from solstone.convey import create_app

    app = create_app(str(empty_timeline_env))
    app.config.update(TESTING=True)
    return app.test_client()


def _assert_spa_shell(response):
    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_workspace_root_renders(client):
    response = client.get("/app/timeline/", follow_redirects=True)

    _assert_spa_shell(response)
    assert b"/app/timeline/static/data-mock.js" not in response.data


def test_root_redirects_to_today(client, monkeypatch):
    class _FakeDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 5, 21)

    monkeypatch.setattr(routes, "date", _FakeDate)

    response = client.get("/app/timeline/")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/app/timeline/20260521")


def test_year_view_renders_shell(client):
    response = client.get("/app/timeline/year")

    _assert_spa_shell(response)


def test_month_view_renders_shell(client):
    response = client.get("/app/timeline/202605")

    _assert_spa_shell(response)


def test_day_view_renders_shell(client):
    response = client.get("/app/timeline/20260510")

    _assert_spa_shell(response)


def test_day_view_accepts_calendar_invalid(client):
    response = client.get("/app/timeline/20260230")

    _assert_spa_shell(response)


def test_unknown_path_returns_404(client):
    response = client.get("/app/timeline/notaday")
    short_digits = client.get("/app/timeline/2026053")

    assert response.status_code == 404
    assert short_digits.status_code == 404


def test_empty_journal_workspace_has_no_demo_shell(empty_client):
    response = empty_client.get("/app/timeline/", follow_redirects=True)

    _assert_spa_shell(response)
    assert b"Start timeline demo" not in response.data
    assert b"solstone.app/install" not in response.data
    assert b"data-mock.js" not in response.data
    assert b"no observations yet" not in response.data


def test_timeline_static_literal_paths_resolve(client):
    adapter = client.application.url_map.bind("localhost")

    for path in (
        "/app/timeline/static/timeline.css",
        "/app/timeline/static/timeline.js",
    ):
        endpoint, _args = adapter.match(path, method="GET")
        assert endpoint


def test_empty_journal_index_returns_empty_recent_months(empty_client):
    response = empty_client.get("/app/timeline/api/overview")

    assert response.status_code == 200
    payload = response.get_json()
    assert len(payload["months"]) == 12
    for month in payload["months"]:
        assert month["month_top"] == []
        assert month["day_count"] == 0
        assert month["days_with_data"] == []


def test_index_metadata_absent_when_master_minimal(empty_client, empty_timeline_env):
    (empty_timeline_env / "timeline.json").write_text(
        json.dumps({"months": {}}) + "\n", encoding="utf-8"
    )

    response = empty_client.get("/app/timeline/api/overview")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["generated_at"] is None
    assert payload["model"] is None
    assert payload["data_through"] is None


def test_index_shape_and_size(client):
    response = client.get("/app/timeline/api/overview")

    assert response.status_code == 200
    assert len(response.data) < 20 * 1024
    payload = response.get_json()
    assert set(payload) == {
        "now",
        "today",
        "generated_at",
        "model",
        "data_through",
        "months",
        "year_top",
    }
    assert payload["generated_at"] == 1770000000
    assert isinstance(payload["generated_at"], int)
    assert payload["model"] == "test-model"
    assert isinstance(payload["model"], str)
    assert payload["data_through"] == DAY
    assert isinstance(payload["data_through"], str)
    assert len(payload["months"]) == 12
    month = next(m for m in payload["months"] if m["ym"] == MONTH)
    assert month["day_count"] == 1
    assert month["days_with_data"] == [DAY]
    assert month["month_top"][0]["title"] == "Timeline Port"
    assert "days" not in month
    assert [item["month"] for item in payload["year_top"]] == ["202604", "202605"]


def _chronicle_snapshot(journal: Path) -> dict[Path, int]:
    return {
        path: path.stat().st_mtime_ns
        for path in sorted((journal / "chronicle").rglob("*"))
    }


def _expected_date_nav_months() -> dict[str, int]:
    months: dict[str, int] = {}
    for day, count in routes._day_segment_counts().items():
        month = day[:6]
        months[month] = months.get(month, 0) + count
    return months


def test_api_index_reports_nonzero_coverage_and_months(client):
    response = client.get("/app/timeline/api/index")
    assert response.status_code == 200
    body = response.get_json()

    expected_days = routes._day_segment_counts()
    assert body["coverage"] == {
        "start": min(expected_days),
        "end": max(expected_days),
    }
    assert body["months"] == _expected_date_nav_months()


def test_api_index_month_totals_match_api_stats(client):
    response = client.get("/app/timeline/api/index")
    assert response.status_code == 200
    body = response.get_json()

    for month, total in body["months"].items():
        month_response = client.get(f"/app/timeline/api/stats/{month}")
        assert month_response.status_code == 200
        assert total == sum(month_response.get_json().values())


def test_api_index_empty_journal(empty_client):
    response = empty_client.get("/app/timeline/api/index")
    assert response.status_code == 200
    assert response.get_json() == {"coverage": None, "months": {}}


def test_api_index_is_read_only(client, timeline_env):
    before = _chronicle_snapshot(timeline_env)

    response = client.get("/app/timeline/api/index")

    assert response.status_code == 200
    assert _chronicle_snapshot(timeline_env) == before


def test_month_known_shape(client):
    response = client.get(f"/app/timeline/api/month/{MONTH}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ym"] == MONTH
    assert payload["generated_at"] == 1770000000
    assert payload["model"] == "test-model"
    assert payload["day_count"] == 1
    assert payload["days_with_data"] == [DAY]
    assert payload["days"][DAY] == {
        "day": DAY,
        "generated_at": 1770000100,
        "model": "test-day-model",
        "day_top": [
            {
                "title": "Timeline Port",
                "description": "Reviewed the timeline app port.",
                "origin": "20260510/100000_300",
            }
        ],
        "day_rationale": "Fixture day for timeline route tests.",
    }
    assert "hours" not in payload["days"][DAY]
    assert "hours_avail" not in payload["days"][DAY]


def test_month_unknown_returns_404(client):
    response = client.get("/app/timeline/api/month/202501")

    assert response.status_code == 404
    payload = response.get_json()
    assert payload["reason_code"] == "timeline_month_not_found"
    assert payload["detail"] == "no data for 202501"


def test_month_bad_input_returns_400(client):
    response = client.get("/app/timeline/api/month/badinput")

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_month"


def test_day_known_includes_hours_avail(client):
    response = client.get(f"/app/timeline/api/day/{DAY}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["day"] == DAY
    assert payload["generated_at"] == 1770000100
    assert payload["model"] == "test-day-model"
    assert payload["day_top"][0]["title"] == "Timeline Port"
    assert payload["hours"]["10"]["picks"][0]["title"] == "Default Both"

    hour10 = payload["hours_avail"]["10"]["buckets"][0]
    assert hour10 == {
        "minute": 0,
        "best_origin": "20260510/100000_300",
        "has_audio": True,
        "has_screen": True,
        "has_browser": False,
        "browser_origin": None,
        "segment_count": 1,
    }

    hour11 = payload["hours_avail"]["11"]["buckets"][0]
    assert hour11["best_origin"] == "20260510/default/110000_300"
    assert hour11["has_audio"] is True
    assert hour11["has_screen"] is True

    hour12 = payload["hours_avail"]["12"]["buckets"][0]
    assert hour12["best_origin"] == "20260510/default/120000_300"
    assert hour12["has_audio"] is True
    assert hour12["has_screen"] is False

    hour13 = payload["hours_avail"]["13"]["buckets"][0]
    assert hour13["best_origin"] == "20260510/default/130000_300"
    assert hour13["has_audio"] is False
    assert hour13["has_screen"] is True

    assert payload["hours_avail"]["10"]["buckets"][1]["best_origin"] is None


def test_day_browser_only_bucket_and_segment_payload(client, timeline_env):
    corrupt = (
        timeline_env
        / "chronicle"
        / "20260510"
        / "workstation.browser"
        / "140000_300"
        / "browser_corrupt-example-com.jsonl"
    )
    corrupt.write_text("{bad json\n", encoding="utf-8")

    day_response = client.get(f"/app/timeline/api/day/{DAY}")

    assert day_response.status_code == 200
    payload = day_response.get_json()
    bucket = payload["hours_avail"]["14"]["buckets"][0]
    assert bucket["has_browser"] is True
    assert bucket["has_audio"] is False
    assert bucket["has_screen"] is False
    assert bucket["best_origin"] == "20260510/workstation.browser/140000_300"
    assert bucket["browser_origin"] == "20260510/workstation.browser/140000_300"

    segment_response = client.get(
        f"/app/timeline/api/segment/{DAY}/workstation.browser/140000_300"
    )
    assert segment_response.status_code == 200
    segment = segment_response.get_json()
    browser_files = {item["file"]: item for item in segment["browser"]}

    docs = browser_files["browser_docs-example-com.jsonl"]
    assert docs["site_name"] == "Docs"
    assert docs["site"] == "docs.example.com"
    assert docs["title"] == "Timeline Browser Only"
    assert [entry["kind"] for entry in docs["entries"]] == ["snapshot", "change"]
    assert [entry["ts"] for entry in docs["entries"]] == [
        1778443200000,
        1778443215000,
    ]
    assert docs["error"] is None

    corrupt = browser_files["browser_corrupt-example-com.jsonl"]
    assert corrupt["site_name"] == "corrupt.example.com"
    assert corrupt["entries"] == []
    assert corrupt["error"] == "couldn't read this file"


def test_day_mixed_bucket_keeps_best_origin_and_loads_browser_origin(client):
    day_response = client.get(f"/app/timeline/api/day/{DAY}")

    assert day_response.status_code == 200
    payload = day_response.get_json()
    bucket = payload["hours_avail"]["15"]["buckets"][0]
    assert bucket["has_browser"] is True
    assert bucket["has_audio"] is True
    assert bucket["has_screen"] is True
    assert bucket["best_origin"] == "20260510/default/150000_300"
    assert bucket["browser_origin"] == "20260510/workstation.browser/150000_300"

    browser_response = client.get(
        f"/app/timeline/api/segment/{DAY}/workstation.browser/150000_300"
    )
    assert browser_response.status_code == 200
    browser_payload = browser_response.get_json()
    assert browser_payload["browser"][0]["site_name"] == "Mail"
    assert browser_payload["browser"][0]["title"] == "Timeline Mixed Browser"
    assert [entry["kind"] for entry in browser_payload["browser"][0]["entries"]] == [
        "snapshot",
        "change",
    ]


def test_day_bad_input_returns_400(client):
    response = client.get("/app/timeline/api/day/badinput")

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_day"


def test_segment_named_stream_loads_audio_and_screen(client):
    response = client.get(f"/app/timeline/api/segment/{DAY}/default/110000_300")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["day"] == DAY
    assert payload["stream"] == "default"
    assert payload["segment"] == "110000_300"
    assert payload["audio"]["header"]["setting"] == "desk"
    assert len(payload["audio"]["lines"]) == 2
    assert payload["screen"]["filename"] == "desktop.screen.jsonl"
    assert len(payload["screen"]["frames"]) == 2


def test_segment_default_stream_loads_top_level_segment(client):
    response = client.get(f"/app/timeline/api/segment/{DAY}/100000_300")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["stream"] == ""
    assert payload["audio"]["lines"][0]["text"] == "Reviewed timeline data."
    assert payload["screen"]["frames"][0]["analysis"]["primary"] == "code"


def test_segment_unknown_returns_seed_style_payload(client):
    response = client.get(f"/app/timeline/api/segment/{DAY}/unknown/999999_300")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["audio"] is None
    assert payload["screen"] is None
    assert payload["error"].startswith("segment dir not found: ")
    assert payload["error"].endswith("chronicle/20260510/unknown/999999_300")


def test_segment_bad_input_returns_400(client):
    response = client.get(f"/app/timeline/api/segment/{DAY}/default/badseg")

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_path"


def test_stats_returns_seg_counts(empty_client, empty_timeline_env):
    seed_segment(empty_timeline_env, DAY, "090000_300")
    seed_segment(empty_timeline_env, DAY, "091000_300", stream="default")

    response = empty_client.get(f"/app/timeline/api/stats/{MONTH}")

    assert response.status_code == 200
    assert response.get_json() == {DAY: 2}


def test_stats_empty_month(empty_client):
    response = empty_client.get("/app/timeline/api/stats/202501")

    assert response.status_code == 200
    assert response.get_json() == {}


def test_stats_invalid_month(empty_client):
    response = empty_client.get("/app/timeline/api/stats/notamonth")

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "invalid_month"


def test_stats_missing_journal_root(client, monkeypatch):
    monkeypatch.setattr(routes.state, "journal_root", None)

    response = client.get(f"/app/timeline/api/stats/{MONTH}")

    assert response.status_code == 200
    assert response.get_json() == {}


def test_stats_cache_invalidates_on_mtime(empty_client, empty_timeline_env):
    seed_segment(empty_timeline_env, DAY, "090000_300")
    first = empty_client.get(f"/app/timeline/api/stats/{MONTH}")

    assert first.status_code == 200
    assert first.get_json() == {DAY: 1}

    second_segment = seed_segment(empty_timeline_env, DAY, "091000_300")
    bumped = time.time() + 10
    os.utime(second_segment, (bumped, bumped))
    os.utime(second_segment / "marker", (bumped, bumped))

    second = empty_client.get(f"/app/timeline/api/stats/{MONTH}")

    assert second.status_code == 200
    assert second.get_json() == {DAY: 2}


def test_master_cache_invalidates_on_mtime(client, timeline_env):
    first = client.get("/app/timeline/api/overview").get_json()
    first_title = next(m for m in first["months"] if m["ym"] == MONTH)["month_top"][0][
        "title"
    ]
    assert first_title == "Timeline Port"

    timeline_path = timeline_env / "timeline.json"
    data = json.loads(timeline_path.read_text(encoding="utf-8"))
    data["months"][MONTH]["month_top"][0]["title"] = "Updated Timeline"
    timeline_path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    bumped = time.time() + 2
    os.utime(timeline_path, (bumped, bumped))

    second = client.get("/app/timeline/api/overview").get_json()
    second_title = next(m for m in second["months"] if m["ym"] == MONTH)["month_top"][
        0
    ]["title"]
    assert second_title == "Updated Timeline"


def test_segment_lru_eviction(client, timeline_env):
    segment_root = timeline_env / "chronicle" / DAY / "default"
    for idx in range(33):
        seg = f"14{idx:02d}00_300"
        (segment_root / seg).mkdir()
        routes._load_segment(DAY, "default", seg)

    assert len(routes._seg_cache) <= routes._SEG_CACHE_MAX
