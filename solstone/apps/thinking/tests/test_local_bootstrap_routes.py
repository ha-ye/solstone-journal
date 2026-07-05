# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib
import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from solstone.apps.thinking import local_bootstrap
from solstone.apps.thinking.install_copy import INSTALL_FAILED_NO_PROGRESS
from solstone.convey import create_app
from solstone.think.models import LOCAL_MODEL, QWEN_35_9B
from solstone.think.providers import fit_report, memory
from solstone.think.providers.install_state import (
    InstallState,
    InstallStatus,
    make_idle_status,
    read_install_status,
    transition_state,
    write_install_status,
)
from solstone.think.providers.local import LOCAL_MODEL_SPECS


def _client(journal_path):
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def _settings_config() -> dict:
    return {
        "setup": {"completed_at": "2026-05-09T00:00:00Z"},
        "providers": {
            "generate": {"provider": "google", "tier": 2, "backup": "anthropic"},
            "cogitate": {"provider": "openai", "tier": 2, "backup": "anthropic"},
            "auth": {"google": "api_key", "openai": "api_key"},
        },
    }


def _write_config(journal_path, config: dict) -> None:
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _reset_local_state():
    with local_bootstrap._INSTALL_LOCK:
        local_bootstrap._INSTALL_THREADS.clear()
        local_bootstrap._INSTALL_PROGRESS.clear()


class _FakeThread:
    init_count = 0
    start_count = 0
    targets = []

    def __init__(self, *args, **kwargs):
        type(self).init_count += 1
        type(self).targets.append(kwargs.get("target"))
        self.alive = True

    def start(self):
        type(self).start_count += 1

    def is_alive(self):
        return self.alive


def _write_local_status(
    state: InstallState,
    *,
    error: str | None = None,
    last_progress_at: str | None = None,
) -> InstallStatus:
    status = make_idle_status(local_bootstrap.local_install.LOCAL_PROVIDER_NAME)
    status["install_state"] = state
    status["last_transition_at"] = "2026-05-23T00:00:00+00:00"
    status["last_progress_at"] = last_progress_at
    status["install_error"] = error if state == "failed" else None
    write_install_status(status, scope="bundled")
    return status


def _old_progress_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()


def _fresh_progress_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mlx_readiness(**overrides):
    readiness = {
        "install_state": "idle",
        "model_installed": True,
        "snapshot_installed": True,
        "variant_installed": True,
        "ram_sufficient": True,
        "platform_supported": True,
        "package_available": True,
        "model_id": QWEN_35_9B,
        "snapshot_dir": "/tmp/qwen-snapshot",
        "variant_dir": None,
        "runtime_dir": "/tmp/qwen-snapshot",
        "install_error": None,
    }
    readiness.update(overrides)
    return readiness


def _fit(
    severity: fit_report.FitSeverity,
    *,
    name: str = "test",
    detail: str | None = None,
) -> fit_report.FitReport:
    return fit_report.FitReport(
        artifact="test bootstrap",
        checks=(fit_report.FitCheck(name, severity, detail or f"{severity} detail"),),
    )


def test_mlx_backend_predicate_tracks_sys_platform(monkeypatch):
    monkeypatch.setattr(local_bootstrap.sys, "platform", "darwin")
    assert local_bootstrap._is_mlx_backend() is True

    monkeypatch.setattr(local_bootstrap.sys, "platform", "linux")
    assert local_bootstrap._is_mlx_backend() is False


def test_local_bootstrap_linux_contract_for_model_helpers():
    assert local_bootstrap._is_mlx_backend() is False
    assert local_bootstrap._resolve_model_id(LOCAL_MODEL) == LOCAL_MODEL
    assert local_bootstrap.accepted_request_model(None) == LOCAL_MODEL
    assert local_bootstrap.accepted_request_model("not-real") is None
    assert local_bootstrap.list_local_models() == [
        {
            "name": LOCAL_MODEL,
            "label": "qwen 3.5 4B VLM — 8 GB",
            "min_ram_gb": 8,
            "size_bytes": LOCAL_MODEL_SPECS[LOCAL_MODEL].size_bytes,
        },
    ]


