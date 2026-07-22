# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from solstone.apps.thinking import routes
from solstone.convey import create_app
from solstone.think import brain_health

NOW = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()
EXPIRES_ISO = datetime(2026, 4, 10, 13, 0, tzinfo=timezone.utc).isoformat()
_RECORD_DEFAULT = object()


def _client(
    settings_env,
    *,
    confidential: bool = False,
    active_provider: str = "google",
):
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": 1700000000000}
    config.setdefault("providers", {})["active"] = {
        "provider": active_provider,
        "model": "local/qwen3.5-4b"
        if active_provider == "local"
        else "gemini-3.5-flash",
    }
    if confidential:
        credential = "credential-secret"
        config.setdefault("services", {})["confidential"] = {
            "enabled_at": "2026-05-24T00:00:00Z",
            "account_id": "acct-secret",
            "endpoint_url": "https://spp.example.test/v1",
            "served_model_id": "confidential-model",
            "credential_created_at": "2026-05-24T00:00:00Z",
            "credential_fingerprint_sha256": hashlib.sha256(
                credential.encode("utf-8")
            ).hexdigest(),
            "prior_active": {"provider": "google", "model": "gemini-3.5-flash"},
            "prior_local_endpoint": None,
        }
        config.setdefault("providers", {})["local"] = {
            "endpoint_url": "https://spp.example.test",
            "served_model_id": "confidential-model",
            "credential": credential,
        }
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def _providers(client) -> dict:
    response = client.get("/app/thinking/api/providers")
    assert response.status_code == 200
    payload = response.get_json()
    assert isinstance(payload, dict)
    return payload


def _brain_snapshot() -> dict[str, Any]:
    return {
        "state": "ready",
        "headline": "sol can think",
        "reason_code": None,
        "reason_text": "ok",
        "failing_component": None,
        "action": None,
        "identity": {"lane": "spp", "provider": "local", "model": "local/qwen3.5-4b"},
        "evidence": {
            "observed_at": NOW_ISO,
            "age_seconds": 0,
            "age_text": "0s",
        },
        "components": {
            "generate": {
                "status": "ok",
                "reason_code": None,
                "reason_text": "ok",
                "observed_at": NOW_ISO,
            },
            "cogitate": {
                "status": "ok",
                "reason_code": None,
                "reason_text": "ok",
                "observed_at": NOW_ISO,
            },
        },
        "progressing": False,
    }


def _presentation(attestation: dict[str, Any]) -> dict[str, Any]:
    return {
        "brain": _brain_snapshot(),
        "spp_active": True,
        "spp_readiness": {
            "generate_ready": True,
            "cogitate_ready": True,
            "issues": [],
        },
        "confidential_attestation": attestation,
    }


def _component(status: str = "ok", reason: str | None = None) -> dict[str, Any]:
    component: dict[str, Any] = {
        "status": status,
        "observed_at": NOW_ISO,
    }
    if status == "ok":
        component["expires_at"] = EXPIRES_ISO
    if reason is not None:
        component["reason_code"] = reason
    return component


def _record(
    *,
    lane_prerequisites: dict[str, Any] | None = None,
    generate: dict[str, Any] | None = None,
    cogitate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "revision": 1,
        "aggregate_state": "ready",
        "reason_code": None,
        "active_lane": "spp",
        "active_provider": "local",
        "active_model": "local/qwen3.5-4b",
        "fingerprint_sha256": "a" * 64,
        "checking": None,
        "evidence": {
            "configuration": _component(),
            "lane_prerequisites": lane_prerequisites
            if lane_prerequisites is not None
            else _component(),
            "generate": generate if generate is not None else _component(),
            "cogitate": cogitate if cogitate is not None else _component(),
        },
        "runtime_failure_marker": None,
        "diagnostic": {},
        "updated_at": NOW_ISO,
    }


def _inspection(
    *,
    aggregate: str = "ready",
    reason: str | None = None,
    lane: str | None = "spp",
    record: dict[str, Any] | None | object = _RECORD_DEFAULT,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "path": "/tmp/brain.json",
        "record": _record() if record is _RECORD_DEFAULT else record,
        "projection": {
            "aggregate_state": aggregate,
            "reason_code": reason,
            "active_lane": lane,
            "active_provider": "local" if lane == "spp" else "google",
            "active_model": "local/qwen3.5-4b" if lane == "spp" else "gemini",
            "fingerprint_sha256": "a" * 64,
            "runtime_transition_in_progress": False,
        },
        "reason_code": reason,
        "error": None,
    }


