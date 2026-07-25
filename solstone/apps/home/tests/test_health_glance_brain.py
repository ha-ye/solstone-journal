# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from solstone.apps.home.health_glance import build_health_glance
from solstone.think.brain_health import HEADLINES, build_brain_snapshot
from solstone.think.journal_io import atomic_replace
from solstone.think.providers import brain_state as brain_state_module
from solstone.think.providers.brain_state import (
    CHECKING_TTL,
    DEFAULT_READY_EVIDENCE_TTL,
    begin_brain_refresh,
    finish_brain_refresh,
)
from solstone.think.providers.runtime_health import runtime_health_path
from tests.helpers.health_glance import healthy_backlog_source

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)
BUNDLED_RUNTIME_FINGERPRINT = "b" * 64


def _write_config(journal: Path, config: dict[str, Any]) -> None:
    path = journal / "config" / "journal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding="utf-8")


def _cloud_config() -> dict[str, Any]:
    return {
        "providers": {"active": {"provider": "openai", "model": "gpt-5"}},
        "env": {"OPENAI_API_KEY": "config-secret"},
    }


def _bundled_config() -> dict[str, Any]:
    return {
        "providers": {"active": {"provider": "local", "model": "local/bundled"}},
        "env": {},
    }


def _component(now: datetime = NOW) -> dict[str, str]:
    return {
        "status": "ok",
        "observed_at": now.isoformat(),
        "expires_at": (now + DEFAULT_READY_EVIDENCE_TTL).isoformat(),
    }


def _ready_outcome(now: datetime = NOW) -> dict[str, dict[str, str]]:
    return {
        "configuration": _component(now),
        "lane_prerequisites": _component(now),
        "generate": _component(now),
        "cogitate": _component(now),
    }


def _active_capture() -> dict[str, Any]:
    return {
        "status": "active",
        "observers": [{"name": "fedora", "status": "active"}],
    }


def _no_observers_capture() -> dict[str, Any]:
    return {"status": "no_observers", "observers": []}


def _unavailable_capture() -> dict[str, Any]:
    return {"status": "unknown", "observers": []}


def _write_ready_record(journal: Path, config: dict[str, Any]) -> None:
    _write_config(journal, config)
    permit = begin_brain_refresh(NOW, run_id="ready", journal_path=journal)
    assert permit is not None
    finish_brain_refresh(permit, _ready_outcome(), NOW, journal_path=journal)


def _write_runtime_health_record(journal: Path, *, phase: str) -> None:
    record = {
        "schema_version": 1,
        "provider": "local",
        "revision": 1,
        "phase": phase,
        "reason_code": None,
        "detail": {},
        "desired_fingerprint_sha256": BUNDLED_RUNTIME_FINGERPRINT,
        "incarnation": None,
        "generation": 1,
        "attempt": 0,
        "process": None,
        "updated_at": NOW.isoformat(),
        "display_deadline_at": None,
        "owner": None,
    }
    path = runtime_health_path("local", journal_path=journal)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace(path, json.dumps(record), mode=0o600)


def _assert_checking_row(result: dict[str, Any]) -> None:
    assert result["verdict"] == "checking"
    assert result["severity"] == "amber"
    assert result["headline"] == HEADLINES["checking"]
    assert result["last_observation"] is None
    assert result["cta"] is None
    assert result["issues"] == []


def _assert_progressing_row(result: dict[str, Any]) -> None:
    assert result["verdict"] == "progressing"
    assert result["severity"] == "amber"
    assert result["headline"] == HEADLINES["blocked"]
    assert result["last_observation"] is None
    assert result["cta"] is None
    assert result["issues"] == []


def _assert_unknown_issue(result: dict[str, Any]) -> None:
    assert result["verdict"] == "attention"
    assert result["severity"] == "amber"
    assert result["headline"] == "1 thing needs your attention"
    assert result["last_observation"] is None
    assert result["cta"] is None
    assert result["issues"] == [
        {
            "text": HEADLINES["unknown"],
            "severity": "amber",
            "href": "/app/health/#brain",
        }
    ]
    assert result["severity"] != "green"
    assert result["headline"] != HEADLINES["checking"]
    assert result["issues"][0]["text"] != HEADLINES["checking"]


def _assert_unavailable_row(result: dict[str, Any]) -> None:
    assert result["verdict"] == "unavailable"
    assert result["severity"] == "amber"
    assert result["headline"] == "i don't know the status of your devices right now."
    assert result["last_observation"] is None
    assert result["cta"] is None
    assert result["issues"] == []


def _assert_local_setup_issue(result: dict[str, Any]) -> None:
    assert result["verdict"] == "attention"
    assert result["severity"] == "amber"
    assert result["headline"] == "1 thing needs your attention"
    assert result["last_observation"] is None
    assert result["cta"] is None
    assert result["issues"] == [
        {
            "text": HEADLINES["blocked"],
            "severity": "amber",
            "href": "/app/thinking/#local-setup",
        }
    ]