def test_local_availability_payload_exact_shape(settings_env, monkeypatch):
    journal_path, _config = settings_env(_settings_config())
    monkeypatch.setattr(local_bootstrap, "check_binary_present", lambda: True)
    monkeypatch.setattr(local_bootstrap, "check_model_present", lambda _model: True)
    monkeypatch.setattr(local_bootstrap, "_platform_supported", lambda: (True, ""))
    monkeypatch.setattr(
        memory.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=32 * 1024**3, total=32 * 1024**3),
    )
    client = _client(journal_path)

    response = client.get("/app/thinking/api/local/availability")

    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {
        "model",
        "platform_supported",
        "total_memory_gb",
        "available_memory_gb",
        "min_ram_gb",
        "binary_present",
        "model_present",
        "available",
        "reason",
        "warning",
        "download_bytes",
    }
    assert payload == {
        "model": LOCAL_MODEL,
        "platform_supported": True,
        "total_memory_gb": 32.0,
        "available_memory_gb": 32.0,
        "min_ram_gb": 8,
        "binary_present": True,
        "model_present": True,
        "available": True,
        "reason": "",
        "warning": "",
        "download_bytes": (
            LOCAL_MODEL_SPECS[LOCAL_MODEL].size_bytes
            + (LOCAL_MODEL_SPECS[LOCAL_MODEL].mmproj_size_bytes or 0)
        ),
    }


def test_mlx_availability_payload_exact_shape(settings_env, monkeypatch):
    settings_env(_settings_config())
    monkeypatch.setattr(local_bootstrap, "_is_mlx_backend", lambda: True)
    monkeypatch.setattr(
        local_bootstrap.mlx_install,
        "inspect_readiness",
        lambda _model: _mlx_readiness(),
    )
    monkeypatch.setattr(
        memory.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=32 * 1024**3, total=32 * 1024**3),
    )

    payload = local_bootstrap.get_availability_payload(QWEN_35_9B)

    assert set(payload) == {
        "model",
        "platform_supported",
        "total_memory_gb",
        "available_memory_gb",
        "min_ram_gb",
        "binary_present",
        "model_present",
        "available",
        "reason",
        "warning",
        "download_bytes",
    }
    assert payload == {
        "model": QWEN_35_9B,
        "platform_supported": True,
        "total_memory_gb": 32.0,
        "available_memory_gb": 32.0,
        "min_ram_gb": 13,
        "binary_present": True,
        "model_present": True,
        "available": True,
        "reason": "",
        "warning": "",
        "download_bytes": 10453446077,
    }


def test_local_models_route_returns_settings_shape(settings_env):
    journal_path, _config = settings_env(_settings_config())
    client = _client(journal_path)

    response = client.get("/app/thinking/api/local/models")

    assert response.status_code == 200
    assert response.get_json() == [
        {
            "name": LOCAL_MODEL,
            "label": "qwen 3.5 4B VLM — 8 GB",
            "min_ram_gb": 8,
            "size_bytes": LOCAL_MODEL_SPECS[LOCAL_MODEL].size_bytes,
        },
    ]


def test_mlx_models_route_returns_settings_shape(settings_env, monkeypatch):
    journal_path, _config = settings_env(_settings_config())
    monkeypatch.setattr(local_bootstrap, "_is_mlx_backend", lambda: True)
    client = _client(journal_path)

    response = client.get("/app/thinking/api/local/models")

    assert response.status_code == 200
    assert response.get_json() == [
        {
            "name": QWEN_35_9B,
            "label": "qwen 3.5 9B VLM — 13 GB",
            "min_ram_gb": 13,
            "size_bytes": 10453446077,
        },
    ]


def test_mlx_availability_accepts_first_fetch_alias(settings_env, monkeypatch):
    journal_path, _config = settings_env(_settings_config())
    monkeypatch.setattr(local_bootstrap, "_is_mlx_backend", lambda: True)
    monkeypatch.setattr(
        local_bootstrap.mlx_install,
        "inspect_readiness",
        lambda _model: _mlx_readiness(),
    )
    monkeypatch.setattr(
        memory.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=32 * 1024**3, total=32 * 1024**3),
    )
    client = _client(journal_path)

    response = client.get(f"/app/thinking/api/local/availability?model={LOCAL_MODEL}")

    assert response.status_code == 200
    assert response.get_json()["model"] == QWEN_35_9B


