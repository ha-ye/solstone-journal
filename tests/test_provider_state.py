# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json

from solstone.think.models import LOCAL_MODEL
from solstone.think.providers import local_install, local_server, state
from solstone.think.providers.shared import (
    RUNTIME_REASON_CODES,
    classify_provider_error,
)


def _readiness(
    *,
    binary: bool = True,
    model: bool = True,
    ram: bool = True,
    install_state: str = "installed",
) -> dict:
    return {
        "install_state": install_state,
        "binary_installed": binary,
        "model_installed": model,
        "ram_sufficient": ram,
        "binary_path": "/tmp/llama-server",
        "model_path": "/tmp/model.gguf",
        "model_id": LOCAL_MODEL,
        "install_error": None,
    }


def test_runtime_reason_codes_are_state_reason_codes():
    known_returns = {
        "provider_quota_exceeded",
        "provider_key_invalid",
        "chat_timeout",
        "network_unreachable",
        "provider_unavailable",
        "provider_response_invalid",
        "unknown",
    }
    assert RUNTIME_REASON_CODES == frozenset(known_returns)
    assert state.REASON_CODES == state.READINESS_REASON_CODES | RUNTIME_REASON_CODES
    assert "provider_quota_exceeded" in RUNTIME_REASON_CODES
    assert "provider_quota_exceeded" in state.REASON_CODES

    samples = [
        ValueError("no response from model"),
        TimeoutError("timed out"),
        ConnectionError("network down"),
        RuntimeError("llm provider unavailable"),
        RuntimeError("unclassified"),
    ]
    assert {
        classify_provider_error(exc, "google") for exc in samples
    } <= RUNTIME_REASON_CODES


def test_cloud_readiness_missing_key(monkeypatch):
    monkeypatch.setattr(state, "cloud_key_configured", lambda _env_key: False)

    provider_state = state.readiness_for_provider("google", "generate", "gemini")

    assert provider_state.status == "blocked"
    assert provider_state.reason_code == "provider_key_missing"
    assert provider_state.source == "config"


def test_cloud_readiness_key_present_without_health_row_is_unknown(monkeypatch):
    monkeypatch.setattr(state, "cloud_key_configured", lambda _env_key: True)
    monkeypatch.setattr(state, "read_health_status", lambda: None)

    provider_state = state.readiness_for_provider("google", "generate", "gemini")

    assert provider_state.status == "unknown"
    assert provider_state.reason_code == "unknown"
    assert provider_state.source == "config"


def test_cloud_readiness_ok_row_is_ready(monkeypatch):
    monkeypatch.setattr(state, "cloud_key_configured", lambda _env_key: True)
    monkeypatch.setattr(
        state,
        "read_health_status",
        lambda: {
            "checked_at": "2026-06-04T12:00:00+00:00",
            "results": [
                {
                    "provider": "google",
                    "model": "gemini",
                    "interface": "generate",
                    "ok": True,
                    "status": "ok",
                }
            ],
        },
    )

    provider_state = state.readiness_for_provider("google", "generate", "gemini")

    assert provider_state.status == "ready"
    assert provider_state.reason_code is None
    assert provider_state.checked_at == "2026-06-04T12:00:00+00:00"
    assert provider_state.source == "active_check"


def test_cloud_readiness_future_quota_row_is_unhealthy(monkeypatch):
    monkeypatch.setattr(state, "cloud_key_configured", lambda _env_key: True)
    monkeypatch.setattr(state, "now_ms", lambda: 1_000)
    monkeypatch.setattr(
        state,
        "read_health_status",
        lambda: {
            "results": [
                {
                    "provider": "google",
                    "model": "gemini",
                    "interface": "generate",
                    "ok": False,
                    "status": "quota_exhausted",
                    "reset_at_ms": 2_000,
                }
            ],
        },
    )

    provider_state = state.readiness_for_provider("google", "generate", "gemini")

    assert provider_state.status == "unhealthy"
    assert provider_state.reason_code == "provider_quota_exceeded"
    assert provider_state.reset_at_ms == 2_000