def _build_presentation(monkeypatch, inspection: dict[str, Any], *, configured: bool):
    monkeypatch.setattr(
        brain_health, "inspect_brain_state", lambda *_a, **_k: inspection
    )
    return brain_health.build_brain_presentation(
        NOW,
        surface="thinking",
        spp_configured=configured,
    )


def test_active_lane_confidential_attestation_defaults_to_off(settings_env):
    client = _client(settings_env)

    payload = _providers(client)

    assert payload["active_lane"]["confidential_attestation"] == {
        "state": "off",
        "reason": "confidential_not_configured",
        "observed_at": None,
        "expires_at": None,
    }


def test_active_lane_confidential_attestation_configured_but_inactive(settings_env):
    client = _client(settings_env, confidential=True)

    payload = _providers(client)

    assert payload["active_lane"]["confidential_attestation"] == {
        "state": "inactive",
        "reason": "confidential_not_active",
        "observed_at": None,
        "expires_at": None,
    }


def test_active_lane_confidential_attestation_uses_canonical_presentation(
    settings_env,
    monkeypatch,
):
    client = _client(settings_env, confidential=True, active_provider="local")
    attestation = {
        "state": "verified",
        "reason": None,
        "observed_at": NOW_ISO,
        "expires_at": EXPIRES_ISO,
    }
    monkeypatch.setattr(
        routes,
        "build_brain_presentation",
        lambda *_args, **_kwargs: _presentation(attestation),
    )

    response = client.get("/app/thinking/api/providers")
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["active_lane"]["confidential_attestation"] == attestation
    assert set(payload["active_lane"]["confidential_attestation"]) == {
        "state",
        "reason",
        "observed_at",
        "expires_at",
    }
    serialized = response.get_data(as_text=True)
    assert "last_verified" not in serialized


@pytest.mark.parametrize(
    ("attestation", "expected"),
    [
        (
            {
                "state": "verifying",
                "reason": "brain_check_in_progress",
                "observed_at": None,
                "expires_at": None,
            },
            {
                "state": "verifying",
                "reason": "brain_check_in_progress",
                "observed_at": None,
                "expires_at": None,
            },
        ),
        (
            {
                "state": "unreachable",
                "reason": "attestation_not_verified",
                "observed_at": NOW_ISO,
                "expires_at": None,
            },
            {
                "state": "unreachable",
                "reason": "attestation_not_verified",
                "observed_at": NOW_ISO,
                "expires_at": None,
            },
        ),
        (
            {
                "state": "failed",
                "reason": "attestation_rejected",
                "observed_at": NOW_ISO,
                "expires_at": None,
            },
            {
                "state": "failed",
                "reason": "attestation_rejected",
                "observed_at": NOW_ISO,
                "expires_at": None,
            },
        ),
        (
            {
                "state": "stale",
                "reason": "attestation_expired",
                "observed_at": NOW_ISO,
                "expires_at": EXPIRES_ISO,
            },
            {
                "state": "stale",
                "reason": "attestation_expired",
                "observed_at": NOW_ISO,
                "expires_at": EXPIRES_ISO,
            },
        ),
        (
            {
                "state": "stale",
                "reason": "brain_record_stale",
                "observed_at": None,
                "expires_at": None,
            },
            {
                "state": "stale",
                "reason": "brain_record_stale",
                "observed_at": None,
                "expires_at": None,
            },
        ),
    ],
)
def test_route_serializes_closed_attestation_view(
    settings_env,
    monkeypatch,
    attestation,
    expected,
):
    client = _client(settings_env, confidential=True, active_provider="local")
    monkeypatch.setattr(
        routes,
        "build_brain_presentation",
        lambda *_args, **_kwargs: _presentation(attestation),
    )

    payload = _providers(client)

    assert payload["active_lane"]["confidential_attestation"] == expected