def test_mlx_availability_blocks_below_available_floor(settings_env, monkeypatch):
    settings_env(_settings_config())
    monkeypatch.setattr(local_bootstrap, "_is_mlx_backend", lambda: True)
    monkeypatch.setattr(
        local_bootstrap.mlx_install,
        "inspect_readiness",
        lambda _model: _mlx_readiness(),
    )
    monkeypatch.setattr(
        memory.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=12 * 1024**3, total=32 * 1024**3),
    )

    payload = local_bootstrap.get_availability_payload(QWEN_35_9B)

    assert payload["available"] is False
    assert payload["min_ram_gb"] == 13
    assert payload["available_memory_gb"] == 12.0
    assert str(payload["reason"]).startswith("insufficient RAM")
    assert payload["warning"] == ""


def test_local_availability_warns_but_does_not_block_on_low_memory(
    settings_env, monkeypatch
):
    journal_path, _config = settings_env(_settings_config())
    monkeypatch.setattr(local_bootstrap, "check_binary_present", lambda: True)
    monkeypatch.setattr(local_bootstrap, "check_model_present", lambda _model: True)
    monkeypatch.setattr(local_bootstrap, "_platform_supported", lambda: (True, ""))
    monkeypatch.setattr(
        memory.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=1 * 1024**3, total=32 * 1024**3),
    )
    client = _client(journal_path)

    response = client.get("/app/thinking/api/local/availability")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["available"] is True
    assert payload["reason"] == ""
    assert payload["warning"].startswith("Available memory is below 8 GB")
    assert payload["available_memory_gb"] == 1.0


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/app/thinking/api/local/availability"),
        ("post", "/app/thinking/api/local/bootstrap"),
        ("get", "/app/thinking/api/local/bootstrap/status"),
    ],
)
def test_local_routes_reject_unknown_model(settings_env, method, path):
    journal_path, _config = settings_env(_settings_config())
    client = _client(journal_path)

    response = getattr(client, method)(f"{path}?model=not-real")

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_request_value"
    assert "not-real" in payload["detail"]
    assert LOCAL_MODEL in payload["detail"]


@pytest.mark.parametrize(
    ("method", "path", "helper_name", "return_value"),
    [
        (
            "get",
            "/app/thinking/api/local/availability",
            "get_availability_payload",
            {"available": True},
        ),
        (
            "post",
            "/app/thinking/api/local/bootstrap",
            "start_bootstrap",
            ({"install_state": "installed"}, 200),
        ),
        (
            "get",
            "/app/thinking/api/local/bootstrap/status",
            "get_state",
            {"install_state": "idle"},
        ),
    ],
)
def test_local_routes_default_to_flash_model(
    settings_env, monkeypatch, method, path, helper_name, return_value
):
    journal_path, _config = settings_env(_settings_config())
    calls = []

    def fake_helper(model):
        calls.append(model)
        return return_value

    monkeypatch.setattr(local_bootstrap, helper_name, fake_helper)
    client = _client(journal_path)

    response = getattr(client, method)(path)

    assert response.status_code == 200
    assert calls == [LOCAL_MODEL]


def test_local_bootstrap_post_rejects_unqualified_host(settings_env, monkeypatch):
    journal_path, _config = settings_env(_settings_config())
    monkeypatch.setattr(
        local_bootstrap,
        "start_bootstrap",
        lambda _model: (_ for _ in ()).throw(
            local_bootstrap.LocalBootstrapUnavailableError("unsupported platform")
        ),
    )
    client = _client(journal_path)

    response = client.post("/app/thinking/api/local/bootstrap")

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_request_value"
    assert payload["detail"] == "unsupported platform"


def test_local_bootstrap_post_rejects_byo_endpoint(settings_env):
    journal_path, config = settings_env(_settings_config())
    config["providers"]["local"] = {
        "endpoint_url": "http://host.test:8080",
        "served_model_id": "served-model",
    }
    _write_config(journal_path, config)
    client = _client(journal_path)

    response = client.post("/app/thinking/api/local/bootstrap")

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_request_value"
    assert payload["detail"] == "BYO local endpoint is active"