def test_cloud_readiness_expired_quota_row_is_unknown(monkeypatch):
    monkeypatch.setattr(state, "cloud_key_configured", lambda _env_key: True)
    monkeypatch.setattr(state, "now_ms", lambda: 3_000)
    monkeypatch.setattr(
        state,
        "read_health_status",
        lambda: {
            "results": [
                {
                    "provider": "google",
                    "model": "gemini",
                    "interface": "generate",
                    "ok": False,
                    "status": "quota_exhausted",
                    "reset_at_ms": 2_000,
                }
            ],
        },
    )

    provider_state = state.readiness_for_provider("google", "generate", "gemini")

    assert provider_state.status == "unknown"
    assert provider_state.reason_code == "provider_quota_exceeded"
    assert provider_state.reset_at_ms == 2_000


def test_local_readiness_missing_artifacts(monkeypatch):
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model=None: _readiness(binary=False, model=False),
    )

    provider_state = state.readiness_for_provider("local", "generate")

    assert provider_state.status == "blocked"
    assert provider_state.reason_code == "local_model_missing"
    assert provider_state.source == "local_install"


def test_local_readiness_installing(monkeypatch):
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model=None: _readiness(install_state="downloading"),
    )

    provider_state = state.readiness_for_provider("local", "generate")

    assert provider_state.status == "blocked"
    assert provider_state.reason_code == "local_model_installing"
    assert provider_state.source == "local_install"


def test_local_readiness_uses_normal_ready_state_for_non_blocking_memory(
    monkeypatch,
):
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model=None: _readiness(ram=True),
    )
    monkeypatch.setattr(
        local_server,
        "probe_state",
        lambda: (local_server.STATE_READY, None),
    )

    provider_state = state.readiness_for_provider("local", "generate")

    assert provider_state.status == "ready"
    assert provider_state.reason_code is None
    assert provider_state.source == "local_server"


def test_local_readiness_loading(monkeypatch):
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model=None: _readiness(),
    )
    monkeypatch.setattr(
        local_server,
        "probe_state",
        lambda: (local_server.STATE_LOADING, None),
    )

    provider_state = state.readiness_for_provider("local", "generate")

    assert provider_state.status == "blocked"
    assert provider_state.reason_code == "local_model_loading"
    assert provider_state.source == "local_server"


def test_local_readiness_failed_server(monkeypatch):
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model=None: _readiness(),
    )
    monkeypatch.setattr(
        local_server,
        "probe_state",
        lambda: (local_server.STATE_FAILED, "no port"),
    )

    provider_state = state.readiness_for_provider("local", "generate")

    assert provider_state.status == "unhealthy"
    assert provider_state.reason_code == "local_server_unhealthy"
    assert provider_state.message == "no port"


def test_local_readiness_ready(monkeypatch):
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model=None: _readiness(),
    )
    monkeypatch.setattr(
        local_server,
        "probe_state",
        lambda: (local_server.STATE_READY, None),
    )

    provider_state = state.readiness_for_provider("local", "generate")

    assert provider_state.status == "ready"
    assert provider_state.reason_code is None
    assert provider_state.source == "local_server"


def test_readiness_for_context_routes_to_resolved_local_provider(monkeypatch):
    monkeypatch.setattr(
        "solstone.think.models.resolve_provider",
        lambda _context, _interface: ("local", LOCAL_MODEL),
    )
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model=None: _readiness(binary=False, model=False),
    )

    provider_state = state.readiness_for_context("observe.describe.frame", "generate")

    assert provider_state.provider == "local"
    assert provider_state.model == LOCAL_MODEL
    assert provider_state.context == "observe.describe.frame"
    assert provider_state.status == "blocked"
    assert provider_state.reason_code == "local_model_missing"


def test_readiness_for_context_routes_to_resolved_cloud_provider(monkeypatch):
    monkeypatch.setattr(
        "solstone.think.models.resolve_provider",
        lambda _context, _interface: ("google", "gemini"),
    )
    monkeypatch.setattr(state, "cloud_key_configured", lambda _env_key: True)
    monkeypatch.setattr(
        state,
        "read_health_status",
        lambda: {
            "results": [
                {
                    "provider": "google",
                    "model": "gemini",
                    "interface": "generate",
                    "ok": True,
                    "status": "ok",
                }
            ],
        },
    )

    provider_state = state.readiness_for_context("talent.system.default", "generate")

    assert provider_state.provider == "google"
    assert provider_state.model == "gemini"
    assert provider_state.context == "talent.system.default"
    assert provider_state.status == "ready"


def test_record_quota_failure_writes_reason_code(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    state.record_quota_failure("google", "flash", "gemini", "cogitate", 12345)

    payload = json.loads((tmp_path / "health" / "talents.json").read_text())
    assert payload["results"][0]["reason_code"] == "provider_quota_exceeded"
