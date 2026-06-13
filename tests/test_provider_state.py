# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
from types import SimpleNamespace

import pytest

from solstone.think.models import LOCAL_MODEL
from solstone.think.providers import (
    local_endpoint,
    local_install,
    local_server,
    local_vulkan,
    state,
)
from solstone.think.providers.shared import (
    RUNTIME_REASON_CODES,
    classify_provider_error,
)


def _provider_exc(
    module: str,
    name: str,
    *,
    message: str = "provider error",
    attrs: dict[str, object] | None = None,
) -> BaseException:
    exc_type = type(name, (Exception,), {"__module__": module})
    exc = exc_type(message)
    for attr, value in (attrs or {}).items():
        setattr(exc, attr, value)
    return exc


def _readiness(
    *,
    binary: bool = True,
    model: bool = True,
    ram: bool = True,
    gpu: bool = True,
    gpu_probe_ok: bool | None = None,
    install_state: str = "installed",
) -> dict:
    payload = {
        "install_state": install_state,
        "binary_installed": binary,
        "model_installed": model,
        "ram_sufficient": ram,
        "gpu_available": gpu,
        "binary_path": "/tmp/llama-server",
        "model_path": "/tmp/model.gguf",
        "model_id": LOCAL_MODEL,
        "install_error": None,
    }
    if gpu_probe_ok is not None:
        payload["gpu_probe_ok"] = gpu_probe_ok
    return payload


def _byo_endpoint() -> local_endpoint.LocalEndpoint:
    return local_endpoint.LocalEndpoint(
        base_url="http://byo.example",
        served_model_id="served-model",
        credential=None,
        is_bundled=False,
    )


def test_runtime_reason_codes_are_state_reason_codes():
    known_returns = {
        "provider_quota_exceeded",
        "provider_key_invalid",
        "chat_timeout",
        "network_unreachable",
        "provider_unavailable",
        "provider_response_invalid",
        "context_window_exceeded",
        "max_turns_exhausted",
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


def test_classify_provider_error_matches_provider_exception_names():
    cases = [
        (
            _provider_exc("anthropic", "AuthenticationError"),
            "anthropic",
            "provider_key_invalid",
        ),
        (
            _provider_exc("openai", "PermissionDeniedError"),
            "openai",
            "provider_key_invalid",
        ),
        (
            _provider_exc(
                "google.genai.errors",
                "ClientError",
                attrs={"_status_code": 403},
            ),
            "google",
            "provider_key_invalid",
        ),
        (
            _provider_exc("anthropic", "RateLimitError"),
            "anthropic",
            "provider_quota_exceeded",
        ),
        (
            _provider_exc(
                "google.genai.errors",
                "ClientError",
                attrs={"_status_code": 429},
            ),
            "google",
            "provider_quota_exceeded",
        ),
        (
            _provider_exc(
                "google.genai.errors",
                "ClientError",
                attrs={"_status_text": "RESOURCE_EXHAUSTED"},
            ),
            "google",
            "provider_quota_exceeded",
        ),
        (_provider_exc("openai", "APITimeoutError"), "openai", "chat_timeout"),
        (_provider_exc("httpx", "TimeoutException"), "openai", "chat_timeout"),
        (
            _provider_exc("anthropic", "APIConnectionError"),
            "anthropic",
            "network_unreachable",
        ),
        (_provider_exc("httpx", "RequestError"), "openai", "network_unreachable"),
        (
            _provider_exc("openai", "InternalServerError"),
            "openai",
            "provider_unavailable",
        ),
        (
            _provider_exc("google.genai.errors", "ServerError"),
            "google",
            "provider_unavailable",
        ),
        (
            _provider_exc("anthropic", "APIStatusError", attrs={"status_code": 503}),
            "anthropic",
            "provider_unavailable",
        ),
        (
            _provider_exc(
                "httpx",
                "HTTPStatusError",
                attrs={"response": SimpleNamespace(status_code=502)},
            ),
            "openai",
            "provider_unavailable",
        ),
        (
            _provider_exc("google.genai.errors", "UnknownApiResponseError"),
            "google",
            "provider_response_invalid",
        ),
    ]

    for exc, provider, expected in cases:
        assert classify_provider_error(exc, provider) == expected


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


def test_local_readiness_gpu_unavailable_blocks_before_missing_artifacts(monkeypatch):
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model=None: _readiness(binary=False, model=False, gpu=False),
    )

    provider_state = state.readiness_for_provider("local", "generate")

    assert provider_state.status == "blocked"
    assert provider_state.reason_code == "gpu_unavailable"
    assert provider_state.source == "local_install"


@pytest.mark.parametrize("gpu_available", [True, False])
def test_local_readiness_gpu_probe_failed_precedes_gpu_unavailable(
    monkeypatch, gpu_available
):
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model=None: _readiness(
            gpu=gpu_available,
            gpu_probe_ok=False,
        ),
    )
    monkeypatch.setattr(
        local_server,
        "probe_state",
        lambda: (_ for _ in ()).throw(AssertionError("server probe not expected")),
    )

    provider_state = state.readiness_for_provider("local", "generate")

    assert provider_state.status == "blocked"
    assert provider_state.reason_code == "gpu_probe_failed"
    assert provider_state.source == "local_install"


def test_local_readiness_gpu_probe_ok_true_keeps_gpu_unavailable(monkeypatch):
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model=None: _readiness(gpu=False, gpu_probe_ok=True),
    )
    monkeypatch.setattr(
        local_server,
        "probe_state",
        lambda: (_ for _ in ()).throw(AssertionError("server probe not expected")),
    )

    provider_state = state.readiness_for_provider("local", "generate")

    assert provider_state.status == "blocked"
    assert provider_state.reason_code == "gpu_unavailable"
    assert provider_state.source == "local_install"


