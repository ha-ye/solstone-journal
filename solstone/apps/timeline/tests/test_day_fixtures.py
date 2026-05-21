# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
from pathlib import Path

from solstone.apps.timeline import routes
from solstone.convey import state

DAY = "20260510"
FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(tmp_path: Path, monkeypatch, name: str) -> Path:
    src = FIXTURES / name / "journal"
    journal = tmp_path / "journal"
    shutil.copytree(src, journal)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr(state, "journal_root", str(journal))
    return journal


def _any_segment_count(payload: dict) -> bool:
    return any(
        bucket["segment_count"] > 0
        for hour in payload["hours_avail"].values()
        for bucket in hour["buckets"]
    )


def test_day_fixture_empty_no_dir_returns_empty_shape(tmp_path, monkeypatch):
    _load_fixture(tmp_path, monkeypatch, "empty_no_dir")

    payload = routes._build_day(DAY)

    assert payload["day_top"] == []
    assert payload["hours"] == {}
    assert payload["hours_avail"] == {}


def test_day_fixture_segments_no_rollup_returns_availability(tmp_path, monkeypatch):
    _load_fixture(tmp_path, monkeypatch, "empty_segments_no_rollup")

    payload = routes._build_day(DAY)

    assert payload["day_top"] == []
    assert payload["hours"] == {}
    assert _any_segment_count(payload)


def test_day_fixture_day_top_dedup_preserves_raw_route_shape(tmp_path, monkeypatch):
    _load_fixture(tmp_path, monkeypatch, "day_top_dedup")

    payload = routes._build_day(DAY)

    assert len(payload["day_top"]) == 1
    assert len(payload["hours"]["10"]["picks"]) == 2


def test_day_fixture_day_top_empty_array_keeps_hours(tmp_path, monkeypatch):
    _load_fixture(tmp_path, monkeypatch, "day_top_empty_array")

    payload = routes._build_day(DAY)

    assert payload["day_top"] == []
    assert payload["hours"]["10"]["picks"] == []


def test_day_fixture_hour_picks_empty_keeps_available_hour(tmp_path, monkeypatch):
    _load_fixture(tmp_path, monkeypatch, "hour_picks_empty")

    payload = routes._build_day(DAY)

    assert payload["hours"]["09"]["picks"][0]["title"] == "Morning"
    assert payload["hours"]["10"]["picks"] == []
    assert payload["hours"]["11"]["picks"][0]["title"] == "Late Morning"
    assert any(
        bucket["segment_count"] > 0
        for bucket in payload["hours_avail"]["10"]["buckets"]
    )
