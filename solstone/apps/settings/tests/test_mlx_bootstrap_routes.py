# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import importlib
import json
import sys
import threading
import time
from pathlib import Path

import huggingface_hub
import pytest
import tomllib

from solstone.apps.settings import mlx_bootstrap
from solstone.convey import create_app
from solstone.think.providers.mlx import MLX_MODEL_REPO, MLX_MODEL_REVISION


def _client(journal_path):
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def _settings_config() -> dict:
    return {
        "setup": {"completed_at": "2026-05-09T00:00:00Z"},
        "convey": {"trust_localhost": True},
        "providers": {
            "generate": {"provider": "google", "tier": 2, "backup": "anthropic"},
            "cogitate": {"provider": "openai", "tier": 2, "backup": "anthropic"},
            "auth": {"google": "api_key", "openai": "api_key"},
        },
    }


@pytest.fixture(autouse=True)
def _reset_mlx_state(monkeypatch, tmp_path):
    monkeypatch.setattr(mlx_bootstrap, "_STATE", mlx_bootstrap.MlxBootstrapState())
    monkeypatch.setattr(mlx_bootstrap.constants, "HF_HUB_CACHE", str(tmp_path / "hf"))


def _set_state(**updates):
    with mlx_bootstrap._STATE_LOCK:
        for key, value in updates.items():
            setattr(mlx_bootstrap._STATE, key, value)


class _FakeThread:
    init_count = 0
    start_count = 0
    count_lock = threading.Lock()

    def __init__(self, *args, **kwargs):
        with type(self).count_lock:
            type(self).init_count += 1
        self.alive = True

    def start(self):
        with type(self).count_lock:
            type(self).start_count += 1

    def is_alive(self):
        return self.alive


class _DeadThread:
    def is_alive(self):
        return False


