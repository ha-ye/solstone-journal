# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.convey import create_app
from solstone.think.surfaces import health as health_surface
from tests._baseline_harness import make_logged_in_test_client
from tests.test_surfaces_health import (
    _clear_readiness_snapshot,
    _minimal_facet_tree,
    _segment_backlog,
    _utc_dt,
)

PREFIX = "/api/health"


def _assert_error(response, status: int) -> dict:
    assert response.status_code == status
    data = response.get_json()
    assert data["reason_code"]
    if status == 400:
        assert data["detail"]
    return data


def _configure_journal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def _freeze_health_surface(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(health_surface, "_resolve_now", lambda: _utc_dt("20260410"))
    monkeypatch.setattr(
        health_surface,
        "build_readiness_snapshot",
        lambda: _clear_readiness_snapshot(),
    )
    monkeypatch.setattr(
        health_surface,
        "read_segment_backlog",
        lambda: _segment_backlog({}),
    )
    _minimal_facet_tree(tmp_path)


def test_summary_returns_report_shape(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _freeze_health_surface(tmp_path, monkeypatch)
    client = make_logged_in_test_client(tmp_path)

    response = client.get(f"{PREFIX}/summary?day=20260410")

    assert response.status_code == 200
    assert {
        "range",
        "capture_health",
        "synthesis_health",
        "consumer_signal",
        "provider_readiness",
    } <= response.get_json().keys()


def test_summary_and_full_identical_for_same_day(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _freeze_health_surface(tmp_path, monkeypatch)
    client = make_logged_in_test_client(tmp_path)

    summary_response = client.get(f"{PREFIX}/summary?day=20260410")
    full_response = client.get(f"{PREFIX}/full?day=20260410")

    assert summary_response.status_code == 200
    assert full_response.status_code == 200
    assert summary_response.get_json() == full_response.get_json()


def test_none_field_survives_as_json_null(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _freeze_health_surface(tmp_path, monkeypatch)
    client = make_logged_in_test_client(tmp_path)

    response = client.get(f"{PREFIX}/summary?day=20260410")

    assert response.status_code == 200
    assert response.get_json()["capture_health"]["coverage_ratio"] is None


def test_malformed_day_returns_400(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    client = make_logged_in_test_client(tmp_path)

    data = _assert_error(client.get(f"{PREFIX}/summary?day=notaday"), 400)

    assert data["reason_code"] == "invalid_request_value"


def test_range_valid_window(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _freeze_health_surface(tmp_path, monkeypatch)
    client = make_logged_in_test_client(tmp_path)

    response = client.get(f"{PREFIX}/range?day_from=20260404&day_to=20260410")

    assert response.status_code == 200
    assert response.get_json()["range"] == ["20260404", "20260410"]


def test_range_omit_both_uses_default_window(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _freeze_health_surface(tmp_path, monkeypatch)
    client = make_logged_in_test_client(tmp_path)

    response = client.get(f"{PREFIX}/range")

    assert response.status_code == 200
    assert response.get_json()["range"] == ["20260404", "20260410"]


def test_range_only_one_endpoint_returns_400(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    client = make_logged_in_test_client(tmp_path)

    data = _assert_error(client.get(f"{PREFIX}/range?day_from=20260404"), 400)

    assert data["reason_code"] == "invalid_request_value"
    assert "both endpoints or neither" in data["detail"]


def test_range_inverted_returns_400_distinct_detail(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    client = make_logged_in_test_client(tmp_path)

    data = _assert_error(
        client.get(f"{PREFIX}/range?day_from=20260410&day_to=20260404"),
        400,
    )

    assert data["reason_code"] == "invalid_request_value"
    assert "day_from must be <= day_to" in data["detail"]


def test_health_requires_login(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    app = create_app(journal=str(tmp_path))
    app.config["TESTING"] = True
    client = app.test_client()

    response = client.get(f"{PREFIX}/summary")

    assert response.status_code == 302