def test_local_readiness_gpu_unavailable_flows_from_inspect_without_launch(
    tmp_path, monkeypatch
):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text(
        json.dumps({"providers": {}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    binary = local_install.binary_path_for_pin()
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755)
    gguf = local_install.model_path(LOCAL_MODEL)
    gguf.parent.mkdir(parents=True, exist_ok=True)
    gguf.write_text("model", encoding="utf-8")
    mmproj = local_install.mmproj_path(LOCAL_MODEL)
    if mmproj is not None:
        mmproj.write_text("mmproj", encoding="utf-8")

    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])
    monkeypatch.setattr(
        local_server,
        "probe_state",
        lambda: (_ for _ in ()).throw(AssertionError("server probe not expected")),
    )

    readiness = local_install.inspect_readiness(LOCAL_MODEL)
    provider_state = state.readiness_for_provider("local", "generate")

    assert readiness["binary_installed"] is True
    assert readiness["model_installed"] is True
    assert readiness["gpu_available"] is False
    assert provider_state.status == "blocked"
    assert provider_state.reason_code == "gpu_unavailable"
    assert provider_state.source == "local_install"


def test_local_readiness_gpu_probe_failed_flows_from_inspect_without_launch(
    tmp_path, monkeypatch
):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text(
        json.dumps({"providers": {}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    binary = local_install.binary_path_for_pin()
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755)
    gguf = local_install.model_path(LOCAL_MODEL)
    gguf.parent.mkdir(parents=True, exist_ok=True)
    gguf.write_text("model", encoding="utf-8")
    mmproj = local_install.mmproj_path(LOCAL_MODEL)
    if mmproj is not None:
        mmproj.write_text("mmproj", encoding="utf-8")

    monkeypatch.setattr(
        local_vulkan,
        "detect_gpus",
        lambda: [
            local_vulkan.VulkanDevice(
                0,
                "NVIDIA Test GPU",
                local_vulkan.VK_TYPE_DISCRETE,
                8192,
            )
        ],
    )
    monkeypatch.setattr(local_vulkan, "gpu_probe_ok", lambda: False)
    monkeypatch.setattr(
        local_server,
        "probe_state",
        lambda: (_ for _ in ()).throw(AssertionError("server probe not expected")),
    )

    readiness = local_install.inspect_readiness(LOCAL_MODEL)
    provider_state = state.readiness_for_provider("local", "generate")

    assert readiness["binary_installed"] is True
    assert readiness["model_installed"] is True
    assert readiness["gpu_available"] is True
    assert readiness["gpu_probe_ok"] is False
    assert provider_state.status == "blocked"
    assert provider_state.reason_code == "gpu_probe_failed"
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


def test_local_readiness_byo_reachable_skips_bundled_checks(monkeypatch):
    monkeypatch.setattr(local_endpoint, "resolve_local_endpoint", _byo_endpoint)
    monkeypatch.setattr(
        local_endpoint, "probe_local_endpoint", lambda _endpoint: (True, None)
    )
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model=None: (_ for _ in ()).throw(
            AssertionError("install check not expected")
        ),
    )
    monkeypatch.setattr(
        local_server,
        "probe_state",
        lambda: (_ for _ in ()).throw(AssertionError("server probe not expected")),
    )

    provider_state = state.readiness_for_provider("local", "generate")

    assert provider_state.status == "ready"
    assert provider_state.reason_code is None
    assert provider_state.model == "served-model"
    assert provider_state.source == "local_endpoint"


def test_local_readiness_byo_unreachable(monkeypatch):
    monkeypatch.setattr(local_endpoint, "resolve_local_endpoint", _byo_endpoint)
    monkeypatch.setattr(
        local_endpoint,
        "probe_local_endpoint",
        lambda _endpoint: (False, "connection refused"),
    )

    provider_state = state.readiness_for_provider("local", "cogitate")

    assert provider_state.status == "unhealthy"
    assert provider_state.reason_code == "local_endpoint_unreachable"
    assert provider_state.model == "served-model"
    assert provider_state.message == "connection refused"
    assert provider_state.source == "local_endpoint"


@pytest.mark.parametrize(
    ("selected_config", "reachable", "expected_issues"),
    [
        ({"providers": {"generate": {"provider": "local"}}}, True, []),
        (
            {"providers": {"generate": {"provider": "google"}}},
            False,
            ["local_endpoint_unreachable"],
        ),
    ],
)
def test_local_status_dict_byo(
    monkeypatch, selected_config, reachable, expected_issues
):
    monkeypatch.setattr("solstone.think.models.get_config", lambda: selected_config)
    monkeypatch.setattr(local_endpoint, "resolve_local_endpoint", _byo_endpoint)
    monkeypatch.setattr(
        local_endpoint,
        "probe_local_endpoint",
        lambda _endpoint: (reachable, None if reachable else "connection refused"),
    )
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model=None: (_ for _ in ()).throw(
            AssertionError("install check not expected")
        ),
    )

    status = state.local_status_dict()

    assert status == {
        "configured": True,
        "selected": selected_config["providers"]["generate"]["provider"] == "local",
        "generate_ready": reachable,
        "cogitate_ready": reachable,
        "cogitate_cli": None,
        "cogitate_cli_found": False,
        "issues": expected_issues,
    }


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