def test_held_permit_checking_projects_home_status_row(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    _write_config(journal, _cloud_config())
    permit = begin_brain_refresh(NOW, run_id="checking", journal_path=journal)
    assert permit is not None
    try:
        brain = build_brain_snapshot(NOW, surface="home", journal_path=journal)
        result = build_health_glance(
            _active_capture(),
            None,
            "5m ago",
            brain=brain,
            backlog=healthy_backlog_source(),
        )

        assert brain["state"] == "checking"
        assert brain["headline"] == HEADLINES["checking"]
        _assert_checking_row(result)
    finally:
        permit.release()


def test_checking_freshness_boundary_uses_explicit_now_seam(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "journal"
    _write_config(journal, _cloud_config())
    permit = begin_brain_refresh(NOW, run_id="checking-boundary", journal_path=journal)
    assert permit is not None
    try:
        fresh_now = NOW + CHECKING_TTL - timedelta(microseconds=1)
        fresh_brain = build_brain_snapshot(
            fresh_now, surface="home", journal_path=journal
        )
        fresh_result = build_health_glance(
            _active_capture(),
            None,
            "5m ago",
            brain=fresh_brain,
            backlog=healthy_backlog_source(),
        )
        assert fresh_brain["state"] == "checking"
        _assert_checking_row(fresh_result)

        for expired_now in (
            NOW + CHECKING_TTL,
            NOW + CHECKING_TTL + timedelta(microseconds=1),
        ):
            expired_brain = build_brain_snapshot(
                expired_now, surface="home", journal_path=journal
            )
            expired_result = build_health_glance(
                _active_capture(),
                None,
                "5m ago",
                brain=expired_brain,
                backlog=healthy_backlog_source(),
            )

            assert expired_brain["state"] == "unknown"
            assert expired_brain["reason_code"] == "brain_check_interrupted"
            assert expired_brain["headline"] == HEADLINES["unknown"]
            _assert_unknown_issue(expired_result)
    finally:
        permit.release()


def test_released_permit_projects_interrupted_unknown_issue(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "journal"
    _write_config(journal, _cloud_config())
    permit = begin_brain_refresh(
        NOW, run_id="checking-interrupted", journal_path=journal
    )
    assert permit is not None
    permit.release()

    brain = build_brain_snapshot(NOW, surface="home", journal_path=journal)
    result = build_health_glance(
        _active_capture(), None, "5m ago", brain=brain, backlog=healthy_backlog_source()
    )

    assert brain["state"] == "unknown"
    assert brain["reason_code"] == "brain_check_interrupted"
    _assert_unknown_issue(result)


def test_bundled_runtime_transition_projects_home_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        brain_state_module,
        "_bundled_runtime_fingerprint_sha",
        lambda: BUNDLED_RUNTIME_FINGERPRINT,
    )
    journal = tmp_path / "journal"
    _write_ready_record(journal, _bundled_config())

    _write_runtime_health_record(journal, phase="starting")
    progressing_brain = build_brain_snapshot(NOW, surface="home", journal_path=journal)
    progressing_result = build_health_glance(
        _active_capture(),
        None,
        "5m ago",
        brain=progressing_brain,
        backlog=healthy_backlog_source(),
    )

    assert progressing_brain["state"] == "blocked"
    assert progressing_brain["reason_code"] == "local_runtime_not_ready"
    assert progressing_brain["progressing"] is True
    assert progressing_brain["action"] is None
    _assert_progressing_row(progressing_result)

    _write_runtime_health_record(journal, phase="backoff")
    blocked_brain = build_brain_snapshot(NOW, surface="home", journal_path=journal)
    blocked_result = build_health_glance(
        _active_capture(),
        None,
        "5m ago",
        brain=blocked_brain,
        backlog=healthy_backlog_source(),
    )

    assert blocked_brain["state"] == "blocked"
    assert blocked_brain["reason_code"] == "local_runtime_not_ready"
    assert blocked_brain["progressing"] is False
    assert blocked_brain["action"] == {
        "label": "open local setup",
        "href": "/app/thinking/#local-setup",
    }
    _assert_local_setup_issue(blocked_result)


def test_capture_variants_against_real_checking_projection(tmp_path: Path) -> None:
    journal = tmp_path / "journal"
    _write_config(journal, _cloud_config())
    permit = begin_brain_refresh(NOW, run_id="checking-capture", journal_path=journal)
    assert permit is not None
    try:
        brain = build_brain_snapshot(NOW, surface="home", journal_path=journal)

        active = build_health_glance(
            _active_capture(),
            None,
            "5m ago",
            brain=brain,
            backlog=healthy_backlog_source(),
        )
        no_observers = build_health_glance(
            _no_observers_capture(),
            None,
            None,
            brain=brain,
            backlog=healthy_backlog_source(),
        )
        unavailable = build_health_glance(
            _unavailable_capture(),
            None,
            None,
            brain=brain,
            backlog=healthy_backlog_source(),
        )

        assert brain["state"] == "checking"
        _assert_checking_row(active)
        _assert_checking_row(no_observers)
        _assert_unavailable_row(unavailable)
    finally:
        permit.release()