def test_mlx_availability_payload_exact_shape(settings_env, monkeypatch):
    journal_path, _config = settings_env(_settings_config())
    monkeypatch.setattr(mlx_bootstrap, "is_mlx_available", lambda: (True, ""))
    monkeypatch.setattr(mlx_bootstrap, "check_model_present", lambda: True)
    monkeypatch.setattr(mlx_bootstrap.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(mlx_bootstrap.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        mlx_bootstrap.psutil,
        "virtual_memory",
        lambda: type("VMem", (), {"total": 32 * 1024**3})(),
    )
    monkeypatch.setattr(mlx_bootstrap, "_is_package_installed", lambda _name: True)
    client = _client(journal_path)

    response = client.get("/app/settings/api/mlx/availability")

    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {
        "is_apple_silicon",
        "total_memory_gb",
        "mlx_installed",
        "model_present",
        "available",
        "reason",
    }
    assert payload == {
        "is_apple_silicon": True,
        "total_memory_gb": 32.0,
        "mlx_installed": True,
        "model_present": True,
        "available": True,
        "reason": "",
    }
    assert all(
        isinstance(payload[key], bool)
        for key in (
            "is_apple_silicon",
            "mlx_installed",
            "model_present",
            "available",
        )
    )
    assert isinstance(payload["total_memory_gb"], float)
    assert isinstance(payload["reason"], str)


def test_bootstrap_post_is_idempotent_while_downloading(settings_env, monkeypatch):
    settings_env(_settings_config())
    monkeypatch.setattr(mlx_bootstrap, "is_mlx_available", lambda: (True, ""))
    present_barrier = threading.Barrier(2)
    present_lock = threading.Lock()
    present_calls = 0

    def fake_check_model_present():
        nonlocal present_calls
        with present_lock:
            present_calls += 1
            call_number = present_calls
        if call_number <= 2:
            present_barrier.wait(timeout=2)
        return False

    monkeypatch.setattr(mlx_bootstrap, "check_model_present", fake_check_model_present)
    real_thread = threading.Thread
    _FakeThread.init_count = 0
    _FakeThread.start_count = 0
    monkeypatch.setattr(mlx_bootstrap.threading, "Thread", _FakeThread)
    call_barrier = threading.Barrier(3)
    results = []
    errors = []
    results_lock = threading.Lock()

    def call_start_bootstrap():
        try:
            call_barrier.wait(timeout=2)
            result = mlx_bootstrap.start_bootstrap()
            with results_lock:
                results.append(result)
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            with results_lock:
                errors.append(exc)

    callers = [
        real_thread(target=call_start_bootstrap),
        real_thread(target=call_start_bootstrap),
    ]
    for caller in callers:
        caller.start()
    call_barrier.wait(timeout=2)
    for caller in callers:
        caller.join(timeout=2)

    assert errors == []
    assert sorted(status for _payload, status in results) == [200, 202]
    assert [payload for payload, _status in results] == [
        {"state": "downloading"},
        {"state": "downloading"},
    ]
    assert _FakeThread.init_count == 1
    assert _FakeThread.start_count == 1


def test_bootstrap_post_already_installed_returns_installed_without_worker(
    settings_env,
    monkeypatch,
):
    journal_path, _config = settings_env(_settings_config())
    monkeypatch.setattr(mlx_bootstrap, "is_mlx_available", lambda: (True, ""))
    monkeypatch.setattr(mlx_bootstrap, "check_model_present", lambda: True)
    monkeypatch.setattr(
        mlx_bootstrap.threading,
        "Thread",
        lambda *args, **kwargs: pytest.fail("worker should not be created"),
    )
    client = _client(journal_path)

    response = client.post("/app/settings/api/mlx/bootstrap")

    assert response.status_code == 200
    assert response.get_json() == {"state": "installed"}


@pytest.mark.parametrize(
    "reason",
    [
        "not running on macOS",
        "not running on Apple Silicon",
        "insufficient RAM (need 16 GB, have 8 GB)",
        "mlx-vlm package not installed",
    ],
)
def test_bootstrap_post_rejects_unqualified_host(settings_env, monkeypatch, reason):
    journal_path, _config = settings_env(_settings_config())
    monkeypatch.setattr(mlx_bootstrap, "is_mlx_available", lambda: (False, reason))
    client = _client(journal_path)

    response = client.post("/app/settings/api/mlx/bootstrap")

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["reason_code"] == "invalid_request_value"
    assert payload["detail"] == reason


def test_bootstrap_status_always_returns_state_bytes_message(settings_env):
    journal_path, _config = settings_env(_settings_config())
    client = _client(journal_path)

    for state in ("idle", "downloading", "verifying", "installed", "failed"):
        _set_state(
            state=state,
            received_bytes=12,
            total_bytes=24,
            message="bad" if state == "failed" else None,
            thread=_FakeThread() if state in ("downloading", "verifying") else None,
            started_at=time.monotonic(),
            last_progress_at=time.monotonic(),
        )
        response = client.get("/app/settings/api/mlx/bootstrap/status")
        assert response.status_code == 200
        payload = response.get_json()
        assert set(payload) == {"state", "received_bytes", "total_bytes", "message"}
        assert payload["state"] == state
        assert isinstance(payload["received_bytes"], int)
        assert isinstance(payload["total_bytes"], int)
        assert payload["message"] == ("bad" if state == "failed" else None)


def test_bootstrap_status_transitions_stalled_download_to_failed(settings_env):
    journal_path, _config = settings_env(_settings_config())
    _set_state(
        state="downloading",
        thread=_DeadThread(),
        started_at=time.monotonic() - 90,
        last_progress_at=time.monotonic() - 90,
    )
    client = _client(journal_path)

    payload = client.get("/app/settings/api/mlx/bootstrap/status").get_json()

    assert payload["state"] == "failed"
    assert "stalled" in payload["message"] or "no progress" in payload["message"]


def test_bootstrap_status_transitions_stalled_verifying_to_failed(settings_env):
    journal_path, _config = settings_env(_settings_config())
    _set_state(
        state="verifying",
        thread=_DeadThread(),
        started_at=time.monotonic() - 90,
        last_progress_at=time.monotonic() - 90,
    )
    client = _client(journal_path)

    payload = client.get("/app/settings/api/mlx/bootstrap/status").get_json()

    assert payload["state"] == "failed"
    assert "stalled" in payload["message"] or "no progress" in payload["message"]


def test_routes_import_without_mlx_vlm_registers_mlx_endpoints(
    monkeypatch, settings_env
):
    monkeypatch.setitem(sys.modules, "mlx_vlm", None)
    routes = importlib.import_module("solstone.apps.settings.routes")
    journal_path, _config = settings_env(_settings_config())
    app = create_app(str(journal_path))
    registered = {rule.rule for rule in app.url_map.iter_rules()}

    assert routes.settings_bp is not None
    assert "/app/settings/api/mlx/availability" in registered
    assert "/app/settings/api/mlx/bootstrap" in registered
    assert "/app/settings/api/mlx/bootstrap/status" in registered


def _write_snapshot(tmp_path: Path, monkeypatch, files: dict[str, bytes]) -> Path:
    monkeypatch.setattr(mlx_bootstrap.constants, "HF_HUB_CACHE", str(tmp_path / "hf"))
    snapshot_dir = mlx_bootstrap._snapshot_dir()
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {f"w{i}": path for i, path in enumerate(files)}}),
        encoding="utf-8",
    )
    for rel_path, content in files.items():
        file_path = snapshot_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
    return snapshot_dir