@pytest.mark.parametrize(
    ("state", "expected_payload", "expected_status"),
    [
        ("installed", {"install_state": "installed"}, 200),
        ("downloading", {"install_state": "downloading"}, 200),
        ("verifying", {"install_state": "verifying"}, 200),
        ("idle", {"install_state": "downloading"}, 202),
        ("failed", {"install_state": "downloading"}, 202),
    ],
)
def test_start_bootstrap_payload_for_canonical_states(
    settings_env, monkeypatch, state, expected_payload, expected_status
):
    settings_env(_settings_config())
    _write_local_status(
        state,
        error="failed before" if state == "failed" else None,
        last_progress_at=(
            _fresh_progress_iso() if state in ("downloading", "verifying") else None
        ),
    )
    monkeypatch.setattr(
        local_bootstrap,
        "get_availability_payload",
        lambda _model: {
            "platform_supported": True,
            "reason": "local runtime is not installed",
            "binary_present": False,
            "model_present": False,
            "download_bytes": (
                LOCAL_MODEL_SPECS[LOCAL_MODEL].size_bytes
                + (LOCAL_MODEL_SPECS[LOCAL_MODEL].mmproj_size_bytes or 0)
            ),
        },
    )
    monkeypatch.setattr(
        memory.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=100 * 1024**3),
    )
    monkeypatch.setattr(
        local_bootstrap, "_fit_report_for_model", lambda _model: _fit("ok")
    )
    _FakeThread.init_count = 0
    _FakeThread.start_count = 0
    monkeypatch.setattr(local_bootstrap.threading, "Thread", _FakeThread)

    assert local_bootstrap.start_bootstrap(LOCAL_MODEL) == (
        expected_payload,
        expected_status,
    )


def test_start_bootstrap_low_memory_warning_does_not_block(settings_env, monkeypatch):
    settings_env(_settings_config())
    _write_local_status("idle")
    monkeypatch.setattr(local_bootstrap, "check_binary_present", lambda: False)
    monkeypatch.setattr(local_bootstrap, "check_model_present", lambda _model: False)
    monkeypatch.setattr(local_bootstrap, "_platform_supported", lambda: (True, ""))
    monkeypatch.setattr(
        memory.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=1 * 1024**3, total=32 * 1024**3),
    )
    monkeypatch.setattr(
        memory.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=100 * 1024**3),
    )
    monkeypatch.setattr(
        local_bootstrap, "_fit_report_for_model", lambda _model: _fit("warning")
    )
    _FakeThread.init_count = 0
    _FakeThread.start_count = 0
    monkeypatch.setattr(local_bootstrap.threading, "Thread", _FakeThread)

    assert local_bootstrap.start_bootstrap(LOCAL_MODEL) == (
        {"install_state": "downloading"},
        202,
    )
    assert _FakeThread.start_count == 1


def test_start_bootstrap_insufficient_disk_blocks_before_worker(
    settings_env, monkeypatch
):
    settings_env(_settings_config())
    _write_local_status("idle")
    monkeypatch.setattr(
        local_bootstrap,
        "get_availability_payload",
        lambda _model: {
            "platform_supported": True,
            "reason": "local runtime is not installed",
            "binary_present": False,
            "model_present": False,
            "download_bytes": 4 * 1024**3,
        },
    )
    monkeypatch.setattr(
        memory.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=1 * 1024**3),
    )
    monkeypatch.setattr(
        local_bootstrap,
        "_fit_report_for_model",
        lambda _model: _fit(
            "blocked",
            name="disk",
            detail="insufficient disk space for known downloads",
        ),
    )
    _FakeThread.init_count = 0
    _FakeThread.start_count = 0
    monkeypatch.setattr(local_bootstrap.threading, "Thread", _FakeThread)

    with pytest.raises(
        local_bootstrap.LocalBootstrapUnavailableError, match="insufficient disk"
    ):
        local_bootstrap.start_bootstrap(LOCAL_MODEL)

    assert _FakeThread.init_count == 0
    status = read_install_status(scope="bundled", name="local")
    assert status["install_state"] == "idle"


def test_local_bootstrap_status_returns_canonical_shape(settings_env):
    journal_path, _config = settings_env(_settings_config())
    _write_local_status("downloading", last_progress_at=_fresh_progress_iso())
    with local_bootstrap._INSTALL_LOCK:
        local_bootstrap._INSTALL_PROGRESS[LOCAL_MODEL] = (12, 24)
    client = _client(journal_path)

    response = client.get("/app/thinking/api/local/bootstrap/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert {
        "name",
        "install_state",
        "last_transition_at",
        "last_progress_at",
        "progress_bytes_received",
        "progress_bytes_total",
        "install_error",
    } == set(payload)
    assert payload["install_state"] == "downloading"
    assert payload["progress_bytes_received"] == 12
    assert payload["progress_bytes_total"] == 24


