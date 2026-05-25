# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from datetime import date as real_date
from datetime import timedelta

import pytest

from solstone.apps.tokens import copy as tokens_copy
from solstone.apps.tokens import routes as token_routes


def _day(offset: int, today: real_date | None = None) -> str:
    base = today or real_date.today()
    return (base - timedelta(days=offset)).strftime("%Y%m%d")


def _entry(model: str, total_tokens: int) -> dict:
    return {
        "timestamp": 1772676000.0,
        "model": model,
        "context": "think.cortex.flow:42",
        "usage": {
            "input_tokens": total_tokens // 2,
            "output_tokens": total_tokens // 2,
            "total_tokens": total_tokens,
        },
    }


def _patch_token_cost(monkeypatch):
    def calc_cost(entry: dict) -> dict:
        total_tokens = entry.get("usage", {}).get("total_tokens", 0) or 0
        return {
            "total_cost": total_tokens / 10000,
            "input_cost": 0.0,
            "output_cost": total_tokens / 10000,
            "currency": "USD",
        }

    monkeypatch.setattr(token_routes, "calc_token_cost", calc_cost)


def test_api_daily_happy_path(tokens_env, monkeypatch):
    token_logs = {
        _day(2): [_entry("gpt-5", 1000)],
        _day(1): [
            _entry("gemini-2.5-flash", 2000),
            _entry("claude-sonnet-4-5", 3000),
        ],
        _day(0): [_entry("claude-sonnet-4-5", 4000)],
    }
    env = tokens_env(token_logs)
    _patch_token_cost(monkeypatch)

    response = env.client.get("/app/tokens/api/daily?days=14")

    assert response.status_code == 200
    rows = response.get_json()
    assert len(rows) == 14
    assert all(set(row) == {"day", "cost", "tokens"} for row in rows)
    assert sorted(rows, key=lambda row: row["day"]) == rows

    by_day = {row["day"]: row for row in rows}
    assert by_day[_day(2)]["tokens"] == 1000
    assert by_day[_day(2)]["cost"] == pytest.approx(0.1)
    assert by_day[_day(1)]["tokens"] == 5000
    assert by_day[_day(1)]["cost"] == pytest.approx(0.5)
    assert by_day[_day(0)]["tokens"] == 4000
    assert by_day[_day(0)]["cost"] == pytest.approx(0.4)

    zero_rows = [row for row in rows if row["day"] not in token_logs]
    assert len(zero_rows) == 11
    assert all(row["cost"] == 0.0 and row["tokens"] == 0 for row in zero_rows)


def test_api_daily_zero_fills_missing_days(tokens_env, monkeypatch):
    token_logs = {
        _day(5): [_entry("gpt-5", 1000)],
        _day(0): [_entry("gemini-2.5-flash", 2000)],
    }
    env = tokens_env(token_logs)
    _patch_token_cost(monkeypatch)

    response = env.client.get("/app/tokens/api/daily?days=7")

    assert response.status_code == 200
    rows = response.get_json()
    assert [row["day"] for row in rows] == [_day(offset) for offset in range(6, -1, -1)]
    by_day = {row["day"]: row for row in rows}
    assert by_day[_day(5)]["tokens"] == 1000
    assert by_day[_day(0)]["tokens"] == 2000
    for day in {_day(offset) for offset in range(6, -1, -1)} - set(token_logs):
        assert by_day[day] == {"day": day, "cost": 0.0, "tokens": 0}


def test_api_daily_rejects_invalid_days(tokens_env):
    env = tokens_env({})

    for days in ["0", "-1", "91", "abc"]:
        response = env.client.get(f"/app/tokens/api/daily?days={days}")
        assert response.status_code == 400


def test_api_daily_cross_month_boundary(tokens_env, monkeypatch):
    fixed_today = real_date(2026, 3, 4)

    class FakeDate(real_date):
        @staticmethod
        def today():
            return fixed_today

    monkeypatch.setattr(token_routes, "date", FakeDate)
    token_logs = {
        _day(offset, fixed_today): [_entry("claude-sonnet-4-5", (7 - offset) * 1000)]
        for offset in range(6, -1, -1)
    }
    env = tokens_env(token_logs)
    _patch_token_cost(monkeypatch)

    response = env.client.get("/app/tokens/api/daily?days=7")

    assert response.status_code == 200
    rows = response.get_json()
    assert [row["day"] for row in rows] == [
        "20260226",
        "20260227",
        "20260228",
        "20260301",
        "20260302",
        "20260303",
        "20260304",
    ]
    expected_rate = sum(row["cost"] for row in rows) / 7
    assert expected_rate == pytest.approx(0.4)


def test_tokens_page_renders_copy_payload_and_static_labels(tokens_env):
    env = tokens_env({})

    response = env.client.get("/app/tokens/20260304")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert tokens_copy.TOKENS_TILE_COST_LABEL in html
    assert tokens_copy.TOKENS_TILE_TOKENS_LABEL in html
    assert tokens_copy.TOKENS_TILE_RUN_RATE_LABEL in html
    assert tokens_copy.TOKENS_TILE_TOP_DRIVER_LABEL in html
    assert "window.TOKENS_COPY = {" in html


def test_tokens_page_renders_collapsed_details_for_all_breakdowns(tokens_env):
    env = tokens_env({})

    response = env.client.get("/app/tokens/20260304")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    tags = re.findall(r'<details[^>]*data-disclosure="([\w-]+)"[^>]*>', html)
    assert set(tags) == {"provider", "model", "token-type", "context", "segment"}
    assert len(tags) == 5
    detail_tags = re.findall(r'<details[^>]*data-disclosure="[\w-]+"[^>]*>', html)
    assert all(" open" not in tag for tag in detail_tags)
