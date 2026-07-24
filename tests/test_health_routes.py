# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from solstone.convey import create_app
from solstone.convey import health as convey_health
from solstone.think.brain_health import HEADLINES
from solstone.think.surfaces import health as health_surface
from tests._baseline_harness import make_test_client
from tests.test_surfaces_health import (
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
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "journal.json").write_text(
        json.dumps({"setup": {"completed_at": 1}}),
        encoding="utf-8",
    )


def _configure_unset_journal(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def _brain_snapshot() -> dict[str, object]:
    return {
        "state": "ready",
        "headline": HEADLINES["ready"],
        "reason_code": None,
        "reason_text": None,
        "failing_component": None,
        "action": None,
        "identity": {"lane": "cloud", "provider": "google", "model": "gemini"},
        "evidence": {
            "observed_at": "2026-04-10T12:00:00Z",
            "age_seconds": 60,
            "age_text": "1m",
        },
        "components": {
            "generate": {
                "status": "ready",
                "reason_code": None,
                "reason_text": None,
                "observed_at": "2026-04-10T12:00:00Z",
            },
            "cogitate": {
                "status": "ready",
                "reason_code": None,
                "reason_text": None,
                "observed_at": "2026-04-10T12:00:00Z",
            },
        },
        "progressing": False,
    }


def _freeze_health_surface(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(health_surface, "_resolve_now", lambda: _utc_dt("20260410"))
    monkeypatch.setattr(
        health_surface,
        "build_brain_snapshot",
        lambda *_args, **_kwargs: _brain_snapshot(),
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
    client = make_test_client(tmp_path)

    response = client.get(f"{PREFIX}/summary?day=20260410")

    assert response.status_code == 200
    assert {
        "range",
        "capture_health",
        "synthesis_health",
        "consumer_signal",
        "brain_health",
    } <= response.get_json().keys()


def test_summary_and_full_identical_for_same_day(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _freeze_health_surface(tmp_path, monkeypatch)
    client = make_test_client(tmp_path)

    summary_response = client.get(f"{PREFIX}/summary?day=20260410")
    full_response = client.get(f"{PREFIX}/full?day=20260410")

    assert summary_response.status_code == 200
    assert full_response.status_code == 200
    assert summary_response.get_json() == full_response.get_json()


def test_none_field_survives_as_json_null(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _freeze_health_surface(tmp_path, monkeypatch)
    client = make_test_client(tmp_path)

    response = client.get(f"{PREFIX}/summary?day=20260410")

    assert response.status_code == 200
    assert response.get_json()["capture_health"]["coverage_ratio"] is None


def test_malformed_day_returns_400(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    client = make_test_client(tmp_path)

    data = _assert_error(client.get(f"{PREFIX}/summary?day=notaday"), 400)

    assert data["reason_code"] == "invalid_request_value"


def test_range_valid_window(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _freeze_health_surface(tmp_path, monkeypatch)
    client = make_test_client(tmp_path)

    response = client.get(f"{PREFIX}/range?day_from=20260404&day_to=20260410")

    assert response.status_code == 200
    assert response.get_json()["range"] == ["20260404", "20260410"]


def test_range_omit_both_uses_default_window(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    _freeze_health_surface(tmp_path, monkeypatch)
    client = make_test_client(tmp_path)

    response = client.get(f"{PREFIX}/range")

    assert response.status_code == 200
    assert response.get_json()["range"] == ["20260404", "20260410"]


def test_range_only_one_endpoint_returns_400(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    client = make_test_client(tmp_path)

    data = _assert_error(client.get(f"{PREFIX}/range?day_from=20260404"), 400)

    assert data["reason_code"] == "invalid_request_value"
    assert "both endpoints or neither" in data["detail"]


def test_range_inverted_returns_400_distinct_detail(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    client = make_test_client(tmp_path)

    data = _assert_error(
        client.get(f"{PREFIX}/range?day_from=20260410&day_to=20260404"),
        400,
    )

    assert data["reason_code"] == "invalid_request_value"
    assert "day_from must be <= day_to" in data["detail"]


def test_pipeline_route_preserves_summary_key_order(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    summary = {
        "day": "20260410",
        "generated_at": 1,
        "status": "healthy",
        "anomalies": [{"kind": "ordered", "error": "kept"}],
        "runs": {
            "segment": {"count": 1, "duration_ms_total": 20},
            "daily": {"count": 0, "duration_ms_total": 0},
        },
        "talents": {"dispatched": 1, "completed": 1},
    }
    monkeypatch.setattr(
        convey_health,
        "summarize_pipeline_day",
        lambda day: summary | {"day": day},
    )
    client = make_test_client(tmp_path)

    response = client.get(f"{PREFIX}/pipeline?day=20260410")

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    raw = response.get_data(as_text=True)
    assert raw.startswith(
        '{"day":"20260410","generated_at":1,"status":"healthy","anomalies":'
    )
    assert list(json.loads(raw)) == [
        "day",
        "generated_at",
        "status",
        "anomalies",
        "runs",
        "talents",
    ]


def test_pipeline_route_missing_day_returns_existing_reason(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    client = make_test_client(tmp_path)

    data = _assert_error(client.get(f"{PREFIX}/pipeline"), 400)

    assert data["reason_code"] == "missing_required_field"
    assert data["detail"] == "day is required"


def test_pipeline_route_invalid_day_returns_existing_reason(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)
    client = make_test_client(tmp_path)

    data = _assert_error(client.get(f"{PREFIX}/pipeline?day=20260230"), 400)

    assert data["reason_code"] == "invalid_request_value"
    assert "YYYYMMDD" in data["detail"]


def test_pipeline_route_calc_failure_returns_existing_reason(tmp_path, monkeypatch):
    _configure_journal(tmp_path, monkeypatch)

    def fail(_day: str) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(convey_health, "summarize_pipeline_day", fail)
    client = make_test_client(tmp_path)

    data = _assert_error(client.get(f"{PREFIX}/pipeline?day=20260410"), 500)

    assert data["reason_code"] == "health_report_failed"
    assert data["detail"] == "health report unavailable"


def test_health_redirects_to_init_when_setup_incomplete(tmp_path, monkeypatch):
    _configure_unset_journal(tmp_path, monkeypatch)
    app = create_app(journal=str(tmp_path))
    app.config["TESTING"] = True
    client = app.test_client()

    response = client.get(f"{PREFIX}/summary")

    assert response.status_code == 302
    assert "/init" in response.headers["Location"]