def test_local_bootstrap_lazy_stall_without_live_thread_fails(settings_env):
    settings_env(_settings_config())
    _write_local_status("downloading", last_progress_at=_old_progress_iso())

    payload = local_bootstrap.get_state(LOCAL_MODEL)

    assert payload["install_state"] == "failed"
    assert payload["install_error"] == INSTALL_FAILED_NO_PROGRESS
    persisted = read_install_status(scope="bundled", name="local")
    assert persisted["install_state"] == "failed"
    assert persisted["install_error"] == INSTALL_FAILED_NO_PROGRESS


def test_local_bootstrap_lazy_stall_with_live_thread_stays_in_flight(settings_env):
    settings_env(_settings_config())
    _write_local_status("verifying", last_progress_at=_old_progress_iso())
    with local_bootstrap._INSTALL_LOCK:
        local_bootstrap._INSTALL_THREADS[LOCAL_MODEL] = _FakeThread()

    payload = local_bootstrap.get_state(LOCAL_MODEL)

    assert payload["install_state"] == "verifying"
    assert payload["install_error"] is None


def test_mlx_availability_ignores_install_state_when_snapshot_missing(
    settings_env, monkeypatch
):
    settings_env(_settings_config())
    _write_local_status("downloading", last_progress_at=_fresh_progress_iso())
    monkeypatch.setattr(local_bootstrap, "_is_mlx_backend", lambda: True)
    monkeypatch.setattr(
        local_bootstrap.mlx_install,
        "inspect_readiness",
        lambda _model: _mlx_readiness(
            install_state="downloading",
            model_installed=False,
            snapshot_installed=False,
        ),
    )
    monkeypatch.setattr(
        memory.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=32 * 1024**3, total=32 * 1024**3),
    )

    payload = local_bootstrap.get_availability_payload(QWEN_35_9B)

    assert payload["available"] is False
    assert payload["model_present"] is False
    assert payload["reason"] == "local model files are not installed"


def test_mlx_bootstrap_lazy_stall_with_live_thread_stays_in_flight(
    settings_env, monkeypatch
):
    settings_env(_settings_config())
    _write_local_status("downloading", last_progress_at=_old_progress_iso())
    monkeypatch.setattr(local_bootstrap, "_is_mlx_backend", lambda: True)
    with local_bootstrap._INSTALL_LOCK:
        local_bootstrap._INSTALL_THREADS[QWEN_35_9B] = _FakeThread()

    payload = local_bootstrap.get_state(QWEN_35_9B)

    assert payload["install_state"] == "downloading"
    assert payload["install_error"] is None


def test_mlx_bootstrap_lazy_stall_without_live_thread_fails(settings_env, monkeypatch):
    settings_env(_settings_config())
    _write_local_status("downloading", last_progress_at=_old_progress_iso())
    monkeypatch.setattr(local_bootstrap, "_is_mlx_backend", lambda: True)

    payload = local_bootstrap.get_state(QWEN_35_9B)

    assert payload["install_state"] == "failed"
    assert payload["install_error"] == INSTALL_FAILED_NO_PROGRESS


@pytest.mark.parametrize("state", ["installed", "failed"])
def test_local_bootstrap_restart_terminal_states_have_no_bytes(settings_env, state):
    settings_env(_settings_config())
    _write_local_status(state, error="boom" if state == "failed" else None)

    payload = local_bootstrap.get_state(LOCAL_MODEL)

    assert payload["install_state"] == state
    assert payload["progress_bytes_received"] is None
    assert payload["progress_bytes_total"] is None


def test_local_bootstrap_migrates_preexisting_install_without_worker(
    settings_env, monkeypatch
):
    settings_env(_settings_config())
    monkeypatch.setattr(
        local_bootstrap.local_install,
        "inspect_readiness",
        lambda _model=None: {
            "binary_installed": True,
            "model_installed": True,
            "ram_sufficient": True,
            "binary_path": "/tmp/llama-server",
            "model_path": "/tmp/model.gguf",
        },
    )
    monkeypatch.setattr(local_bootstrap, "_platform_supported", lambda: (True, ""))
    monkeypatch.setattr(
        memory.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=32 * 1024**3, total=32 * 1024**3),
    )
    monkeypatch.setattr(
        local_bootstrap, "_fit_report_for_model", lambda _model: _fit("ok")
    )
    monkeypatch.setattr(
        local_bootstrap.threading,
        "Thread",
        lambda *args, **kwargs: pytest.fail("worker should not be created"),
    )

    assert local_bootstrap.start_bootstrap(LOCAL_MODEL) == (
        {"install_state": "installed"},
        200,
    )
    status = read_install_status(scope="bundled", name="local")
    assert status["install_state"] == "installed"


