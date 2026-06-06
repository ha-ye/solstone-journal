# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import math
import os

from flask import Flask

from solstone.convey.utils import (
    created,
    format_date,
    format_date_short,
    relative_time,
    respond_collection,
    safe_journal_path,
    time_since,
)
from solstone.think.utils import day_path


def _app_context():
    return Flask(__name__).app_context()


def test_format_date():
    assert "2024" not in format_date("20240102")
    assert format_date("bad") == "bad"


def test_format_date_short(monkeypatch):
    from datetime import datetime

    # Mock today as Nov 29, 2025
    class MockDatetime(datetime):
        @classmethod
        def now(cls):
            return datetime(2025, 11, 29, 12, 0, 0)

    monkeypatch.setattr("solstone.convey.utils.datetime", MockDatetime)

    # Test relative dates
    assert format_date_short("20251129") == "Today"
    assert format_date_short("20251128") == "Yesterday"
    assert format_date_short("20251130") == "Tomorrow"

    # Test within past 6 days - should return day name
    assert format_date_short("20251127") == "Thursday"
    assert format_date_short("20251124") == "Monday"

    # Test older date same year - short format without year
    result = format_date_short("20250815")
    assert "Aug" in result
    assert "15" in result
    assert "'" not in result  # No year suffix

    # Test date >6 months ago in different year - should have year suffix
    result = format_date_short("20240301")
    assert "Mar" in result
    assert "'24" in result

    # Test invalid date - should return input unchanged
    assert format_date_short("bad") == "bad"


def test_time_since(monkeypatch):
    monkeypatch.setattr("time.time", lambda: 120)
    assert time_since(60) == "1 minute ago"


def test_relative_time():
    cases = [
        (-1, "0 seconds"),
        (math.inf, "0 seconds"),
        (0, "0 seconds"),
        (1, "1 second"),
        (59, "59 seconds"),
        (60, "1 minute"),
        (119, "1 minute"),
        (120, "2 minutes"),
        (3599, "59 minutes"),
        (3600, "1 hour"),
        (7199, "1 hour"),
        (7200, "2 hours"),
        (86399, "23 hours"),
        (86400, "1 day"),
        (604799, "6 days"),
        (604800, "1 week"),
        (1209600, "2 weeks"),
        (2419199, "3 weeks"),
        (2419200, "1 month"),
        (5183999, "1 month"),
        (5184000, "2 months"),
        (31536000, "12 months"),
    ]
    for seconds, expected in cases:
        assert relative_time(seconds) == expected


def test_list_day_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    from solstone.think.utils import day_dirs

    day_path("20240101")
    day_path("20240103")
    days = sorted(day_dirs().keys())
    assert days == ["20240101", "20240103"]


def test_respond_collection_default_total():
    with _app_context():
        response, status = respond_collection([{"id": 1}, {"id": 2}])
    assert status == 200
    body = response.get_json()
    assert body == {"items": [{"id": 1}, {"id": 2}], "total": 2}
    assert "next_cursor" not in body


def test_respond_collection_explicit_total_omits_cursor():
    with _app_context():
        response, status = respond_collection([{"id": 1}], total=57)
    assert status == 200
    body = response.get_json()
    assert body == {"items": [{"id": 1}], "total": 57}
    assert "next_cursor" not in body


def test_respond_collection_with_cursor():
    with _app_context():
        response, status = respond_collection([], total=99, cursor="20240102")
    assert status == 200
    assert response.get_json() == {
        "items": [],
        "total": 99,
        "next_cursor": "20240102",
    }


def test_created_returns_201_without_location():
    with _app_context():
        response, status = created({"id": "abc", "name": "thing"})
    assert status == 201
    assert response.get_json() == {"id": "abc", "name": "thing"}
    assert "Location" not in response.headers


def test_created_sets_location_header():
    with _app_context():
        response, status = created({"id": "abc"}, location="/app/things/abc")
    assert status == 201
    assert response.headers["Location"] == "/app/things/abc"
    assert response.get_json() == {"id": "abc"}


def _assert_invalid_path_error(error):
    assert error is not None
    response, status = error
    assert status == 400
    assert response.get_json() == {
        "error": "I couldn't use that path.",
        "reason_code": "invalid_path",
        "detail": "",
    }


def test_safe_journal_path_accepts_contained_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    path, error = safe_journal_path("facets/work/facet.json")

    assert error is None
    assert path == tmp_path / "facets" / "work" / "facet.json"
    assert path.is_absolute()


def test_safe_journal_path_rejects_invalid_relpaths(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    with _app_context():
        for relpath in ("..", "../escape", "/etc/passwd", "a\\b", ""):
            path, error = safe_journal_path(relpath)

            assert path is None
            _assert_invalid_path_error(error)


def test_safe_journal_path_rejects_symlink_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    outside = tmp_path.parent / f"{tmp_path.name}_outside"
    outside.mkdir()
    os.symlink(outside, tmp_path / "out")

    with _app_context():
        path, error = safe_journal_path("out/secret.txt")

    assert path is None
    _assert_invalid_path_error(error)