def test_model_present_requires_index_and_all_safetensors(tmp_path, monkeypatch):
    _write_snapshot(
        tmp_path,
        monkeypatch,
        {
            "model-00001-of-00002.safetensors": b"one",
            "model-00002-of-00002.safetensors": b"two",
        },
    )

    assert mlx_bootstrap.check_model_present() is True

    (mlx_bootstrap._snapshot_dir() / "model-00002-of-00002.safetensors").unlink()
    assert mlx_bootstrap.check_model_present() is False


def test_snapshot_download_called_without_resume_download(monkeypatch):
    calls = {}

    def fake_snapshot_download(**kwargs):
        calls.update(kwargs)

    _set_state(state="downloading", thread=threading.current_thread())
    monkeypatch.setattr(
        mlx_bootstrap.huggingface_hub, "snapshot_download", fake_snapshot_download
    )
    monkeypatch.setattr(
        mlx_bootstrap, "_verify_safetensors_sha256_hashes", lambda: None
    )

    mlx_bootstrap._run_bootstrap_worker()

    assert calls["repo_id"] == MLX_MODEL_REPO
    assert calls["revision"] == MLX_MODEL_REVISION
    assert "resume_download" not in calls


def test_hfapi_list_repo_tree_called_with_pinned_revision(tmp_path, monkeypatch):
    _write_snapshot(tmp_path, monkeypatch, {"model.safetensors": b"abc"})
    expected = hashlib.sha256(b"abc").hexdigest()
    calls = {}

    class FakeApi:
        def list_repo_tree(self, **kwargs):
            calls.update(kwargs)
            return [
                huggingface_hub.RepoFile(
                    path="model.safetensors",
                    size=3,
                    oid="oid",
                    lfs={"size": 3, "oid": expected, "pointerSize": 123},
                )
            ]

    monkeypatch.setattr(mlx_bootstrap.huggingface_hub, "HfApi", lambda: FakeApi())
    _set_state(state="verifying")

    mlx_bootstrap._verify_safetensors_sha256_hashes()

    assert calls["repo_id"] == MLX_MODEL_REPO
    assert calls["revision"] == MLX_MODEL_REVISION
    assert calls["recursive"] is True


def test_worker_enters_verifying_state_between_download_and_install(monkeypatch):
    entered_verify = threading.Event()
    release_verify = threading.Event()

    def slow_verify():
        entered_verify.set()
        release_verify.wait(timeout=2)

    _set_state(state="downloading", thread=None)
    monkeypatch.setattr(
        mlx_bootstrap.huggingface_hub, "snapshot_download", lambda **_kwargs: None
    )
    monkeypatch.setattr(mlx_bootstrap, "_verify_safetensors_sha256_hashes", slow_verify)
    worker = threading.Thread(target=mlx_bootstrap._run_bootstrap_worker)
    _set_state(thread=worker)

    worker.start()
    assert entered_verify.wait(timeout=2)
    assert mlx_bootstrap.get_state()["state"] == "verifying"
    release_verify.set()
    worker.join(timeout=2)
    assert mlx_bootstrap.get_state()["state"] == "installed"


def test_worker_verify_mismatch_transitions_to_failed_with_filename(
    tmp_path, monkeypatch
):
    snapshot_dir = _write_snapshot(tmp_path, monkeypatch, {"model.safetensors": b"abc"})

    class FakeApi:
        def list_repo_tree(self, **kwargs):
            return [
                huggingface_hub.RepoFile(
                    path="model.safetensors",
                    size=3,
                    oid="oid",
                    lfs={"size": 3, "oid": "0" * 64, "pointerSize": 123},
                )
            ]

    _set_state(state="downloading", thread=threading.current_thread())
    monkeypatch.setattr(
        mlx_bootstrap.huggingface_hub, "snapshot_download", lambda **_kwargs: None
    )
    monkeypatch.setattr(mlx_bootstrap.huggingface_hub, "HfApi", lambda: FakeApi())

    mlx_bootstrap._run_bootstrap_worker()
    payload = mlx_bootstrap.get_state()

    assert payload["state"] == "failed"
    assert "model.safetensors" in payload["message"]
    assert (snapshot_dir / "model.safetensors").is_file()


def test_worker_exception_sets_failed_message(monkeypatch):
    _set_state(state="downloading", thread=threading.current_thread())
    monkeypatch.setattr(
        mlx_bootstrap.huggingface_hub,
        "snapshot_download",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("download broke")),
    )

    mlx_bootstrap._run_bootstrap_worker()

    payload = mlx_bootstrap.get_state()
    assert payload["state"] == "failed"
    assert "download broke" in payload["message"]


def test_pyproject_declares_huggingface_hub_top_level_dependency():
    pyproject_path = Path(__file__).resolve().parents[4] / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    matches = [dep for dep in deps if dep.startswith("huggingface-hub")]

    assert matches
    assert all(";" not in dep for dep in matches)