def test_mlx_start_bootstrap_dispatches_to_mlx_worker(settings_env, monkeypatch):
    settings_env(_settings_config())
    _write_local_status("idle")
    monkeypatch.setattr(local_bootstrap, "_is_mlx_backend", lambda: True)
    monkeypatch.setattr(
        local_bootstrap,
        "get_availability_payload",
        lambda _model: {
            "model": QWEN_35_9B,
            "platform_supported": True,
            "total_memory_gb": 32.0,
            "available_memory_gb": 32.0,
            "min_ram_gb": 13,
            "binary_present": True,
            "model_present": False,
            "available": False,
            "reason": "local model files are not installed",
            "warning": "",
            "download_bytes": 10453446077,
        },
    )
    _FakeThread.init_count = 0
    _FakeThread.start_count = 0
    _FakeThread.targets = []
    monkeypatch.setattr(
        memory.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=100 * 1024**3),
    )
    monkeypatch.setattr(
        local_bootstrap, "_fit_report_for_model", lambda _model: _fit("ok")
    )
    monkeypatch.setattr(local_bootstrap.threading, "Thread", _FakeThread)

    assert local_bootstrap.start_bootstrap(QWEN_35_9B) == (
        {"install_state": "downloading"},
        202,
    )

    assert _FakeThread.targets == [local_bootstrap._mlx_bootstrap_worker]


def test_local_worker_resets_progress_between_binary_and_model(
    settings_env, monkeypatch
):
    settings_env(_settings_config())
    observed = {}
    _write_local_status("downloading", last_progress_at=_fresh_progress_iso())

    def fake_llama_server():
        _write_local_status("installed")

    def fake_install_model(model):
        observed.update(local_bootstrap.get_state(model))
        status = read_install_status(scope="bundled", name="local")
        write_install_status(
            transition_state(status, new_state="installed"),
            scope="bundled",
        )

    monkeypatch.setattr(
        local_bootstrap.local_install, "install_llama_server", fake_llama_server
    )
    monkeypatch.setattr(
        local_bootstrap.local_install, "install_model", fake_install_model
    )
    monkeypatch.setattr(local_bootstrap, "callosum_send", Mock(return_value=True))

    local_bootstrap._run_bootstrap_worker(LOCAL_MODEL)

    assert observed["install_state"] == "downloading"
    assert observed["progress_bytes_total"] is None
    assert observed["progress_bytes_received"] is None


@pytest.mark.parametrize(
    "send_behavior",
    ["success", "false", "raise"],
)
def test_local_worker_success_requests_local_server_start(
    settings_env, monkeypatch, send_behavior
):
    settings_env(_settings_config())
    _write_local_status("downloading", last_progress_at=_fresh_progress_iso())

    def fake_install_model(model):
        assert model == LOCAL_MODEL
        status = read_install_status(scope="bundled", name="local")
        write_install_status(
            transition_state(status, new_state="installed"),
            scope="bundled",
        )

    if send_behavior == "raise":
        callosum_send = Mock(side_effect=RuntimeError("callosum broke"))
    else:
        callosum_send = Mock(return_value=send_behavior == "success")

    monkeypatch.setattr(
        local_bootstrap.local_install, "install_llama_server", lambda: None
    )
    monkeypatch.setattr(
        local_bootstrap.local_install, "install_model", fake_install_model
    )
    monkeypatch.setattr(local_bootstrap, "callosum_send", callosum_send)

    local_bootstrap._run_bootstrap_worker(LOCAL_MODEL)

    callosum_send.assert_called_once_with("supervisor", "start_local")
    status = read_install_status(scope="bundled", name="local")
    assert status["install_state"] == "installed"
    assert status["install_error"] is None


def test_local_worker_install_model_failure_does_not_request_local_server_start(
    settings_env, monkeypatch
):
    settings_env(_settings_config())
    _write_local_status("downloading", last_progress_at=_fresh_progress_iso())
    callosum_send = Mock(return_value=True)

    monkeypatch.setattr(
        local_bootstrap.local_install, "install_llama_server", lambda: None
    )
    monkeypatch.setattr(
        local_bootstrap.local_install,
        "install_model",
        Mock(side_effect=RuntimeError("model download broke")),
    )
    monkeypatch.setattr(local_bootstrap, "callosum_send", callosum_send)

    local_bootstrap._run_bootstrap_worker(LOCAL_MODEL)

    callosum_send.assert_not_called()
    status = read_install_status(scope="bundled", name="local")
    assert status["install_state"] == "failed"
    assert status["install_error"] == "model download broke"


