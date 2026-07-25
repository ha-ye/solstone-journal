# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import Flask

from solstone.think.brain_health import HEADLINES
from tests.helpers.health_glance import healthy_backlog_source


@dataclass(frozen=True)
class Case:
    name: str
    state: str
    reason_code: str | None
    reason_text: str
    progressing: bool = False
    lane: str | None = "byo-cloud"
    provider: str | None = "google"
    model: str | None = "gemini"
    component: str | None = "generate"


CASES = [
    Case("ready", "ready", None, "ok", component=None),
    Case("checking", "checking", "brain_check_in_progress", "brain check in progress"),
    Case(
        "blocked",
        "blocked",
        "thinking_engine_not_chosen",
        "no thinking engine chosen",
        lane="none",
        provider=None,
        model=None,
        component="configuration",
    ),
    Case(
        "blocked-progressing",
        "blocked",
        "local_runtime_not_ready",
        "local runtime not ready",
        progressing=True,
        lane="bundled",
        provider="local",
        model="local/model",
        component="lane_prerequisites",
    ),
    Case("unhealthy", "unhealthy", "provider_unavailable", "provider unavailable"),
    Case("unknown", "unknown", "brain_record_unavailable", "brain record unavailable"),
    Case(
        "checking-expired",
        "unknown",
        "brain_check_interrupted",
        "brain check interrupted",
    ),
    Case("stale", "unknown", "brain_record_stale", "brain record stale"),
    Case("malformed", "unknown", "brain_record_invalid", "brain record invalid"),
    Case("missing", "unknown", "brain_record_missing", "brain record missing"),
    Case(
        "fingerprint-mismatch",
        "unknown",
        "brain_config_changed",
        "brain config changed",
    ),
]


def test_headlines_are_pinned_byte_for_byte():
    assert HEADLINES == {
        "ready": "sol can think",
        "checking": "checking how sol thinks",
        "blocked": "sol needs a way to think",
        "unhealthy": "sol's thinking needs attention",
        "unknown": "thinking status unavailable",
    }


def _expected_action(case: Case, surface: str) -> dict[str, Any] | None:
    if case.state in {"ready", "checking"}:
        return None
    if case.state == "blocked" and case.progressing:
        return None
    if case.state in {"blocked", "unhealthy"}:
        if case.lane == "bundled" and case.reason_code in {
            "gpu_unavailable",
            "local_runtime_not_ready",
            "local_artifact_not_ready",
            "local_server_unhealthy",
            "local_runtime_state_invalid",
            "local_runtime_state_unavailable",
            "local_runtime_state_stale",
            "local_runtime_fingerprint_mismatch",
        }:
            return {"label": "open local setup", "href": "/app/thinking/#local-setup"}
        return {"label": "open thinking", "href": "/app/thinking/#main"}
    if case.state == "unknown" and case.reason_code == "configuration_invalid":
        return {"label": "open thinking", "href": "/app/thinking/#main"}
    if surface in {"thinking", "health"}:
        return {"label": "check again", "refresh": True}
    if surface in {"home", "support"}:
        return {"label": "view health", "href": "/app/health/#brain"}
    return {"label": "check again", "command": "journal brain refresh"}


def _snapshot(case: Case, surface: str) -> dict[str, Any]:
    return {
        "state": case.state,
        "headline": HEADLINES[case.state],
        "reason_code": case.reason_code,
        "reason_text": case.reason_text,
        "failing_component": case.component,
        "action": _expected_action(case, surface),
        "identity": {
            "lane": case.lane,
            "provider": case.provider,
            "model": case.model,
        },
        "evidence": {
            "observed_at": "2026-07-21T00:00:00+00:00",
            "age_seconds": 300,
            "age_text": "5m",
        },
        "components": {
            "generate": {
                "status": "ok" if case.state == "ready" else "failed",
                "reason_code": case.reason_code,
                "reason_text": case.reason_text,
                "observed_at": "2026-07-21T00:00:00+00:00",
            },
            "cogitate": {
                "status": "ok",
                "reason_code": None,
                "reason_text": "ok",
                "observed_at": "2026-07-21T00:00:00+00:00",
            },
        },
        "progressing": case.progressing,
    }