@pytest.mark.parametrize(
    ("inspection", "configured", "expected"),
    [
        (
            _inspection(lane="byo-cloud"),
            False,
            {
                "state": "off",
                "reason": "confidential_not_configured",
                "observed_at": None,
                "expires_at": None,
            },
        ),
        (
            _inspection(lane="byo-cloud"),
            True,
            {
                "state": "inactive",
                "reason": "confidential_not_active",
                "observed_at": None,
                "expires_at": None,
            },
        ),
        (
            _inspection(aggregate="checking", reason="brain_check_in_progress"),
            True,
            {
                "state": "verifying",
                "reason": "brain_check_in_progress",
                "observed_at": None,
                "expires_at": None,
            },
        ),
        (
            _inspection(
                aggregate="unhealthy",
                reason="provider_unavailable",
                record=_record(generate=_component("failed", "provider_unavailable")),
            ),
            True,
            {
                "state": "verified",
                "reason": None,
                "observed_at": NOW_ISO,
                "expires_at": EXPIRES_ISO,
            },
        ),
        (
            _inspection(
                aggregate="blocked",
                reason="attestation_not_verified",
                record=_record(
                    lane_prerequisites=_component(
                        "blocked",
                        "attestation_not_verified",
                    ),
                    generate=_component("not_attempted", "attestation_not_verified"),
                    cogitate=_component("not_attempted", "attestation_not_verified"),
                ),
            ),
            True,
            {
                "state": "unreachable",
                "reason": "attestation_not_verified",
                "observed_at": NOW_ISO,
                "expires_at": None,
            },
        ),
        (
            _inspection(
                aggregate="unhealthy",
                reason="attestation_rejected",
                record=_record(
                    lane_prerequisites=_component("failed", "attestation_rejected"),
                    generate=_component("not_attempted", "attestation_rejected"),
                    cogitate=_component("not_attempted", "attestation_rejected"),
                ),
            ),
            True,
            {
                "state": "failed",
                "reason": "attestation_rejected",
                "observed_at": NOW_ISO,
                "expires_at": None,
            },
        ),
        (
            _inspection(
                aggregate="unhealthy",
                reason="attestation_expired",
                record=_record(
                    lane_prerequisites=_component("failed", "attestation_expired"),
                    generate=_component("not_attempted", "attestation_expired"),
                    cogitate=_component("not_attempted", "attestation_expired"),
                ),
            ),
            True,
            {
                "state": "stale",
                "reason": "attestation_expired",
                "observed_at": NOW_ISO,
                "expires_at": None,
            },
        ),
        (
            _inspection(
                aggregate="unknown",
                reason="brain_record_missing",
                record=None,
            ),
            True,
            {
                "state": "stale",
                "reason": "brain_record_missing",
                "observed_at": None,
                "expires_at": None,
            },
        ),
    ],
)
def test_build_brain_presentation_maps_confidential_attestation(
    monkeypatch,
    inspection,
    configured,
    expected,
):
    presentation = _build_presentation(monkeypatch, inspection, configured=configured)

    assert presentation["confidential_attestation"] == expected


@pytest.mark.parametrize(
    ("reason", "aggregate", "component_status", "expected_state"),
    [
        ("nvattest_install_failed", "unhealthy", "failed", "failed"),
        ("nvattest_platform_unsupported", "blocked", "blocked", "failed"),
        ("nvattest_unavailable", "blocked", "blocked", "failed"),
        ("nvattest_integrity_failed", "unhealthy", "failed", "failed"),
        ("nvattest_install_in_progress", "blocked", "blocked", "verifying"),
    ],
)
def test_build_brain_presentation_preserves_nvattest_reason(
    monkeypatch,
    reason: str,
    aggregate: str,
    component_status: str,
    expected_state: str,
):
    inspection = _inspection(
        aggregate=aggregate,
        reason=reason,
        record=_record(
            lane_prerequisites=_component(component_status, reason),
            generate=_component("not_attempted", reason),
            cogitate=_component("not_attempted", reason),
        ),
    )

    presentation = _build_presentation(monkeypatch, inspection, configured=True)

    assert presentation["confidential_attestation"] == {
        "state": expected_state,
        "reason": reason,
        "observed_at": NOW_ISO,
        "expires_at": None,
    }


@pytest.mark.parametrize(
    "inspection",
    [
        _inspection(aggregate="checking", reason="brain_check_in_progress"),
        _inspection(aggregate="unknown", reason="brain_record_missing", record=None),
        _inspection(
            aggregate="ready",
            reason=None,
            record=_record(generate=_component("failed")),
        ),
        _inspection(
            aggregate="unknown",
            reason=None,
            record=None,
        ),
    ],
)
def test_spp_readiness_not_ready_always_has_issue(monkeypatch, inspection):
    presentation = _build_presentation(monkeypatch, inspection, configured=True)
    readiness = presentation["spp_readiness"]

    if not (readiness["generate_ready"] and readiness["cogitate_ready"]):
        assert readiness["issues"]
