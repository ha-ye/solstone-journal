# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
import requests
from typer.testing import CliRunner

from solstone.think.convey_client import ConveyClient
from solstone.think.surfaces import health as health_surface
from solstone.think.tools.health import app
from tests._baseline_harness import make_logged_in_test_client
from tests.test_surfaces_health import (
    _clear_readiness_snapshot,
    _minimal_facet_tree,
    _segment_backlog,
    _utc_dt,
)


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return tmp_path


@pytest.fixture
def runner(journal, monkeypatch):
    client = ConveyClient(session=make_logged_in_test_client(journal), base_url="")
    monkeypatch.setattr("solstone.think.tools.health.get_client", lambda: client)
    return CliRunner()


class _Response:
    def __init__(self, body: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = json.dumps(body)


class _TextResponse:
    status_code = 200
    text = "not json"


class _CaptureSession:
    def __init__(self, body: object) -> None:
        self.body = body
        self.urls: list[str] = []

    def get(self, url: str):
        parsed = urlsplit(url)
        self.urls.append(parsed.path + (f"?{parsed.query}" if parsed.query else ""))
        return _Response(self.body)


def _report() -> dict[str, object]:
    return {
        "generated_at": 1,
        "range": ["20260410", "20260410"],
        "facets": ["work"],
        "capture_health": {
            "hours_with_capture": 0,
            "hours_total": 24,
            "coverage_ratio": None,
            "facets_with_recent_capture": [],
            "facets_silent_24h": ["work"],
            "last_segment_at": None,
        },
        "synthesis_health": {
            "activities_count": 0,
            "activities_with_participation": 0,
            "activities_with_story": 0,
            "activities_user_edited": 0,
            "activities_anticipated_unfilled": 0,
            "talent_run_failures_24h": None,
            "talent_degraded_outputs_24h": None,
            "indexer_last_rebuild_at": None,
        },
        "consumer_signal": {
            "ledger_open_items_total": 0,
            "ledger_stale_items_count": 0,
            "profile_entities_total": 0,
        },
        "segment_backlog": {"not_thought": 0, "days_with_backlog": 0, "errors": []},
        "notes": [],
        "provider_readiness": {
            "summary": {
                "status": "ready",
                "severity": "ok",
                "active_groups": 0,
                "blocked_count": 0,
            },
            "interfaces": {},
            "groups": [],
        },
    }


def _freeze_health_surface(journal, monkeypatch) -> None:
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
    _minimal_facet_tree(journal)


def test_health_request_mapping(monkeypatch):
    session = _CaptureSession(_report())
    client = ConveyClient(session=session, base_url="")
    monkeypatch.setattr("solstone.think.tools.health.get_client", lambda: client)
    runner = CliRunner()

    summary = runner.invoke(app, ["summary", "--day", "20260410", "--json"])
    full = runner.invoke(app, ["full", "--day", "20260411", "--json"])
    range_result = runner.invoke(
        app,
        [
            "for-range",
            "--day-from",
            "20260404",
            "--day-to",
            "20260410",
            "--json",
        ],
    )

    assert summary.exit_code == 0
    assert full.exit_code == 0
    assert range_result.exit_code == 0
    assert session.urls == [
        "/api/health/summary?day=20260410",
        "/api/health/full?day=20260411",
        "/api/health/range?day_from=20260404&day_to=20260410",
    ]


def test_health_null_fields_render_dash(runner, journal, monkeypatch):
    _freeze_health_surface(journal, monkeypatch)

    result = runner.invoke(app, ["summary", "--day", "20260410"])

    assert result.exit_code == 0
    assert "  coverage_ratio: —\n" in result.stdout
    assert "  last_segment_at: —\n" in result.stdout
    assert "  talent_run_failures_24h: —\n" in result.stdout
    assert "None" not in result.stdout


def test_health_range_validation_detail_to_stderr(runner):
    result = runner.invoke(app, ["for-range", "--day-from", "20260404"])

    assert result.exit_code == 1
    assert "both endpoints or neither" in result.stderr
    assert result.stdout == ""


def test_health_malformed_response_exits_without_stdout(monkeypatch):
    class BadSession:
        def get(self, _url: str):
            return _TextResponse()

    client = ConveyClient(session=BadSession(), base_url="")
    monkeypatch.setattr("solstone.think.tools.health.get_client", lambda: client)

    result = CliRunner().invoke(app, ["summary", "--json"])

    assert result.exit_code == 1
    assert "I couldn't read the journal response." in result.stderr
    assert result.stdout == ""


def test_health_convey_down_prints_require_solstone_message(journal, monkeypatch):
    class DownSession:
        def get(self, _url):
            raise requests.exceptions.ConnectionError()

        def post(self, _url, json=None):
            raise requests.exceptions.ConnectionError()

    client = ConveyClient(session=DownSession(), base_url="http://localhost:5015")
    monkeypatch.setattr("solstone.think.tools.health.get_client", lambda: client)
    monkeypatch.delenv("SOL_SKIP_SUPERVISOR_CHECK", raising=False)
    monkeypatch.delenv("SOL_SUPERVISOR_SPAWNED", raising=False)

    result = CliRunner().invoke(app, ["summary"])

    assert result.exit_code == 1
    assert (
        result.stderr
        == "sol: solstone isn't running. Start it with 'journal up' and retry.\n"
    )
    assert result.stdout == ""