def test_state_parity_matrix(monkeypatch):
    from solstone.apps.health import routes as health_routes
    from solstone.apps.home.health_glance import build_health_glance
    from solstone.apps.support import diagnostics
    from solstone.apps.thinking import routes as thinking_routes
    from solstone.think import doctor, top

    app = Flask(__name__)
    app.register_blueprint(health_routes.health_bp)

    for case in CASES:
        home_brain = _snapshot(case, "home")
        home = build_health_glance(
            {"status": "active", "observers": [{"status": "active"}]},
            None,
            "5m ago",
            brain=home_brain,
            backlog=healthy_backlog_source(),
        )
        home_issue = home["issues"][0] if home["issues"] else None
        assert home_brain["headline"] == HEADLINES[case.state]
        if case.state == "ready":
            assert home["verdict"] == "ok"
            assert home["severity"] == "green"
            assert home["headline"] == "everything's working"
            assert home["last_observation"] == "5m ago"
            assert home["cta"] is None
            assert home["issues"] == []
        elif case.state == "checking":
            assert home["verdict"] == "checking"
            assert home["severity"] == "amber"
            assert home["headline"] == HEADLINES["checking"]
            assert home["last_observation"] is None
            assert home["cta"] is None
            assert home["issues"] == []
            assert home_brain["action"] is None
        elif case.state == "blocked" and case.progressing:
            assert home["verdict"] == "progressing"
            assert home["severity"] == "amber"
            assert home["headline"] == HEADLINES["blocked"]
            assert home["last_observation"] is None
            assert home["cta"] is None
            assert home["issues"] == []
            assert home_brain["action"] is None
        else:
            assert home["verdict"] == "attention"
            assert home["severity"] == "amber"
            assert home["headline"] == "1 thing needs your attention"
            assert home["last_observation"] is None
            assert home["cta"] is None
            assert len(home["issues"]) == 1
            assert home_issue is not None
            assert home_issue["text"] == home_brain["headline"]
            assert home_issue["href"] == (
                home_brain["action"] or {"href": "/app/health/#brain"}
            ).get("href", "/app/health/#brain")
        if case.state != "ready":
            assert home["severity"] != "green"

        thinking_brain = _snapshot(case, "thinking")
        monkeypatch.setattr(thinking_routes, "get_provider_list", lambda: [])
        monkeypatch.setattr(
            thinking_routes,
            "build_provider_status",
            lambda _p, **_kwargs: {},
        )
        monkeypatch.setattr(thinking_routes.local_bootstrap, "get_state", lambda _m: {})
        monkeypatch.setattr(
            thinking_routes,
            "build_brain_presentation",
            lambda *_a, **_k: {
                "brain": thinking_brain,
                "spp_active": False,
                "spp_readiness": {
                    "generate_ready": False,
                    "cogitate_ready": False,
                    "issues": ["brain_record_missing"],
                },
                "confidential_attestation": {
                    "state": "off",
                    "reason": "confidential_not_configured",
                    "observed_at": None,
                    "expires_at": None,
                },
            },
        )
        thinking_payload = thinking_routes._provider_payload({}, "local/model")
        assert thinking_payload["brain"]["headline"] == HEADLINES[case.state]
        assert thinking_payload["brain"]["action"] == _expected_action(case, "thinking")

        health_brain = _snapshot(case, "health")
        monkeypatch.setattr(
            health_routes,
            "build_brain_snapshot",
            lambda *_a, **_k: health_brain,
        )
        with app.test_request_context("/app/health/api/info"):
            health_payload = health_routes.api_info().get_json()
        assert health_payload["brain"]["headline"] == HEADLINES[case.state]
        assert health_payload["brain"]["action"] == _expected_action(case, "health")

        support_brain = _snapshot(case, "support")
        monkeypatch.setattr(
            "solstone.think.brain_health.build_brain_snapshot",
            lambda *_a, **_k: support_brain,
        )
        section = diagnostics.collect_brain_health()
        assert section["snapshot"]["headline"] == HEADLINES[case.state]
        assert section["snapshot"]["action"] == _expected_action(case, "support")

        cli_brain = _snapshot(case, "cli")
        monkeypatch.setattr(
            "solstone.think.brain_health.build_brain_snapshot",
            lambda *_a, **_k: cli_brain,
        )
        result = doctor.brain_check(doctor.Args(False, False, False, 0))
        assert result.status == (
            "ok" if case.state in {"ready", "checking"} else "warn"
        )
        assert HEADLINES[case.state] in result.detail
        assert case.state in result.detail

        manager = top.ServiceManager()
        manager.brain_health = cli_brain
        section_lines = manager.render_brain_health_section()
        assert "Brain Health" in "\n".join(section_lines)
        assert HEADLINES[case.state] in "\n".join(section_lines)