def test_local_worker_cleans_registered_thread(settings_env, monkeypatch):
    settings_env(_settings_config())
    current = threading.current_thread()
    with local_bootstrap._INSTALL_LOCK:
        local_bootstrap._INSTALL_THREADS[LOCAL_MODEL] = current
    _write_local_status("downloading", last_progress_at=_fresh_progress_iso())

    def fake_install_model(_model):
        status = read_install_status(scope="bundled", name="local")
        write_install_status(
            transition_state(status, new_state="installed"),
            scope="bundled",
        )

    monkeypatch.setattr(
        local_bootstrap.local_install, "install_llama_server", lambda: None
    )
    monkeypatch.setattr(
        local_bootstrap.local_install, "install_model", fake_install_model
    )
    monkeypatch.setattr(local_bootstrap, "callosum_send", Mock(return_value=True))

    local_bootstrap._run_bootstrap_worker(LOCAL_MODEL)

    with local_bootstrap._INSTALL_LOCK:
        assert LOCAL_MODEL not in local_bootstrap._INSTALL_THREADS


def test_local_worker_cleans_registered_thread_after_failure(settings_env, monkeypatch):
    settings_env(_settings_config())
    current = threading.current_thread()
    with local_bootstrap._INSTALL_LOCK:
        local_bootstrap._INSTALL_THREADS[LOCAL_MODEL] = current
    _write_local_status("downloading", last_progress_at=_fresh_progress_iso())

    monkeypatch.setattr(
        local_bootstrap.local_install,
        "install_llama_server",
        lambda: (_ for _ in ()).throw(RuntimeError("binary download broke")),
    )

    local_bootstrap._run_bootstrap_worker(LOCAL_MODEL)

    with local_bootstrap._INSTALL_LOCK:
        thread = local_bootstrap._INSTALL_THREADS.get(LOCAL_MODEL)
    assert thread is None or not thread.is_alive()
    status = read_install_status(scope="bundled", name="local")
    assert status["install_state"] == "failed"
    assert status["install_error"] == "binary download broke"


def test_mlx_worker_preserves_install_error_and_cleans_thread(
    settings_env, monkeypatch
):
    settings_env(_settings_config())
    current = threading.current_thread()
    with local_bootstrap._INSTALL_LOCK:
        local_bootstrap._INSTALL_THREADS[QWEN_35_9B] = current
    _write_local_status("downloading", last_progress_at=_fresh_progress_iso())
    monkeypatch.setattr(local_bootstrap, "_is_mlx_backend", lambda: True)

    def fake_install_mlx(_model):
        status = read_install_status(scope="bundled", name="local")
        write_install_status(
            transition_state(status, new_state="failed", error="verify broke"),
            scope="bundled",
        )
        raise local_bootstrap.mlx_install.MLXVerificationError("verify broke")

    monkeypatch.setattr(
        local_bootstrap.mlx_install,
        "install_local_mlx",
        fake_install_mlx,
    )

    local_bootstrap._mlx_bootstrap_worker(QWEN_35_9B)

    with local_bootstrap._INSTALL_LOCK:
        assert QWEN_35_9B not in local_bootstrap._INSTALL_THREADS
    status = read_install_status(scope="bundled", name="local")
    assert status["install_state"] == "failed"
    assert status["install_error"] == "verify broke"
    payload = local_bootstrap.get_state(QWEN_35_9B)
    assert payload["install_state"] == "failed"
    assert payload["install_error"] == "verify broke"


def test_routes_import_registers_local_endpoints(settings_env):
    routes = importlib.import_module("solstone.apps.thinking.routes")
    journal_path, _config = settings_env(_settings_config())
    app = create_app(str(journal_path))
    registered = {rule.rule for rule in app.url_map.iter_rules()}

    assert routes.thinking_bp is not None
    assert "/app/thinking/api/providers/local/status" in registered
    assert "/app/thinking/api/local/availability" in registered
    assert "/app/thinking/api/local/bootstrap" in registered
    assert "/app/thinking/api/local/bootstrap/status" in registered
    assert "/app/thinking/api/local/models" in registered
