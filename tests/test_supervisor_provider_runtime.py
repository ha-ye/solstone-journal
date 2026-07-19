# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ast
import asyncio
import concurrent.futures
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from solstone.think import supervisor
from solstone.think.providers.artifact_proof import ReadinessOutcome
from solstone.think.providers.install_state import InstallState
from solstone.think.providers.runtime_health import (
    RUNTIME_PHASES,
    ReasonCode,
    RuntimeHealthRecord,
    RuntimePhase,
    read_retry_token,
    read_runtime_health,
    request_retry_token,
    write_runtime_health,
)


@pytest.fixture
def provider_cache_reset() -> Iterator[None]:
    from solstone.think.providers import local_server, local_vulkan

    local_vulkan.reset_detect_cache()
    local_server.reset_parallel_slots_cache()
    try:
        yield
    finally:
        local_vulkan.reset_detect_cache()
        local_server.reset_parallel_slots_cache()


@pytest.fixture(autouse=True)
def runtime_state_reset(
    tmp_path, monkeypatch, provider_cache_reset, set_test_journal_path
):
    import solstone.think.utils as think_utils

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    think_utils._journal_path_cache = None
    states = {
        "local": supervisor.ProviderRuntimeState("local"),
        "parakeet": supervisor.ProviderRuntimeState("parakeet"),
    }
    monkeypatch.setattr(supervisor, "_provider_runtime_states", states)
    monkeypatch.setattr(
        supervisor,
        "_recovery_state",
        {
            "local": supervisor.ProviderRecoveryState(),
            "parakeet": supervisor.ProviderRecoveryState(),
        },
    )
    monkeypatch.setattr(supervisor, "_provider_runtime_executor", None)
    monkeypatch.setattr(
        supervisor,
        "_wedge_state",
        {
            "providers": OrderedDict(),
            "failures": set(),
            "cooldown_until": 0.0,
            "awaiting_recovery": False,
        },
    )
    monkeypatch.setattr(supervisor, "_provider_startup_gate", None)
    monkeypatch.setattr(supervisor, "_parakeet_admission_retry_epoch", 0)
    supervisor._SERVICE_STATE.clear()
    yield
    executor = supervisor._provider_runtime_executor
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)
    think_utils._journal_path_cache = None


class _InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


def _future_with(result: Any) -> concurrent.futures.Future:
    future: concurrent.futures.Future = concurrent.futures.Future()
    future.set_result(result)
    return future


class _FakeTaskQueue:
    def __init__(self) -> None:
        self.ready_calls = 0

    def set_ready(self) -> None:
        self.ready_calls += 1


class _FakeReservation:
    def __init__(self, port: int = 45123):
        self.port = port
        self.closed = False

    def release_for_spawn(self) -> int:
        self.closed = True
        return self.port

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    pid = 12345
    returncode = None

    def poll(self) -> None:
        return None


class _FakeManaged:
    def __init__(self, name: str = supervisor.LOCAL_SERVER_PROCESS_NAME) -> None:
        self.name = name
        self.ref = f"ref-{name}"
        self.process = _FakeProcess()
        self.cleanup = MagicMock()
        self.terminate = MagicMock()

    def is_running(self) -> bool:
        return True


class _DeadManaged(_FakeManaged):
    def is_running(self) -> bool:
        return False


def _hold_local_slot_in_child(root: Path):
    ready = root / "slot-holder.ready"
    code = (
        "import fcntl, pathlib, sys, time\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "ready = pathlib.Path(sys.argv[2])\n"
        "root.mkdir(parents=True, exist_ok=True)\n"
        "f = open(root / 'slot-0.lock', 'a+', encoding='utf-8')\n"
        "fcntl.flock(f, fcntl.LOCK_EX)\n"
        "ready.write_text('1', encoding='utf-8')\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(root), str(ready)],
    )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if ready.exists():
            return proc
        time.sleep(0.01)
    proc.terminate()
    proc.wait(timeout=2)
    raise AssertionError("local admission slot holder did not become ready")


def _readiness(
    status: str,
    reason_code: str,
    *,
    install_state: InstallState = "idle",
    host: dict[str, Any] | None = None,
) -> ReadinessOutcome:
    return ReadinessOutcome(
        provider="parakeet",
        status=status,
        reason_code=reason_code,
        target={},
        install={"install_state": install_state},
        host=host or {},
        artifacts={},
        proof={},
    )


def _local_readiness(
    status: str = "ready",
    reason_code: str = "ready",
    *,
    install_state: InstallState = "idle",
    host_reason: str = "gpu_unavailable",
) -> ReadinessOutcome:
    return ReadinessOutcome(
        provider="local",
        status=status,
        reason_code=reason_code,
        target={"model_id": supervisor.LOCAL_MODEL},
        install={"install_state": install_state},
        host=(
            {
                "backend": "vulkan",
                "backend_reason": "test",
            }
            if status == "ready"
            else {"reason": host_reason}
        ),
        artifacts=(
            {
                "binary_path": "/tmp/llama-server",
                "model_path": "/tmp/model.gguf",
                "mmproj_path": None,
            }
            if status == "ready"
            else {}
        ),
        proof={},
    )


def _local_plan() -> supervisor.LocalServerLaunchPlan:
    return supervisor.LocalServerLaunchPlan(
        backend="vulkan",
        desired_fingerprint_json='{"provider":"local"}',
        desired_fingerprint_sha256="fp-local",
        binary_path=Path("/tmp/llama-server"),
        model_path=Path("/tmp/model.gguf"),
        context_tokens=16384,
        parallel_slots=1,
        prompt_cache_mib=0,
        env_updates={"GGML_VK_VISIBLE_DEVICES": "0"},
    )


def _cuda_plan() -> supervisor.LocalServerLaunchPlan:
    return supervisor.LocalServerLaunchPlan(
        backend="cuda",
        desired_fingerprint_json='{"provider":"local","backend":"cuda"}',
        desired_fingerprint_sha256="fp-local-cuda",
        binary_path=Path("/tmp/llama-server"),
        model_path=Path("/tmp/model.gguf"),
        lib_dir=Path("/tmp/cuda/lib"),
        gpu_index=0,
        gpu_vram_mib=24576,
        context_tokens=32768,
        parallel_slots=1,
        prompt_cache_mib=0,
        visible_devices_env="CUDA_VISIBLE_DEVICES",
        visible_devices_value="0",
        env_updates={"CUDA_VISIBLE_DEVICES": "0", "LD_LIBRARY_PATH": "/tmp/cuda/lib"},
    )


def _mlx_plan() -> supervisor.LocalServerLaunchPlan:
    return supervisor.LocalServerLaunchPlan(
        backend="mlx",
        desired_fingerprint_json='{"provider":"local","backend":"mlx"}',
        desired_fingerprint_sha256="fp-local-mlx",
        model_id=supervisor.LOCAL_MODEL,
        runtime_dir=Path("/tmp/mlx-runtime"),
    )


def _parakeet_plan(backend: str = "cpu") -> supervisor.ParakeetServerLaunchPlan:
    return supervisor.ParakeetServerLaunchPlan(
        binary_backend=backend,
        env_updates={"GGML_VK_VISIBLE_DEVICES": "0"} if backend == "vulkan" else {},
        gpu_index=0 if backend == "vulkan" else None,
        binary_path=Path(f"/tmp/parakeet-{backend}"),
        model_path=Path("/tmp/parakeet-model.bin"),
        threads=4,
        library_dirs=(),
        desired_fingerprint_json='{"provider":"parakeet"}',
        desired_fingerprint_sha256="fp-parakeet",
        placement="gpu" if backend == "vulkan" else "cpu",
    )


def _runtime_record(
    provider: str,
    *,
    phase: RuntimePhase,
    fingerprint: str | None,
    generation: int,
    attempt: int,
    process: dict[str, Any] | None = None,
) -> RuntimeHealthRecord:
    record = read_runtime_health(provider)
    return {
        **record,
        "phase": phase,
        "reason_code": None,
        "detail": {},
        "desired_fingerprint_sha256": fingerprint,
        "incarnation": supervisor._PROVIDER_INCARNATION,
        "generation": generation,
        "attempt": attempt,
        "process": process,
        "updated_at": "2026-07-19T00:00:00+00:00",
        "owner": {"test": "provider-runtime"},
    }


@pytest.mark.parametrize(
    ("status", "expected_phase", "expected_reason"),
    [
        ("missing-or-mismatched", "artifact-not-ready", "artifact-missing"),
        (
            "proof-unavailable",
            "state-unavailable",
            "proof-observation-unavailable",
        ),
    ],
)
def test_readiness_block_table_maps_artifact_and_proof_statuses(
    status: str,
    expected_phase: RuntimePhase,
    expected_reason: ReasonCode,
) -> None:
    observation = supervisor._readiness_block_observation(
        provider="parakeet",
        readiness=_readiness(status, "manifest_missing"),
        fingerprint_json='{"provider":"parakeet"}',
        fingerprint_sha256_value="fp-parakeet",
        boot_required=True,
    )

    assert observation is not None
    assert observation.phase == expected_phase
    assert observation.reason_code == expected_reason


@pytest.mark.parametrize(
    ("readiness_reason", "expected_reason"),
    [
        ("platform_unsupported", "platform-unsupported"),
        ("package_unavailable", "package-unavailable"),
        ("ram_insufficient", "ram-insufficient"),
        ("gpu_probe_failed", "gpu-probe-failed"),
        ("gpu_unavailable", "gpu-unavailable"),
    ],
)
def test_host_ineligible_reason_table(readiness_reason: str, expected_reason: str):
    observation = supervisor._readiness_block_observation(
        provider="local",
        readiness=_readiness(
            "host-ineligible",
            readiness_reason,
            host={"reason": readiness_reason},
        ),
        fingerprint_json='{"provider":"local"}',
        fingerprint_sha256_value="fp-local",
        boot_required=True,
    )

    assert observation is not None
    assert observation.phase == "host-blocked"
    assert observation.reason_code == expected_reason


@pytest.mark.parametrize(
    "install_state",
    [
        "idle",
        "resolving",
        "downloading",
        "verifying",
        "installing",
        "installed",
        "failed",
    ],
)
def test_parakeet_missing_artifact_table_permits_acquisition_only_when_idle(
    install_state: InstallState,
) -> None:
    observation = supervisor._readiness_block_observation(
        provider="parakeet",
        readiness=_readiness(
            "missing-or-mismatched",
            "manifest_missing",
            install_state=install_state,
        ),
        fingerprint_json='{"provider":"parakeet"}',
        fingerprint_sha256_value="fp-parakeet",
        boot_required=True,
    )

    assert observation is not None
    assert observation.phase == "artifact-not-ready"
    assert observation.detail["install_acquisition_allowed"] is (
        install_state == "idle"
    )


@pytest.mark.parametrize("active_provider", ["local", "cloud", None])
@pytest.mark.parametrize("endpoint_bundled", [True, False])
@pytest.mark.parametrize(
    ("readiness_status", "readiness_reason", "expected_phase"),
    [
        ("ready", "ready", "starting"),
        ("missing-or-mismatched", "manifest_missing", "artifact-not-ready"),
        (
            "proof-unavailable",
            "proof_cache_unavailable",
            "state-unavailable",
        ),
        ("host-ineligible", "gpu_unavailable", "host-blocked"),
    ],
)
def test_local_desired_state_table(
    monkeypatch,
    active_provider: str | None,
    endpoint_bundled: bool,
    readiness_status: str,
    readiness_reason: str,
    expected_phase: RuntimePhase,
) -> None:
    from solstone.think.providers import local_install, local_server, local_vulkan

    config = {"providers": {"active": {"provider": active_provider}}}
    inspect_calls = 0

    def inspect_readiness(_model_id):
        nonlocal inspect_calls
        inspect_calls += 1
        return _local_readiness(readiness_status, readiness_reason)

    monkeypatch.setattr(supervisor, "_is_remote_mode", False)
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor, "read_journal_config", lambda: config)
    monkeypatch.setattr(
        supervisor,
        "is_local_provider_needed",
        lambda config_arg=None: active_provider == "local",
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.resolve_local_endpoint",
        lambda: type("Endpoint", (), {"is_bundled": endpoint_bundled})(),
    )
    monkeypatch.setattr(
        local_install,
        "target_fingerprint",
        lambda _model_id: {"provider": "local", "target": "one"},
    )
    monkeypatch.setattr(local_install, "inspect_readiness", inspect_readiness)
    monkeypatch.setattr(local_install, "gpu_device_override", lambda: None)
    monkeypatch.setattr(
        local_vulkan,
        "detect_gpus",
        lambda: [
            local_vulkan.VulkanDevice(0, "GPU", local_vulkan.VK_TYPE_DISCRETE, 12288)
        ],
    )
    monkeypatch.setattr(
        local_vulkan, "select_device", lambda devices, **_kw: devices[0]
    )
    monkeypatch.setattr(local_vulkan, "device_local_used_mib", lambda _index: 0)
    monkeypatch.setattr(local_server, "select_server_tier", lambda _vram: _FakeTier())

    observation = supervisor._observe_local_provider_truth()

    if active_provider != "local" or not endpoint_bundled:
        assert observation.phase == "not-desired"
        assert observation.reason_code == "provider-not-needed"
        assert inspect_calls == 0
    else:
        assert observation.phase == expected_phase
        assert inspect_calls == 1


def test_local_desired_state_table_remote_mode_skips_bundled_observation(
    monkeypatch,
) -> None:
    from solstone.think.providers import local_install

    monkeypatch.setattr(supervisor, "_is_remote_mode", True)
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model_id: pytest.fail("remote mode must not inspect bundled local"),
    )

    observation = supervisor._observe_local_provider_truth()

    assert observation.phase == "not-desired"
    assert observation.reason_code == "provider-not-needed"
    assert observation.detail["remote_mode"] is True


def test_local_desired_state_table_darwin_uses_mlx_observation(monkeypatch) -> None:
    from solstone.think.providers import local_install, mlx_install

    monkeypatch.setattr(supervisor, "_is_remote_mode", False)
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(supervisor, "read_journal_config", lambda: {})
    monkeypatch.setattr(
        supervisor, "is_local_provider_needed", lambda _config=None: True
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.resolve_local_endpoint",
        lambda: type("Endpoint", (), {"is_bundled": True})(),
    )
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model_id: pytest.fail("darwin local observation must use MLX"),
    )
    monkeypatch.setattr(
        mlx_install,
        "target_fingerprint",
        lambda: {"provider": "local", "backend": "mlx"},
    )
    monkeypatch.setattr(
        mlx_install,
        "inspect_readiness",
        lambda: ReadinessOutcome(
            provider="local",
            status="ready",
            reason_code="ready",
            target={"model_id": supervisor.LOCAL_MODEL},
            install={"install_state": "idle"},
            host={"backend": "mlx"},
            artifacts={"runtime_dir": "/tmp/mlx-runtime"},
            proof={},
        ),
    )

    observation = supervisor._observe_local_provider_truth()

    assert observation.phase == "starting"
    assert observation.plan is not None
    assert observation.plan.backend == "mlx"


@pytest.mark.parametrize(
    (
        "remote_mode",
        "platform_can_host",
        "latch",
        "expected_phase",
        "expected_reason",
        "readiness_calls",
    ),
    [
        (
            True,
            True,
            None,
            "not-desired",
            "provider-not-needed",
            0,
        ),
        (
            False,
            False,
            None,
            "not-desired",
            "provider-not-needed",
            0,
        ),
        (
            False,
            True,
            {
                "desired": False,
                "blocked": False,
                "reason_code": "provider-not-needed",
            },
            "not-desired",
            "provider-not-needed",
            0,
        ),
        (
            False,
            True,
            {
                "desired": False,
                "blocked": True,
                "reason_code": "host-admission-blocked",
            },
            "host-blocked",
            "host-admission-blocked",
            0,
        ),
        (
            False,
            True,
            {
                "desired": True,
                "blocked": False,
                "reason_code": "provider-not-needed",
            },
            "starting",
            "launch-requested",
            1,
        ),
    ],
)
def test_parakeet_desired_state_table_remote_platform_and_stt(
    monkeypatch,
    remote_mode: bool,
    platform_can_host: bool,
    latch: dict[str, Any] | None,
    expected_phase: RuntimePhase,
    expected_reason: ReasonCode,
    readiness_calls: int,
) -> None:
    from solstone.think.providers import parakeet_install

    calls = 0
    monkeypatch.setattr(supervisor, "_is_remote_mode", remote_mode)
    monkeypatch.setattr(
        supervisor, "_parakeet_platform_can_host", lambda: platform_can_host
    )
    monkeypatch.setattr(supervisor, "read_journal_config", lambda: {"transcribe": {}})
    monkeypatch.setattr(
        "solstone.think.services.spp.confidential_provenance",
        lambda: None,
    )
    monkeypatch.setattr(
        supervisor,
        "_parakeet_stt_admission_latch",
        lambda _transcribe, _confidential: latch,
    )
    monkeypatch.setattr(
        parakeet_install,
        "target_fingerprint",
        lambda *, journal_path=None: {"provider": "parakeet", "target": "one"},
    )

    def inspect_readiness(_journal_path=None):
        nonlocal calls
        calls += 1
        return ReadinessOutcome(
            provider="parakeet",
            status="ready",
            reason_code="ready",
            target={},
            install={"install_state": "idle"},
            host={},
            artifacts={
                "binary_path_cpu": "/tmp/parakeet-cpu",
                "binary_path_vulkan": "/tmp/parakeet-vulkan",
                "model_path": "/tmp/parakeet-model.bin",
            },
            proof={},
        )

    monkeypatch.setattr(parakeet_install, "inspect_readiness", inspect_readiness)
    monkeypatch.setattr(supervisor, "_configured_parakeet_device", lambda: "cpu")
    monkeypatch.setattr(
        supervisor,
        "_resolve_parakeet_backend",
        lambda _device, _selected: ("cpu", {}, None),
    )
    monkeypatch.setattr(supervisor, "parakeet_physical_thread_count", lambda: 4)
    monkeypatch.setattr(supervisor, "_parakeet_runtime_library_dirs", lambda: [])

    observation = supervisor._observe_parakeet_provider_truth()

    assert observation.phase == expected_phase
    assert observation.reason_code == expected_reason
    assert calls == readiness_calls


class _FakeTier:
    name = "floor"
    context_tokens = 16384
    parallel_slots = 1
    prompt_cache_mib = 0


def test_inactive_local_projection_publishes_not_desired_without_attempt(
    monkeypatch,
) -> None:
    observation = supervisor.ProviderTruthObservation(
        provider="local",
        phase="not-desired",
        reason_code="provider-not-needed",
        detail={"projection": {"status": "reconciling"}},
    )
    starts: list[int] = []
    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(
        supervisor, "_observe_local_provider_truth", lambda: observation
    )
    monkeypatch.setattr(
        supervisor,
        "_provider_start_worker",
        lambda provider, plan_arg, fence, _cancel_event: starts.append(fence.attempt),
    )

    asyncio.run(supervisor._reconcile_local_provider_runtime([]))
    asyncio.run(supervisor._reconcile_local_provider_runtime([]))

    state = supervisor._provider_runtime_states["local"]
    record = read_runtime_health("local")
    assert state.latest_phase == "not-desired"
    assert state.retry.attempt_count == 0
    assert starts == []
    assert record["phase"] == "not-desired"
    assert record["detail"]["projection"]["status"] == "reconciling"


def test_byo_local_performs_no_bundled_observation(monkeypatch):
    from solstone.think.providers import local_install

    monkeypatch.setattr(supervisor, "_is_remote_mode", False)
    monkeypatch.setattr(supervisor, "read_journal_config", lambda: {})
    monkeypatch.setattr(
        supervisor, "is_local_provider_needed", lambda _config=None: True
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.resolve_local_endpoint",
        lambda: type("Endpoint", (), {"is_bundled": False})(),
    )
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model_id: pytest.fail(
            "BYO endpoint must not inspect bundled readiness"
        ),
    )

    observation = supervisor._observe_local_provider_truth()

    assert observation.phase == "not-desired"
    assert observation.detail["projection"]["status"] == "reconciling"


def test_truth_observation_runs_off_tick_and_single_flight(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def slow_observer():
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=5)
        return supervisor.ProviderTruthObservation(
            provider="local",
            phase="not-desired",
            reason_code="provider-not-needed",
            detail={},
        )

    monkeypatch.setattr(supervisor, "_observe_local_provider_truth", slow_observer)

    asyncio.run(supervisor._reconcile_local_provider_runtime([]))

    assert started.wait(timeout=1)
    state = supervisor._provider_runtime_states["local"]
    assert state.truth_future is not None
    assert not state.truth_future.done()

    asyncio.run(supervisor._reconcile_local_provider_runtime([]))

    assert calls == 1
    release.set()
    state.truth_future.result(timeout=1)
    asyncio.run(supervisor._reconcile_local_provider_runtime([]))

    assert state.latest_phase == "not-desired"
    assert read_runtime_health("local")["phase"] == "not-desired"


def test_no_op_tick_waits_for_truth_cadence_and_backoff_deadline(monkeypatch) -> None:
    plan = _local_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "backoff"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry = supervisor.ProviderRetryState(
        attempt_count=1,
        next_at=200.0,
        desired_fingerprint=plan.desired_fingerprint_sha256,
    )
    state.next_truth_at = 160.0

    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        supervisor,
        "_provider_start_worker",
        lambda *_args: pytest.fail("backoff deadline has not arrived"),
    )
    monkeypatch.setattr(
        supervisor,
        "_observe_local_provider_truth",
        lambda: pytest.fail("truth cadence has not arrived"),
    )

    asyncio.run(supervisor._reconcile_local_provider_runtime([]))

    assert state.start_future is None
    assert state.truth_future is None
    assert state.latest_phase == "backoff"


def test_start_worker_is_single_flight(monkeypatch) -> None:
    plan = _local_plan()
    pending: concurrent.futures.Future = concurrent.futures.Future()
    submitted = 0
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "starting"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.desired_fingerprint = plan.desired_fingerprint_sha256
    state.next_truth_at = 9999.0

    class _PendingExecutor:
        def submit(self, *_args, **_kwargs):
            nonlocal submitted
            submitted += 1
            return pending

    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _PendingExecutor())

    asyncio.run(supervisor._reconcile_local_provider_runtime([]))
    asyncio.run(supervisor._reconcile_local_provider_runtime([]))

    assert submitted == 1
    assert state.start_future is pending
    assert state.retry.attempt_count == 1


def test_ready_episode_reobserves_on_sixty_second_cadence(monkeypatch) -> None:
    now = 100.0
    observations = 0
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "ready"
    state.desired_fingerprint = "fp-local"
    state.next_truth_at = now + supervisor.PROVIDER_STABLE_READY_REFRESH_SECONDS

    def monotonic() -> float:
        return now

    def observe():
        nonlocal observations
        observations += 1
        return supervisor.ProviderTruthObservation(
            provider="local",
            phase="ready",
            reason_code="ready-existing-owned-process",
            detail={},
            desired_fingerprint_sha256="fp-local",
            boot_required=True,
        )

    monkeypatch.setattr(supervisor.time, "monotonic", monotonic)
    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(supervisor, "_observe_local_provider_truth", observe)

    asyncio.run(supervisor._reconcile_local_provider_runtime([]))

    assert observations == 0

    now = 160.0
    asyncio.run(supervisor._reconcile_local_provider_runtime([]))
    asyncio.run(supervisor._reconcile_local_provider_runtime([]))

    assert observations == 1
    assert state.latest_phase == "ready"


def test_retry_token_resets_live_target_without_launching(monkeypatch) -> None:
    plan = _local_plan()
    observations = 0
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "ready"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry = supervisor.ProviderRetryState(
        attempt_count=4,
        next_at=9999.0,
        desired_fingerprint=plan.desired_fingerprint_sha256,
    )
    state.next_truth_at = 9999.0
    managed = _FakeManaged()
    request_retry_token(
        "local",
        desired_fingerprint_sha256=plan.desired_fingerprint_sha256,
        owner={"test": "retry"},
    )
    monkeypatch.setattr(
        supervisor,
        "_provider_start_worker",
        lambda *_args: pytest.fail("live retry token must not duplicate launch"),
    )

    def observe():
        nonlocal observations
        observations += 1
        return supervisor.ProviderTruthObservation(
            provider="local",
            phase="ready",
            reason_code="ready-existing-owned-process",
            detail={},
            desired_fingerprint_sha256=plan.desired_fingerprint_sha256,
            boot_required=True,
        )

    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(supervisor, "_observe_local_provider_truth", observe)

    asyncio.run(supervisor._reconcile_local_provider_runtime([managed]))
    asyncio.run(supervisor._reconcile_local_provider_runtime([managed]))

    assert state.retry.attempt_count == 0
    assert state.latest_phase == "stopped"
    assert observations == 1


@pytest.mark.parametrize(
    "observation",
    [
        supervisor.ProviderTruthObservation(
            provider="local",
            phase="starting",
            reason_code="launch-requested",
            detail={},
            desired_fingerprint_json='{"provider":"local","target":"new"}',
            desired_fingerprint_sha256="fp-new",
            plan=_local_plan(),
            boot_required=True,
        ),
        supervisor.ProviderTruthObservation(
            provider="local",
            phase="not-desired",
            reason_code="provider-not-needed",
            detail={},
        ),
        supervisor.ProviderTruthObservation(
            provider="local",
            phase="state-corrupt",
            reason_code="record-malformed",
            detail={},
            desired_fingerprint_sha256="fp-local",
            boot_required=True,
        ),
        supervisor.ProviderTruthObservation(
            provider="local",
            phase="state-unavailable",
            reason_code="record-unavailable",
            detail={},
            desired_fingerprint_sha256="fp-local",
            boot_required=True,
        ),
    ],
)
def test_truth_change_signals_pending_start_cancel_event(
    observation: supervisor.ProviderTruthObservation,
) -> None:
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "starting"
    state.desired_fingerprint = "fp-local"
    state.retry.desired_fingerprint = "fp-local"
    state.start_fence = supervisor._provider_fence(state, 0)
    state.start_future = concurrent.futures.Future()
    state.start_cancel_event = threading.Event()
    state.truth_fence = supervisor._provider_fence(state, 0)
    state.truth_future = _future_with(observation)

    assert supervisor._handle_provider_truth_result(state) is True

    assert state.start_cancel_event is not None
    assert state.start_cancel_event.is_set()


@pytest.mark.parametrize(
    ("provider", "phase", "plan", "observation"),
    [
        (
            "local",
            "starting",
            _cuda_plan(),
            supervisor.ProviderTruthObservation(
                provider="local",
                phase="host-blocked",
                reason_code="gpu-unavailable",
                detail={"host": {"reason": "transient cuda pressure"}},
                desired_fingerprint_sha256="fp-local-cuda",
                boot_required=True,
            ),
        ),
        (
            "parakeet",
            "starting",
            _parakeet_plan("cpu"),
            supervisor.ProviderTruthObservation(
                provider="parakeet",
                phase="starting",
                reason_code="launch-requested",
                detail={"placement": "gpu"},
                desired_fingerprint_sha256="fp-parakeet",
                plan=_parakeet_plan("vulkan"),
                boot_required=True,
            ),
        ),
        (
            "parakeet",
            "ready",
            _parakeet_plan("cpu"),
            supervisor.ProviderTruthObservation(
                provider="parakeet",
                phase="starting",
                reason_code="launch-requested",
                detail={"placement": "gpu"},
                desired_fingerprint_sha256="fp-parakeet",
                plan=_parakeet_plan("vulkan"),
                boot_required=True,
            ),
        ),
    ],
)
def test_same_target_transient_observation_keeps_captured_plan_authoritative(
    provider: str,
    phase: RuntimePhase,
    plan: supervisor.LocalServerLaunchPlan | supervisor.ParakeetServerLaunchPlan,
    observation: supervisor.ProviderTruthObservation,
) -> None:
    state = supervisor._provider_runtime_states[provider]
    state.latest_phase = phase
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.desired_fingerprint = plan.desired_fingerprint_sha256
    state.truth_fence = supervisor._provider_fence(state, 0)
    state.truth_future = _future_with(observation)
    if phase == "starting":
        state.start_fence = supervisor._provider_fence(state, 0)
        state.start_future = concurrent.futures.Future()
        state.start_cancel_event = threading.Event()

    assert supervisor._handle_provider_truth_result(state) is True

    if state.start_cancel_event is not None:
        assert not state.start_cancel_event.is_set()
    assert state.latest_plan is plan
    assert state.latest_phase == phase


def test_owned_local_host_blocked_defers_admission_exclusive_stop() -> None:
    plan = _cuda_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "ready"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.desired_fingerprint = plan.desired_fingerprint_sha256
    state.truth_fence = supervisor._provider_fence(state, 0)
    state.truth_future = _future_with(
        supervisor.ProviderTruthObservation(
            provider="local",
            phase="host-blocked",
            reason_code="gpu-unavailable",
            detail={"host": {"reason": "transient cuda pressure"}},
            desired_fingerprint_sha256=plan.desired_fingerprint_sha256,
            boot_required=True,
        )
    )

    assert supervisor._handle_provider_truth_result(state) is True

    assert state.latest_plan is plan
    assert state.latest_phase == "stop-deferred"
    assert state.pending_stop_admission_exclusive is True
    assert state.pending_stop_target_phase == "host-blocked"


def test_parakeet_stt_admission_latch_survives_ready_probe_and_restart(
    monkeypatch,
) -> None:
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(supervisor, "local_stt_backend", lambda: "parakeet")
    monkeypatch.setattr(supervisor, "stt_local_floor_bytes", lambda: 4 * 1024**3)
    transcribe: dict[str, Any] = {}
    admission_input = supervisor._parakeet_stt_admission_input(transcribe, False)
    _input_json, input_sha = supervisor._target_fingerprint_pair(admission_input)
    latch = {
        "input_json": "{}",
        "input_sha256": input_sha,
        "retry_epoch": 0,
        "choice": "parakeet",
        "desired": True,
        "blocked": False,
        "reason_code": "launch-requested",
    }
    plan = _parakeet_plan("cpu")
    state = supervisor._provider_runtime_states["parakeet"]
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.generation = 1
    state.retry.attempt_count = 1
    write_runtime_health(
        {
            **read_runtime_health("parakeet"),
            "phase": "starting",
            "reason_code": "launch-requested",
            "detail": {"stt_admission_latch": latch},
            "desired_fingerprint_sha256": plan.desired_fingerprint_sha256,
            "incarnation": supervisor._PROVIDER_INCARNATION,
            "generation": 1,
            "attempt": 1,
            "process": None,
            "updated_at": "2026-07-19T00:00:00+00:00",
            "owner": {"test": "latch"},
        }
    )

    supervisor._write_provider_runtime(
        state,
        phase="ready",
        reason_code="probe-ready",
        detail={"backend": "cpu", "port": 45678},
        process={
            "name": supervisor.PARAKEET_SERVER_PROCESS_NAME,
            "pid": 12345,
            "ref": "ref-parakeet",
            "port": 45678,
        },
    )
    supervisor._write_provider_runtime(
        state,
        phase="ready-proof-unavailable",
        reason_code="proof-observation-unavailable",
        detail={"health_state": "failed"},
        process={
            "name": supervisor.PARAKEET_SERVER_PROCESS_NAME,
            "pid": 12345,
            "ref": "ref-parakeet",
            "port": 45678,
        },
    )
    monkeypatch.setattr(
        supervisor,
        "read_available_bytes",
        lambda: pytest.fail("valid latch must avoid point-in-time RAM recheck"),
    )

    recovered = supervisor._parakeet_stt_admission_latch(transcribe, False)

    assert recovered == latch
    assert read_runtime_health("parakeet")["detail"]["stt_admission_latch"] == latch


def test_handle_shutdown_signals_pending_provider_start(monkeypatch) -> None:
    state = supervisor._provider_runtime_states["local"]
    event = threading.Event()
    state.start_cancel_event = event
    state.start_fence = supervisor.ProviderFence(
        incarnation=supervisor._PROVIDER_INCARNATION,
        generation=1,
        fingerprint="fp-local",
        attempt=1,
    )
    monkeypatch.setattr(supervisor, "_managed_procs", [])
    monkeypatch.setattr(supervisor, "shutdown_requested", False)

    try:
        with pytest.raises(KeyboardInterrupt):
            supervisor.handle_shutdown(15, None)
    finally:
        supervisor.shutdown_requested = False

    assert event.is_set()


def test_local_probe_worker_reports_loading_without_opening_socket(monkeypatch) -> None:
    from solstone.think.providers import local_server

    calls: list[int] = []

    def fake_probe(port: int):
        calls.append(port)
        return local_server.STATE_LOADING, None

    monkeypatch.setattr(local_server, "_probe_health", fake_probe)

    outcome = supervisor._provider_probe_worker(
        "local",
        45678,
        supervisor.ProviderFence(
            incarnation=supervisor._PROVIDER_INCARNATION,
            generation=1,
            fingerprint="fp-local",
            attempt=1,
        ),
    )

    assert calls == [45678]
    assert outcome.status == "not-ready"
    assert outcome.reason_code == "proof-observation-unavailable"
    assert outcome.detail["health_state"] == local_server.STATE_LOADING


def test_parakeet_probe_worker_has_no_loading_state(monkeypatch) -> None:
    from solstone.think.providers import parakeet_server

    calls: list[int] = []

    def fake_probe(port: int):
        calls.append(port)
        return parakeet_server.STATE_FAILED, "warming"

    monkeypatch.setattr(parakeet_server, "_probe_health", fake_probe)

    outcome = supervisor._provider_probe_worker(
        "parakeet",
        45679,
        supervisor.ProviderFence(
            incarnation=supervisor._PROVIDER_INCARNATION,
            generation=1,
            fingerprint="fp-parakeet",
            attempt=1,
        ),
    )

    assert calls == [45679]
    assert outcome.status == "unavailable"
    assert outcome.reason_code == "proof-observation-unavailable"
    assert outcome.detail["health_state"] == parakeet_server.STATE_FAILED


def test_probe_slot_transitions_ready_and_proof_unavailable_with_fakes(
    monkeypatch,
) -> None:
    from solstone.think.providers import local_server

    managed = _FakeManaged()
    state = supervisor._provider_runtime_states["local"]
    plan = _local_plan()
    _set_provider_ready("local", state, plan)
    state.next_truth_at = 10**12
    state.next_probe_at = 0.0
    supervisor.write_service_port("local", 45678)
    write_runtime_health(
        _runtime_record(
            "local",
            phase="ready",
            fingerprint=plan.desired_fingerprint_sha256,
            generation=1,
            attempt=1,
            process={
                "name": managed.name,
                "pid": managed.process.pid,
                "ref": managed.ref,
                "port": 45678,
            },
        )
    )
    observations = [
        (local_server.STATE_LOADING, None),
        (local_server.STATE_READY, None),
    ]
    calls: list[int] = []

    def fake_probe(port: int):
        calls.append(port)
        return observations.pop(0)

    monkeypatch.setattr(local_server, "_probe_health", fake_probe)
    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 100.0)

    asyncio.run(supervisor._reconcile_local_provider_runtime([managed]))
    asyncio.run(supervisor._reconcile_local_provider_runtime([managed]))

    assert state.latest_phase == "ready-proof-unavailable"
    assert calls == [45678]
    assert read_runtime_health("local")["process"]["ref"] == managed.ref

    state.next_probe_at = 0.0
    asyncio.run(supervisor._reconcile_local_provider_runtime([managed]))
    asyncio.run(supervisor._reconcile_local_provider_runtime([managed]))

    assert state.latest_phase == "ready"
    assert calls == [45678, 45678]


def test_wedge_threshold_records_recycle_token_without_sync_termination(
    monkeypatch,
) -> None:
    from solstone.think.providers import local_endpoint, local_server
    from solstone.think.providers.local_endpoint import LocalEndpoint

    state = supervisor._provider_runtime_states["local"]
    plan = _local_plan()
    _set_provider_ready("local", state, plan)
    state.generation = 4
    state.next_truth_at = 9999.0
    managed = _FakeManaged()
    supervisor._managed_procs = [managed]
    supervisor._SERVICE_STATE[managed.name] = {
        "restart": False,
        "shutdown_timeout": 15,
    }
    monkeypatch.setattr(
        local_endpoint,
        "resolve_local_endpoint",
        lambda: LocalEndpoint("", "", None, True),
    )
    monkeypatch.setattr(supervisor, "read_service_port", lambda service: 45678)
    monkeypatch.setattr(
        local_server,
        "_probe_health",
        lambda port: (local_server.STATE_READY, None),
    )
    monkeypatch.setattr(
        supervisor,
        "_restart_service",
        lambda *_args, **_kwargs: pytest.fail("wedge must not restart synchronously"),
    )
    monkeypatch.setattr(
        supervisor,
        "_start_termination_thread",
        lambda *_args, **_kwargs: pytest.fail("wedge must not terminate synchronously"),
    )

    for idx in range(supervisor.LOCAL_WEDGE_THRESHOLD):
        use_id = f"wedge-{idx}"
        supervisor._handle_cortex_outcome(
            {
                "tract": "cortex",
                "event": "start",
                "use_id": use_id,
                "provider": "local",
            }
        )
        supervisor._handle_cortex_outcome(
            {
                "tract": "cortex",
                "event": "error",
                "use_id": use_id,
                "reason_code": "provider_unavailable",
            }
        )

    token = read_retry_token("local")
    assert token["reason_code"] == "local-wedge-provider-unavailable"
    assert token["desired_fingerprint_sha256"] == plan.desired_fingerprint_sha256
    assert state.generation == 5
    assert state.latest_phase == "retry-requested"
    assert state.next_truth_at == 0.0
    assert supervisor._recovery_state["local"].down_generation == 5
    assert supervisor._SERVICE_STATE[managed.name] == {
        "restart": False,
        "shutdown_timeout": 15,
    }
    managed.terminate.assert_not_called()


def _plan_for_backend(
    backend: str,
) -> supervisor.LocalServerLaunchPlan | supervisor.ParakeetServerLaunchPlan:
    if backend == "vulkan":
        return _local_plan()
    if backend == "cuda":
        return _cuda_plan()
    if backend == "mlx":
        return _mlx_plan()
    if backend == "parakeet-vulkan":
        return _parakeet_plan("vulkan")
    return _parakeet_plan("cpu")


def _process_name_for_backend(backend: str) -> str:
    if backend == "mlx":
        return supervisor.MLX_SERVER_PROCESS_NAME
    if backend.startswith("parakeet"):
        return supervisor.PARAKEET_SERVER_PROCESS_NAME
    return supervisor.LOCAL_SERVER_PROCESS_NAME


def _launch_backend_for_test(
    backend: str,
    plan: supervisor.LocalServerLaunchPlan | supervisor.ParakeetServerLaunchPlan,
    reservation: _FakeReservation,
    cancel_event: threading.Event,
) -> supervisor.ProviderLaunchOutcome:
    if backend.startswith("parakeet"):
        return supervisor.start_parakeet_server(
            plan,
            reservation,
            cancel_event,
        )
    return supervisor.start_local_server(plan, reservation, cancel_event)


@pytest.mark.parametrize(
    "backend",
    ["vulkan", "cuda", "mlx", "parakeet-cpu", "parakeet-vulkan"],
)
@pytest.mark.parametrize("cancel_point", ["before-probe", "ready", "wait"])
def test_start_worker_cancellation_cleans_child_at_warmup_boundaries(
    monkeypatch,
    backend: str,
    cancel_point: str,
) -> None:
    from solstone.think.providers import local_server, local_vulkan, parakeet_server

    plan = _plan_for_backend(backend)
    cancel_event = threading.Event()
    probe_entered = threading.Event()
    managed = _FakeManaged(_process_name_for_backend(backend))
    service_name = managed.name
    ports: list[tuple[str, int]] = []
    placements: list[str] = []

    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(
        supervisor,
        "write_service_port",
        lambda service, port: ports.append((service, port)),
    )
    monkeypatch.setattr(
        local_server,
        "write_local_context_window",
        lambda _tokens: None,
    )
    monkeypatch.setattr(
        local_server,
        "fetch_props",
        lambda _port: pytest.fail("cancelled launch must not fetch props"),
    )
    monkeypatch.setattr(
        local_vulkan,
        "device_local_used_mib",
        lambda _index: pytest.fail("cancelled launch must not inspect post-ready VRAM"),
    )
    monkeypatch.setattr(
        parakeet_server,
        "write_parakeet_placement",
        lambda placement: placements.append(placement),
    )

    def launch_process(name, _cmd, **_kwargs):
        supervisor._SERVICE_STATE[name] = {
            "restart": True,
            "shutdown_timeout": 15,
        }
        if cancel_point == "before-probe":
            cancel_event.set()
        return managed

    def local_probe(_port):
        probe_entered.set()
        if cancel_point == "ready":
            cancel_event.set()
            return local_server.STATE_READY, None
        return local_server.STATE_STARTING, None

    def parakeet_probe(_port):
        probe_entered.set()
        if cancel_point == "ready":
            cancel_event.set()
            return parakeet_server.STATE_READY, None
        return parakeet_server.STATE_FAILED, "warming"

    monkeypatch.setattr(supervisor, "_launch_process", launch_process)
    monkeypatch.setattr(local_server, "_probe_health", local_probe)
    monkeypatch.setattr(parakeet_server, "_probe_health", parakeet_probe)

    outcome_box: dict[str, supervisor.ProviderLaunchOutcome] = {}

    def run_launch() -> None:
        outcome_box["outcome"] = _launch_backend_for_test(
            backend,
            plan,
            _FakeReservation(port=45678),
            cancel_event,
        )

    if cancel_point == "wait":
        thread = threading.Thread(target=run_launch)
        thread.start()
        assert probe_entered.wait(timeout=1.0)
        cancel_event.set()
        thread.join(timeout=1.0)
        assert not thread.is_alive()
    else:
        run_launch()

    outcome = outcome_box["outcome"]
    assert outcome.status == "launch-failed"
    assert outcome.detail["cancelled"] is True
    assert outcome.managed is None
    managed.terminate.assert_called_once()
    managed.cleanup.assert_called_once_with()
    assert service_name not in supervisor._SERVICE_STATE
    assert ports == []
    assert placements == []
    if cancel_point == "before-probe":
        assert not probe_entered.is_set()


def test_cancelled_launch_outcome_preserves_handle_when_cleanup_raises(
    monkeypatch,
) -> None:
    managed = _FakeManaged()
    monkeypatch.setattr(
        supervisor,
        "_terminate_cleanup_handle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    outcome = supervisor._cancelled_launch_outcome(
        "local",
        backend="cuda",
        port=45678,
        managed=managed,
        reason="test cancellation",
    )

    assert outcome.status == "launch-failed"
    assert outcome.managed is managed
    assert outcome.detail["cancelled"] is True
    assert outcome.detail["cleanup_failed"] is True
    assert outcome.detail["cleanup_deferred_to"] == "cleanup-failed-reconciler"


def test_cancelled_ready_result_is_cleaned_without_port_publication(monkeypatch):
    plan = _local_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "starting"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 1
    fence = supervisor._provider_fence(state, 1)
    managed = _FakeManaged()
    cancel_event = threading.Event()
    cancel_event.set()
    ports: list[tuple[str, int]] = []
    state.start_fence = fence
    state.start_cancel_event = cancel_event
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="ready",
            reason_code="probe-ready",
            detail={"port": 45678},
            managed=managed,
        )
    )
    monkeypatch.setattr(
        supervisor,
        "write_service_port",
        lambda service, port: ports.append((service, port)),
    )

    assert supervisor._handle_provider_start_result(state, []) is True

    assert ports == []
    managed.terminate.assert_called_once()
    managed.cleanup.assert_called_once_with()
    assert state.latest_phase == "backoff"


def test_superseded_start_cleanup_failure_is_adopted(monkeypatch) -> None:
    plan = _local_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_plan = plan
    state.latest_phase = "ready"
    state.desired_fingerprint = "fp-new"
    state.retry.attempt_count = 2
    state.generation = 2
    old_managed = _FakeManaged()
    state.start_fence = supervisor.ProviderFence(
        incarnation=supervisor._PROVIDER_INCARNATION,
        generation=1,
        fingerprint="fp-old",
        attempt=1,
    )
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="warmup-timeout",
            reason_code="warmup-timeout",
            detail={"port": 11111},
            managed=old_managed,
        )
    )
    monkeypatch.setattr(
        supervisor,
        "_terminate_cleanup_handle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    assert supervisor._handle_provider_start_result(state, []) is True

    assert state.latest_phase == "cleanup-failed"
    assert state.pending_stop_request is not None
    assert state.pending_stop_request.managed is old_managed


def test_pending_stop_request_assignment_uses_single_chokepoint() -> None:
    tree = ast.parse(Path(supervisor.__file__).read_text(encoding="utf-8"))
    offenders: list[tuple[str, int]] = []
    stack: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                self._check_target(target, node.lineno)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            self._check_target(node.target, node.lineno)
            self.generic_visit(node)

        def _check_target(self, target: ast.expr, lineno: int) -> None:
            if not (
                isinstance(target, ast.Attribute)
                and target.attr == "pending_stop_request"
            ):
                return
            owner = stack[-1] if stack else "<module>"
            if owner != "_set_provider_pending_stop_request":
                offenders.append((owner, lineno))

    Visitor().visit(tree)

    assert offenders == []


def test_pending_cleanup_survives_truth_phase_change_and_blocks_start(
    monkeypatch,
) -> None:
    plan = _local_plan()
    managed = _FakeManaged()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "cleanup-failed"
    state.latest_plan = None
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 1
    state.pending_stop_request = supervisor._make_stop_request(
        state,
        managed,
        reason_code="cleanup-attempt-failed",
        detail={"source": "preserved-handle"},
        target_phase="stopped",
        target_reason_code="cleanup-succeeded",
    )
    state.cleanup_attempt_count = 1
    state.cleanup_next_at = 50.0
    state.truth_fence = supervisor._provider_fence(state, 1)
    state.truth_future = _future_with(
        supervisor.ProviderTruthObservation(
            provider="local",
            phase="starting",
            reason_code="launch-requested",
            detail={"backend": plan.backend},
            desired_fingerprint_json=plan.desired_fingerprint_json,
            desired_fingerprint_sha256=plan.desired_fingerprint_sha256,
            plan=plan,
            boot_required=True,
        )
    )
    start_submits: list[str] = []
    monkeypatch.setattr(
        supervisor,
        "_provider_start_worker",
        lambda *_args: start_submits.append("start"),
    )

    assert supervisor._handle_provider_truth_result(state) is True
    assert state.latest_phase == "starting"
    assert state.pending_stop_request is not None
    assert state.pending_stop_request.managed is managed

    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 10.0)
    assert supervisor._submit_provider_stop_cleanup_if_needed(state, []) is True
    assert state.stop_cleanup_future is None
    supervisor._submit_provider_start_if_needed(state, [])
    assert state.start_future is None
    assert start_submits == []

    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 50.0)
    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    assert supervisor._submit_provider_stop_cleanup_if_needed(state, []) is True
    assert supervisor._handle_provider_stop_cleanup_result(state, []) is True

    managed.terminate.assert_called_once()
    managed.cleanup.assert_called_once_with()
    assert state.pending_stop_request is None
    assert state.latest_phase == "stopped"
    assert start_submits == []


def test_cancelled_stop_cleanup_preserves_unresolved_handle(monkeypatch) -> None:
    plan = _local_plan()
    managed = _FakeManaged()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "stopping"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 1
    state.pending_stop_request = supervisor._make_stop_request(
        state,
        managed,
        reason_code="cleanup-attempt-failed",
        detail={"source": "preserved-handle"},
        target_phase="stopped",
        target_reason_code="cleanup-succeeded",
    )
    state.stop_cleanup_fence = supervisor._provider_fence(state, 1)
    state.stop_cleanup_future = _future_with(
        supervisor.ProviderStopCleanupOutcome(
            status="cancelled",
            reason_code="stale-result-ignored",
            detail={"cancelled": True},
            managed=managed,
        )
    )
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 100.0)

    assert supervisor._handle_provider_stop_cleanup_result(state, []) is True

    assert state.latest_phase == "cleanup-failed"
    assert state.pending_stop_request is not None
    assert state.pending_stop_request.managed is managed
    assert state.cleanup_attempt_count == 1
    assert state.cleanup_next_at == 102.0
    supervisor._submit_provider_start_if_needed(state, [])
    assert state.start_future is None


def test_late_probe_cannot_declare_ready_with_cleanup_outstanding(
    monkeypatch,
) -> None:
    plan = _local_plan()
    managed = _FakeManaged()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "cleanup-failed"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 1
    state.pending_stop_request = supervisor._make_stop_request(
        state,
        managed,
        reason_code="cleanup-attempt-failed",
        detail={"source": "preserved-handle"},
        target_phase="stopped",
        target_reason_code="cleanup-succeeded",
    )
    state.probe_fence = supervisor._provider_fence(state, 1)
    state.probe_future = _future_with(
        supervisor.ProviderProbeOutcome(
            status="ready",
            reason_code="probe-ready",
            detail={"port": 45678},
        )
    )
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 100.0)

    assert supervisor._handle_provider_probe_result(state) is True

    assert state.latest_phase == "cleanup-failed"
    assert state.pending_stop_request is not None
    assert state.pending_stop_request.managed is managed
    assert state.next_probe_at == 100.0 + supervisor.PROVIDER_PROBE_INTERVAL_SECONDS
    assert read_runtime_health("local")["reason_code"] == "stale-result-ignored"


def test_deferred_stop_preserves_existing_cleanup_request() -> None:
    old_plan = _local_plan()
    managed = _FakeManaged()
    state = supervisor._provider_runtime_states["local"]
    _set_provider_ready("local", state, old_plan)
    state.pending_stop_request = supervisor._make_stop_request(
        state,
        managed,
        reason_code="cleanup-attempt-failed",
        detail={"source": "preserved-handle"},
        target_phase="stopped",
        target_reason_code="cleanup-succeeded",
    )
    state.cleanup_attempt_count = 2
    state.cleanup_next_at = 50.0
    observation = supervisor.ProviderTruthObservation(
        provider="local",
        phase="not-desired",
        reason_code="provider-not-needed",
        detail={"active_provider": "cloud"},
        desired_fingerprint_sha256=old_plan.desired_fingerprint_sha256,
        boot_required=False,
    )

    supervisor._defer_provider_stop_for_observation(
        state,
        observation,
        reason_code="admission-exclusive-stop",
        admission_exclusive=True,
    )

    assert state.latest_phase == "stop-deferred"
    assert state.pending_stop_request is not None
    assert state.pending_stop_request.managed is managed
    assert state.pending_stop_request.target_phase == "stop-deferred"
    assert state.pending_stop_request.target_detail["target_phase"] == "not-desired"
    assert state.cleanup_attempt_count == 2
    assert state.cleanup_next_at == 50.0


def test_unresolvable_cleanup_stays_owned_visible_and_blocks_start(
    monkeypatch,
) -> None:
    plan = _local_plan()
    managed = _FakeManaged()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "cleanup-failed"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 1
    state.pending_stop_request = supervisor._make_stop_request(
        state,
        managed,
        reason_code="cleanup-attempt-failed",
        detail={"source": "preserved-handle"},
        target_phase="stopped",
        target_reason_code="cleanup-succeeded",
    )
    state.cleanup_attempt_count = 0
    state.cleanup_next_at = 0.0
    starts: list[str] = []
    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        supervisor,
        "_terminate_cleanup_handle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("still alive")),
    )
    monkeypatch.setattr(
        supervisor,
        "_provider_start_worker",
        lambda *_args: starts.append("start"),
    )

    assert supervisor._submit_provider_stop_cleanup_if_needed(state, []) is True
    assert supervisor._handle_provider_stop_cleanup_result(state, []) is True
    supervisor._submit_provider_start_if_needed(state, [])

    assert state.latest_phase == "cleanup-failed"
    assert state.pending_stop_request is not None
    assert state.pending_stop_request.managed is managed
    assert state.cleanup_attempt_count == 1
    assert state.cleanup_next_at == 102.0
    assert state.start_future is None
    assert starts == []


def test_cancelled_ready_cleanup_failure_is_adopted(monkeypatch) -> None:
    plan = _local_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "starting"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 1
    fence = supervisor._provider_fence(state, 1)
    managed = _FakeManaged()
    cancel_event = threading.Event()
    cancel_event.set()
    state.start_fence = fence
    state.start_cancel_event = cancel_event
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="ready",
            reason_code="probe-ready",
            detail={"port": 45678},
            managed=managed,
        )
    )
    monkeypatch.setattr(
        supervisor,
        "_terminate_cleanup_handle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    assert supervisor._handle_provider_start_result(state, []) is True

    assert state.latest_phase == "cleanup-failed"
    assert state.pending_stop_request is not None
    assert state.pending_stop_request.managed is managed


def test_missing_ready_port_cleanup_failure_is_adopted(monkeypatch) -> None:
    plan = _local_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "starting"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 1
    managed = _FakeManaged()
    state.start_fence = supervisor._provider_fence(state, 1)
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="ready",
            reason_code="probe-ready",
            detail={},
            managed=managed,
        )
    )
    monkeypatch.setattr(
        supervisor,
        "_terminate_cleanup_handle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    assert supervisor._handle_provider_start_result(state, []) is True

    assert state.latest_phase == "cleanup-failed"
    assert state.pending_stop_request is not None
    assert state.pending_stop_request.managed is managed


def test_port_publication_cleanup_failure_is_adopted(monkeypatch) -> None:
    plan = _local_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "starting"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 1
    managed = _FakeManaged()
    state.start_fence = supervisor._provider_fence(state, 1)
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="ready",
            reason_code="probe-ready",
            detail={"port": 45678},
            managed=managed,
        )
    )
    monkeypatch.setattr(
        supervisor,
        "write_service_port",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(
        supervisor,
        "_terminate_cleanup_handle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )

    assert supervisor._handle_provider_start_result(state, []) is True

    assert state.latest_phase == "cleanup-failed"
    assert state.pending_stop_request is not None
    assert state.pending_stop_request.managed is managed


def test_observation_raced_when_target_fingerprint_changes_between_reads(
    monkeypatch,
) -> None:
    from solstone.think.providers import local_install, local_server, local_vulkan

    fingerprints = iter(
        [
            {"provider": "local", "target": "one"},
            {"provider": "local", "target": "two"},
        ]
    )
    monkeypatch.setattr(supervisor, "_is_remote_mode", False)
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor, "read_journal_config", lambda: {})
    monkeypatch.setattr(
        supervisor, "is_local_provider_needed", lambda _config=None: True
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.resolve_local_endpoint",
        lambda: type("Endpoint", (), {"is_bundled": True})(),
    )
    monkeypatch.setattr(
        local_install,
        "target_fingerprint",
        lambda _model_id: next(fingerprints),
    )
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model_id: _local_readiness(),
    )
    monkeypatch.setattr(local_install, "gpu_device_override", lambda: None)
    monkeypatch.setattr(
        local_vulkan,
        "detect_gpus",
        lambda: [
            local_vulkan.VulkanDevice(0, "GPU", local_vulkan.VK_TYPE_DISCRETE, 12288)
        ],
    )
    monkeypatch.setattr(
        local_vulkan, "select_device", lambda devices, **_kw: devices[0]
    )
    monkeypatch.setattr(local_vulkan, "device_local_used_mib", lambda _index: 0)
    monkeypatch.setattr(local_server, "select_server_tier", lambda _vram: _FakeTier())
    monkeypatch.setattr(
        supervisor,
        "_launch_process",
        lambda *_args, **_kwargs: pytest.fail("observation race must not launch"),
    )

    observation = supervisor._observe_local_provider_truth()

    assert observation.phase == "observing"
    assert observation.reason_code == "observation-raced"


def test_observation_raced_when_device_changes_during_plan_construction(
    monkeypatch,
) -> None:
    from solstone.think.providers import local_install, local_server, local_vulkan

    devices = [
        local_vulkan.VulkanDevice(0, "GPU-A", local_vulkan.VK_TYPE_DISCRETE, 12288),
        local_vulkan.VulkanDevice(1, "GPU-B", local_vulkan.VK_TYPE_DISCRETE, 16384),
    ]
    selections = iter([devices[0], devices[0], devices[1]])
    monkeypatch.setattr(supervisor, "_is_remote_mode", False)
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor, "read_journal_config", lambda: {})
    monkeypatch.setattr(
        supervisor, "is_local_provider_needed", lambda _config=None: True
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.resolve_local_endpoint",
        lambda: type("Endpoint", (), {"is_bundled": True})(),
    )
    monkeypatch.setattr(
        local_install,
        "target_fingerprint",
        lambda _model_id: {"provider": "local", "target": "one"},
    )
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda _model_id: _local_readiness(),
    )
    monkeypatch.setattr(local_install, "gpu_device_override", lambda: None)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: devices)
    monkeypatch.setattr(
        local_vulkan, "select_device", lambda _devices, **_kw: next(selections)
    )
    monkeypatch.setattr(local_vulkan, "device_local_used_mib", lambda _index: 0)
    monkeypatch.setattr(local_server, "select_server_tier", lambda _vram: _FakeTier())

    observation = supervisor._observe_local_provider_truth()

    assert observation.phase == "observing"
    assert observation.reason_code == "observation-raced"


def test_discarded_truth_result_reobserves_immediately_after_retry_fence_change(
    monkeypatch,
) -> None:
    plan = _local_plan()
    now = 100.0
    truth_submits = 0
    first_truth: concurrent.futures.Future = concurrent.futures.Future()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "ready"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.desired_fingerprint = plan.desired_fingerprint_sha256
    state.next_truth_at = 0.0

    class _RaceExecutor:
        def submit(self, fn, *args, **kwargs):
            nonlocal truth_submits
            if fn is supervisor._observe_provider_truth:
                truth_submits += 1
                if truth_submits == 1:
                    return first_truth
                return _future_with(
                    supervisor.ProviderTruthObservation(
                        provider="local",
                        phase="not-desired",
                        reason_code="provider-not-needed",
                        detail={},
                    )
                )
            assert fn is supervisor._provider_start_worker
            return _future_with(
                supervisor.ProviderLaunchOutcome(
                    status="launch-failed",
                    reason_code="launch-failed",
                    detail={},
                )
            )

    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _RaceExecutor())
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: now)

    asyncio.run(supervisor._reconcile_local_provider_runtime([]))

    assert truth_submits == 1
    assert state.truth_future is first_truth
    assert state.next_truth_at == (
        now + supervisor.PROVIDER_TRUTH_OBSERVATION_INTERVAL_SECONDS
    )

    state.latest_phase = "backoff"
    state.retry.next_at = now
    supervisor._submit_provider_start_if_needed(state, [])
    assert state.retry.attempt_count == 1
    first_truth.set_result(
        supervisor.ProviderTruthObservation(
            provider="local",
            phase="not-desired",
            reason_code="provider-not-needed",
            detail={"active_provider": "cloud"},
        )
    )

    asyncio.run(supervisor._reconcile_local_provider_runtime([]))

    assert truth_submits == 2
    assert state.truth_future is not first_truth
    assert state.next_truth_at == (
        now + supervisor.PROVIDER_TRUTH_OBSERVATION_INTERVAL_SECONDS
    )


def _set_provider_ready(
    provider: str,
    state: supervisor.ProviderRuntimeState,
    plan: supervisor.LocalServerLaunchPlan | supervisor.ParakeetServerLaunchPlan,
) -> None:
    state.latest_phase = "ready"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.desired_fingerprint = plan.desired_fingerprint_sha256
    state.generation = 1
    state.retry.attempt_count = 1
    del provider


def test_admission_exclusive_stop_defers_then_stops_when_slot_frees(
    monkeypatch,
) -> None:
    from solstone.think.providers import local_admission

    plan = _local_plan()
    managed = _FakeManaged()
    state = supervisor._provider_runtime_states["local"]
    _set_provider_ready("local", state, plan)
    request = supervisor._make_stop_request(
        state,
        managed,
        reason_code="admission-exclusive-stop",
        detail={},
        target_phase="not-desired",
        target_reason_code="provider-not-needed",
        admission_exclusive=True,
    )
    cancel_event = threading.Event()
    holder = _hold_local_slot_in_child(local_admission._admission_dir())

    assert supervisor.PROVIDER_ADMISSION_STOP_TIMEOUT_S == 5.0
    monkeypatch.setattr(supervisor, "PROVIDER_ADMISSION_STOP_TIMEOUT_S", 0.0)

    try:
        outcome = supervisor._provider_stop_cleanup_worker(
            "local",
            request,
            supervisor._provider_fence(state, 1),
            cancel_event,
        )

        assert outcome.status == "stop-deferred"
        assert managed.terminate.call_count == 0
        assert list(local_admission._admission_dir().glob("wait-*.ticket")) == []
    finally:
        holder.terminate()
        holder.wait(timeout=2)

    outcome = supervisor._provider_stop_cleanup_worker(
        "local",
        request,
        supervisor._provider_fence(state, 1),
        cancel_event,
    )

    assert outcome.status == "stopped"
    managed.terminate.assert_called_once()
    managed.cleanup.assert_called_once_with()


def test_admission_exclusive_stop_uses_launch_captured_capacity(monkeypatch) -> None:
    from solstone.think.providers import local_admission

    capacities: list[int] = []
    original_acquire = local_admission.acquire_local_slot
    plan = replace(_local_plan(), parallel_slots=3)
    managed = _FakeManaged()
    state = supervisor._provider_runtime_states["local"]
    _set_provider_ready("local", state, plan)
    state.latest_phase = "stop-deferred"
    state.pending_stop_target_phase = "not-desired"
    state.pending_stop_target_reason_code = "provider-not-needed"
    state.pending_stop_admission_exclusive = True
    state.next_truth_at = 10**12
    procs = [managed]

    def acquire(capacity, timeout_s, **kwargs):
        capacities.append(capacity)
        return original_acquire(capacity, timeout_s, **kwargs)

    monkeypatch.setattr(local_admission, "acquire_local_slot", acquire)
    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())

    asyncio.run(supervisor._reconcile_local_provider_runtime(procs))
    asyncio.run(supervisor._reconcile_local_provider_runtime(procs))

    assert capacities == [3]
    assert state.latest_phase == "not-desired"


def test_admission_exclusive_stop_rechecks_reactivation_after_acquisition(
    monkeypatch,
) -> None:
    from solstone.think.providers import local_admission

    original_acquire = local_admission.acquire_local_slot
    managed = _FakeManaged()
    state = supervisor._provider_runtime_states["local"]
    plan = _local_plan()
    _set_provider_ready("local", state, plan)
    request = supervisor._make_stop_request(
        state,
        managed,
        reason_code="admission-exclusive-stop",
        detail={},
        target_phase="not-desired",
        target_reason_code="provider-not-needed",
        admission_exclusive=True,
    )
    cancel_event = threading.Event()

    def acquire(*args, **kwargs):
        permit = original_acquire(*args, **kwargs)
        cancel_event.set()
        return permit

    monkeypatch.setattr(local_admission, "acquire_local_slot", acquire)

    outcome = supervisor._provider_stop_cleanup_worker(
        "local",
        request,
        supervisor._provider_fence(state, 1),
        cancel_event,
    )

    assert outcome.status == "cancelled"
    assert outcome.managed is managed
    managed.terminate.assert_not_called()
    managed.cleanup.assert_not_called()


@pytest.mark.parametrize(
    ("provider", "old_managed", "new_observation"),
    [
        (
            "local",
            _FakeManaged(supervisor.MLX_SERVER_PROCESS_NAME),
            supervisor.ProviderTruthObservation(
                provider="local",
                phase="starting",
                reason_code="launch-requested",
                detail={},
                desired_fingerprint_json='{"provider":"local","backend":"cuda"}',
                desired_fingerprint_sha256="fp-local-cuda",
                plan=_cuda_plan(),
                boot_required=True,
            ),
        ),
        (
            "parakeet",
            _FakeManaged(supervisor.PARAKEET_SERVER_PROCESS_NAME),
            supervisor.ProviderTruthObservation(
                provider="parakeet",
                phase="starting",
                reason_code="launch-requested",
                detail={},
                desired_fingerprint_json='{"provider":"parakeet","target":"new"}',
                desired_fingerprint_sha256="fp-parakeet-new",
                plan=replace(
                    _parakeet_plan("cpu"),
                    desired_fingerprint_sha256="fp-parakeet-new",
                ),
                boot_required=True,
            ),
        ),
    ],
)
def test_stop_before_replace_runs_before_replacement_start(
    monkeypatch,
    provider: str,
    old_managed: _FakeManaged,
    new_observation: supervisor.ProviderTruthObservation,
) -> None:
    state = supervisor._provider_runtime_states[provider]
    old_plan = _local_plan() if provider == "local" else _parakeet_plan("cpu")
    _set_provider_ready(provider, state, old_plan)
    state.next_truth_at = 10**12
    state.truth_fence = supervisor._provider_fence(state, 1)
    state.truth_future = _future_with(new_observation)
    order: list[str] = []
    procs = [old_managed]

    def cleanup(managed, *, reason, state_name=None):
        order.append(f"cleanup:{managed.name}:{reason}")
        managed.terminate()
        managed.cleanup()
        managed.is_running = lambda: False

    def start_worker(provider_arg, _plan_arg, _fence, _cancel_event):
        order.append(f"start:{provider_arg}")
        return supervisor.ProviderLaunchOutcome(
            status="launch-failed",
            reason_code="launch-failed",
            detail={},
        )

    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(supervisor, "_terminate_cleanup_handle", cleanup)
    monkeypatch.setattr(supervisor, "_provider_start_worker", start_worker)

    asyncio.run(supervisor._reconcile_provider_runtime(provider, procs))
    assert order == ["cleanup:" + old_managed.name + ":target-changed"]

    for _ in range(3):
        if order[-1] == f"start:{provider}":
            break
        asyncio.run(supervisor._reconcile_provider_runtime(provider, procs))
    assert order[-1] == f"start:{provider}"
    assert old_managed.terminate.call_count == 1


def test_matching_target_duplicate_convergence_keeps_owner_and_stops_stale(
    monkeypatch,
) -> None:
    keep = _FakeManaged(supervisor.LOCAL_SERVER_PROCESS_NAME)
    stale = _FakeManaged(supervisor.MLX_SERVER_PROCESS_NAME)
    state = supervisor._provider_runtime_states["local"]
    plan = _local_plan()
    _set_provider_ready("local", state, plan)
    state.next_truth_at = 10**12
    procs = [keep, stale]
    write_runtime_health(
        _runtime_record(
            "local",
            phase="ready",
            fingerprint=plan.desired_fingerprint_sha256,
            generation=1,
            attempt=1,
            process={
                "name": keep.name,
                "pid": keep.process.pid,
                "ref": keep.ref,
                "port": 45678,
            },
        )
    )
    starts: list[int] = []
    stopped: list[_FakeManaged] = []
    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(
        supervisor,
        "_provider_start_worker",
        lambda *_args: starts.append(1),
    )
    monkeypatch.setattr(
        supervisor,
        "_terminate_cleanup_handle",
        lambda managed, *, reason, state_name=None: (
            stopped.append(managed),
            managed.terminate(),
            managed.cleanup(),
            setattr(managed, "is_running", lambda: False),
        ),
    )

    asyncio.run(supervisor._reconcile_local_provider_runtime(procs))
    asyncio.run(supervisor._reconcile_local_provider_runtime(procs))

    assert stopped == [stale]
    assert keep not in stopped
    assert starts == []


def test_late_cleanup_cannot_clear_newer_generation_port_file() -> None:
    plan = _local_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "stopping"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.generation = 2
    state.retry.attempt_count = 2
    newer = _FakeManaged()
    supervisor.write_service_port("local", 22222)
    write_runtime_health(
        _runtime_record(
            "local",
            phase="ready",
            fingerprint=plan.desired_fingerprint_sha256,
            generation=2,
            attempt=2,
            process={
                "name": newer.name,
                "pid": newer.process.pid,
                "ref": newer.ref,
                "port": 22222,
            },
        )
    )
    old = _FakeManaged()
    old_fence = supervisor.ProviderFence(
        incarnation=supervisor._PROVIDER_INCARNATION,
        generation=1,
        fingerprint=plan.desired_fingerprint_sha256,
        attempt=1,
    )
    state.pending_stop_request = supervisor._make_stop_request(
        state,
        old,
        reason_code="target-changed",
        detail={},
    )
    state.stop_cleanup_fence = old_fence
    state.stop_cleanup_future = _future_with(
        supervisor.ProviderStopCleanupOutcome(
            status="stopped",
            reason_code="cleanup-succeeded",
            detail={"port": 11111},
        )
    )

    assert supervisor._handle_provider_stop_cleanup_result(state, [newer]) is True

    assert supervisor.read_service_port("local") == 22222


def test_fenced_out_stop_cleanup_failure_preserves_handle() -> None:
    plan = _local_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "ready"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.generation = 2
    state.retry.attempt_count = 2
    stale_managed = _FakeManaged()
    old_fence = supervisor.ProviderFence(
        incarnation=supervisor._PROVIDER_INCARNATION,
        generation=1,
        fingerprint=plan.desired_fingerprint_sha256,
        attempt=1,
    )
    state.pending_stop_request = supervisor._make_stop_request(
        state,
        stale_managed,
        reason_code="target-changed",
        detail={},
    )
    state.stop_cleanup_fence = old_fence
    state.stop_cleanup_future = _future_with(
        supervisor.ProviderStopCleanupOutcome(
            status="cleanup-failed",
            reason_code="cleanup-attempt-failed",
            detail={"error": "terminate failed"},
            managed=stale_managed,
        )
    )

    assert supervisor._handle_provider_stop_cleanup_result(state, []) is True

    assert state.latest_phase == "cleanup-failed"
    assert state.pending_stop_request is not None
    assert state.pending_stop_request.managed is stale_managed


def test_cleanup_failed_cadence_consumes_no_launch_budget(monkeypatch) -> None:
    now = 100.0
    plan = _local_plan()
    managed = _FakeManaged()
    state = supervisor._provider_runtime_states["local"]
    _set_provider_ready("local", state, plan)
    state.retry.attempt_count = 4
    state.pending_stop_request = supervisor._make_stop_request(
        state,
        managed,
        reason_code="target-changed",
        detail={},
    )
    delays: list[float] = []

    def monotonic() -> float:
        return now

    monkeypatch.setattr(supervisor.time, "monotonic", monotonic)

    for _ in range(5):
        request = state.pending_stop_request
        assert request is not None
        supervisor._schedule_cleanup_failed_retry(
            state,
            request,
            supervisor.ProviderStopCleanupOutcome(
                status="cleanup-failed",
                reason_code="cleanup-attempt-failed",
                detail={},
                managed=managed,
            ),
        )
        delays.append(state.cleanup_next_at - now)

    assert delays == [2.0, 4.0, 8.0, 16.0, 30.0]
    assert state.retry.attempt_count == 4


def test_cleanup_failed_rechecks_dead_child_and_recovers(monkeypatch) -> None:
    plan = _local_plan()
    dead = _DeadManaged()
    state = supervisor._provider_runtime_states["local"]
    _set_provider_ready("local", state, plan)
    state.latest_phase = "cleanup-failed"
    state.latest_plan = None
    state.next_truth_at = 10**12
    state.pending_stop_request = supervisor._make_stop_request(
        state,
        dead,
        reason_code="target-changed",
        detail={},
    )
    state.cleanup_next_at = 0.0
    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 10.0)

    procs = [dead]
    asyncio.run(supervisor._reconcile_local_provider_runtime(procs))
    asyncio.run(supervisor._reconcile_local_provider_runtime(procs))

    assert state.latest_phase == "stopped"
    dead.cleanup.assert_called_once_with()


def test_pending_cleanup_dead_child_converges_after_truth_changed_phase(
    monkeypatch,
) -> None:
    plan = _local_plan()
    dead = _DeadManaged()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "starting"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 1
    state.pending_stop_request = supervisor._make_stop_request(
        state,
        dead,
        reason_code="cleanup-attempt-failed",
        detail={"source": "preserved-handle"},
        target_phase="stopped",
        target_reason_code="cleanup-succeeded",
    )
    state.cleanup_attempt_count = 1
    state.cleanup_next_at = 0.0
    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 10.0)

    assert supervisor._submit_provider_stop_cleanup_if_needed(state, []) is True
    assert supervisor._handle_provider_stop_cleanup_result(state, [dead]) is True

    assert state.pending_stop_request is None
    assert state.latest_phase == "stopped"
    dead.cleanup.assert_called_once_with()


def test_preserved_cancel_cleanup_handle_is_adopted_not_orphaned() -> None:
    plan = _local_plan()
    managed = _FakeManaged()
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "starting"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.generation = 1
    state.retry.attempt_count = 1
    state.start_fence = supervisor._provider_fence(state, 1)
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="launch-failed",
            reason_code="launch-failed",
            detail={
                "cleanup_failed": True,
                "cleanup_deferred_to": "cleanup-failed-reconciler",
            },
            managed=managed,
        )
    )

    assert supervisor._handle_provider_start_result(state, []) is True

    assert state.latest_phase == "cleanup-failed"
    assert state.pending_stop_request is not None
    assert state.pending_stop_request.managed is managed
    managed.terminate.assert_not_called()
    managed.cleanup.assert_not_called()


def test_shutdown_stop_cleanup_signal_does_not_run_cleanup(monkeypatch) -> None:
    state = supervisor._provider_runtime_states["local"]
    event = threading.Event()
    state.stop_cleanup_cancel_event = event
    state.stop_cleanup_fence = supervisor.ProviderFence(
        incarnation=supervisor._PROVIDER_INCARNATION,
        generation=1,
        fingerprint="fp-local",
        attempt=1,
    )
    monkeypatch.setattr(
        supervisor,
        "_terminate_cleanup_handle",
        lambda *_args, **_kwargs: pytest.fail(
            "shutdown signal must not cleanup inline"
        ),
    )

    supervisor._cancel_all_provider_stops("test shutdown")

    assert event.is_set()


@pytest.mark.parametrize(
    ("initial_phase", "observation", "expected_phase", "expected_fingerprint"),
    [
        (
            "ready",
            supervisor.ProviderTruthObservation(
                provider="local",
                phase="not-desired",
                reason_code="provider-not-needed",
                detail={"active_provider": "cloud"},
            ),
            "not-desired",
            None,
        ),
        (
            "backoff",
            supervisor.ProviderTruthObservation(
                provider="local",
                phase="not-desired",
                reason_code="provider-not-needed",
                detail={"active_provider": "cloud"},
            ),
            "not-desired",
            None,
        ),
        (
            "ready",
            supervisor.ProviderTruthObservation(
                provider="local",
                phase="starting",
                reason_code="launch-requested",
                detail={"target": "new"},
                desired_fingerprint_json='{"provider":"local","target":"new"}',
                desired_fingerprint_sha256="fp-new",
                plan=_local_plan(),
                boot_required=True,
            ),
            "starting",
            "fp-new",
        ),
        (
            "host-blocked",
            supervisor.ProviderTruthObservation(
                provider="local",
                phase="starting",
                reason_code="launch-requested",
                detail={"target": "new"},
                desired_fingerprint_json='{"provider":"local","target":"new"}',
                desired_fingerprint_sha256="fp-new",
                plan=_local_plan(),
                boot_required=True,
            ),
            "starting",
            "fp-new",
        ),
        (
            "failed",
            supervisor.ProviderTruthObservation(
                provider="local",
                phase="starting",
                reason_code="launch-requested",
                detail={"target": "new"},
                desired_fingerprint_json='{"provider":"local","target":"new"}',
                desired_fingerprint_sha256="fp-new",
                plan=_local_plan(),
                boot_required=True,
            ),
            "starting",
            "fp-new",
        ),
        (
            "artifact-not-ready",
            supervisor.ProviderTruthObservation(
                provider="local",
                phase="starting",
                reason_code="launch-requested",
                detail={"target": "new"},
                desired_fingerprint_json='{"provider":"local","target":"new"}',
                desired_fingerprint_sha256="fp-new",
                plan=_local_plan(),
                boot_required=True,
            ),
            "starting",
            "fp-new",
        ),
    ],
)
def test_continuous_reobservation_converges_from_steady_phases(
    monkeypatch,
    initial_phase: RuntimePhase,
    observation: supervisor.ProviderTruthObservation,
    expected_phase: RuntimePhase,
    expected_fingerprint: str | None,
) -> None:
    state = supervisor._provider_runtime_states["local"]
    old_plan = _local_plan()
    state.latest_phase = initial_phase
    state.latest_plan = old_plan
    state.desired_fingerprint = old_plan.desired_fingerprint_sha256
    state.retry = supervisor.ProviderRetryState(
        attempt_count=1,
        next_at=9999.0,
        desired_fingerprint=old_plan.desired_fingerprint_sha256,
    )
    state.next_truth_at = 0.0
    starts: list[int] = []

    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(
        supervisor,
        "_observe_local_provider_truth",
        lambda: observation,
    )
    monkeypatch.setattr(
        supervisor,
        "_provider_start_worker",
        lambda provider, plan_arg, fence, _cancel_event: (
            starts.append(fence.attempt)
            or supervisor.ProviderLaunchOutcome(
                status="launch-failed",
                reason_code="launch-failed",
                detail={},
            )
        ),
    )

    asyncio.run(supervisor._reconcile_local_provider_runtime([]))
    asyncio.run(supervisor._reconcile_local_provider_runtime([]))

    assert state.latest_phase == expected_phase
    assert state.desired_fingerprint == expected_fingerprint
    assert state.truth_future is None


def test_retry_cadence_exhausts_after_six_attempts(monkeypatch):
    now = 1000.0
    launches: list[int] = []
    plan = _local_plan()

    def monotonic() -> float:
        return now

    def observe():
        return supervisor.ProviderTruthObservation(
            provider="local",
            phase="starting",
            reason_code="launch-requested",
            detail={},
            desired_fingerprint_json=plan.desired_fingerprint_json,
            desired_fingerprint_sha256=plan.desired_fingerprint_sha256,
            plan=plan,
            boot_required=True,
        )

    def start_worker(provider, plan_arg, fence, _cancel_event):
        assert provider == "local"
        assert plan_arg is plan
        launches.append(fence.attempt)
        return supervisor.ProviderLaunchOutcome(
            status="launch-failed",
            reason_code="launch-failed",
            detail={"attempt": fence.attempt},
        )

    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(supervisor.time, "monotonic", monotonic)
    monkeypatch.setattr(supervisor, "_observe_local_provider_truth", observe)
    monkeypatch.setattr(supervisor, "_provider_start_worker", start_worker)

    state = supervisor._provider_runtime_states["local"]
    delays: list[float] = []

    for _ in range(40):
        asyncio.run(supervisor._reconcile_local_provider_runtime([]))
        if state.latest_phase == "backoff":
            delays.append(state.retry.next_at - now)
            now = state.retry.next_at
        if state.latest_phase == "failed":
            break

    assert launches == [1, 2, 3, 4, 5, 6]
    assert delays == [2.0, 4.0, 8.0, 16.0, 30.0]
    assert state.latest_phase == "failed"


def test_startup_gate_releases_on_window_and_reconciler_keeps_retrying(monkeypatch):
    now = supervisor.PROVIDER_STARTUP_GATE_WINDOW_SECONDS + 1.0
    queue = _FakeTaskQueue()
    plan = _local_plan()
    launches: list[int] = []

    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "backoff"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.retry.next_at = now
    monkeypatch.setattr(supervisor, "_task_queue", queue)
    monkeypatch.setattr(
        supervisor,
        "_provider_startup_gate",
        supervisor.ProviderStartupGate(started_at=0.0, required={"local"}),
    )
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: now)
    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(
        supervisor,
        "_provider_start_worker",
        lambda provider, plan_arg, fence, _cancel_event: (
            launches.append(fence.attempt)
            or supervisor.ProviderLaunchOutcome(
                status="launch-failed",
                reason_code="launch-failed",
                detail={},
            )
        ),
    )

    supervisor._release_provider_startup_gate_if_ready()

    assert queue.ready_calls == 1
    assert supervisor._provider_startup_gate.released is True

    asyncio.run(supervisor._reconcile_local_provider_runtime([]))
    asyncio.run(supervisor._reconcile_local_provider_runtime([]))

    assert launches == [2]
    assert queue.ready_calls == 1


@pytest.mark.parametrize("terminal_phase", ["ready", "host-blocked"])
def test_startup_gate_releases_on_terminal_provider_state(
    monkeypatch,
    terminal_phase: RuntimePhase,
) -> None:
    queue = _FakeTaskQueue()
    state = supervisor._provider_runtime_states["local"]
    monkeypatch.setattr(supervisor, "_task_queue", queue)
    monkeypatch.setattr(
        supervisor,
        "_provider_startup_gate",
        supervisor.ProviderStartupGate(started_at=0.0, required={"local"}),
    )
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 1.0)

    supervisor._finish_provider_startup_condition(state, terminal_phase)
    supervisor._release_provider_startup_gate_if_ready()
    supervisor._release_provider_startup_gate_if_ready()

    assert queue.ready_calls == 1
    assert supervisor._provider_startup_gate.released is True


def test_fenced_ready_result_publishes_port(monkeypatch):
    from solstone.think.providers import local_server

    plan = _local_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_plan = plan
    state.latest_phase = "starting"
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 3
    fence = supervisor._provider_fence(state, 1)
    managed = _FakeManaged()
    ports: list[tuple[str, int]] = []
    order: list[str] = []
    state.start_fence = fence
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="ready",
            reason_code="probe-ready",
            detail={"port": 45678},
            managed=managed,
        )
    )
    monkeypatch.setattr(
        supervisor,
        "write_service_port",
        lambda service, port: order.append("port") or ports.append((service, port)),
    )
    monkeypatch.setattr(
        local_server,
        "write_local_context_window",
        lambda _tokens: order.append("context"),
    )
    original_write = supervisor._write_provider_runtime

    def write_with_order(*args, **kwargs):
        order.append(f"runtime:{kwargs['phase']}")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(supervisor, "_write_provider_runtime", write_with_order)

    assert supervisor._handle_provider_start_result(state, []) is True

    assert ports == [("local", 45678)]
    assert order[:3] == ["runtime:ready", "context", "port"]
    record = read_runtime_health("local")
    assert record["phase"] == "ready"
    assert record["generation"] == 3
    assert record["attempt"] == 1
    assert record["process"]["port"] == 45678


def test_ready_ownership_write_failure_does_not_publish_port_or_ready(
    monkeypatch,
) -> None:
    plan = _local_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_plan = plan
    state.latest_phase = "starting"
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 1
    managed = _FakeManaged()
    state.start_fence = supervisor._provider_fence(state, 1)
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="ready",
            reason_code="probe-ready",
            detail={"port": 45678},
            managed=managed,
        )
    )
    writes: list[RuntimePhase] = []
    monkeypatch.setattr(
        supervisor,
        "write_service_port",
        lambda *_args, **_kwargs: pytest.fail("port must not be published"),
    )

    def failed_ready_write(*args, **kwargs):
        phase = kwargs["phase"]
        writes.append(phase)
        if phase == "ready":
            return None
        return {
            **read_runtime_health("local"),
            "phase": phase,
            "reason_code": kwargs["reason_code"],
            "detail": kwargs["detail"],
            "desired_fingerprint_sha256": state.desired_fingerprint,
            "incarnation": supervisor._PROVIDER_INCARNATION,
            "generation": state.generation,
            "attempt": state.retry.attempt_count,
            "process": kwargs.get("process"),
            "updated_at": "2026-07-19T00:00:00+00:00",
            "owner": {"test": "failed-ready-write"},
        }

    monkeypatch.setattr(supervisor, "_write_provider_runtime", failed_ready_write)

    assert supervisor._handle_provider_start_result(state, []) is True

    assert writes == ["ready", "state-unavailable"]
    assert state.latest_phase == "state-unavailable"
    assert supervisor.read_service_port("local") is None
    managed.terminate.assert_called_once()
    managed.cleanup.assert_called_once_with()


def test_mlx_ready_clears_stale_local_context_and_capacity_cache(
    monkeypatch,
) -> None:
    from solstone.think.providers import local_server

    journal = Path(supervisor.get_journal())
    health = journal / "health"
    health.mkdir(parents=True, exist_ok=True)
    (health / "local.ctx").write_text("32768", encoding="utf-8")
    supervisor.write_service_port("local", 11111)
    monkeypatch.setattr(local_server, "fetch_props", lambda _port: None)
    local_server.reset_parallel_slots_cache()
    assert local_server.read_server_capacity().parallel_slots == 2

    plan = _mlx_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_plan = plan
    state.latest_phase = "starting"
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 1
    managed = _FakeManaged(supervisor.MLX_SERVER_PROCESS_NAME)
    state.start_fence = supervisor._provider_fence(state, 1)
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="ready",
            reason_code="probe-ready",
            detail={"port": 45678},
            managed=managed,
        )
    )

    assert supervisor._handle_provider_start_result(state, []) is True

    assert not (health / "local.ctx").exists()
    assert supervisor.read_service_port("local") == 45678
    assert local_server.read_server_capacity().parallel_slots == 1
    assert local_server.read_server_capacity().source == "default"


def test_local_capacity_observed_before_ready_is_reset_on_ready(
    monkeypatch,
) -> None:
    from solstone.think.providers import local_server

    monkeypatch.setattr(local_server, "fetch_props", lambda _port: None)
    local_server.reset_parallel_slots_cache()
    assert local_server.read_server_capacity().parallel_slots == 1

    plan = _cuda_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_plan = plan
    state.latest_phase = "starting"
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 1
    managed = _FakeManaged()
    state.start_fence = supervisor._provider_fence(state, 1)
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="ready",
            reason_code="probe-ready",
            detail={"port": 45678},
            managed=managed,
        )
    )

    assert supervisor._handle_provider_start_result(state, []) is True

    assert supervisor.read_service_port("local") == 45678
    assert local_server.read_local_context_window() == plan.context_tokens
    assert local_server.read_server_capacity().parallel_slots == 2
    assert local_server.read_server_capacity().source == "local_ctx"


def test_fenced_parakeet_ready_result_writes_placement_after_ownership(
    monkeypatch,
) -> None:
    from solstone.think.providers import parakeet_server

    plan = _parakeet_plan("vulkan")
    state = supervisor._provider_runtime_states["parakeet"]
    state.latest_plan = plan
    state.latest_phase = "starting"
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 1
    managed = _FakeManaged(supervisor.PARAKEET_SERVER_PROCESS_NAME)
    ports: list[tuple[str, int]] = []
    order: list[str] = []
    state.start_fence = supervisor._provider_fence(state, 1)
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="ready",
            reason_code="probe-ready",
            detail={"port": 45678, "placement": "gpu"},
            managed=managed,
        )
    )
    monkeypatch.setattr(
        supervisor,
        "write_service_port",
        lambda service, port: order.append("port") or ports.append((service, port)),
    )
    monkeypatch.setattr(
        parakeet_server,
        "write_parakeet_placement",
        lambda placement: order.append(f"placement:{placement}"),
    )
    original_write = supervisor._write_provider_runtime

    def write_with_order(*args, **kwargs):
        order.append(f"runtime:{kwargs['phase']}")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(supervisor, "_write_provider_runtime", write_with_order)

    assert supervisor._handle_provider_start_result(state, []) is True

    assert ports == [("parakeet-cpp", 45678)]
    assert order[:3] == ["runtime:ready", "placement:gpu", "port"]


def test_boot_incarnation_invalidates_late_start_result(monkeypatch):
    plan = _local_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_plan = plan
    state.latest_phase = "starting"
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.attempt_count = 1
    state.generation = 1
    stale_fence = supervisor.ProviderFence(
        incarnation="old-boot",
        generation=1,
        fingerprint=plan.desired_fingerprint_sha256,
        attempt=1,
    )
    managed = _FakeManaged()
    state.start_fence = stale_fence
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="ready",
            reason_code="probe-ready",
            detail={"port": 45678},
            managed=managed,
        )
    )
    published: list[tuple[str, int]] = []
    monkeypatch.setattr(
        supervisor,
        "write_service_port",
        lambda service, port: published.append((service, port)),
    )

    assert supervisor._handle_provider_start_result(state, []) is True

    assert published == []
    managed.terminate.assert_called_once()
    managed.cleanup.assert_called_once_with()
    assert read_runtime_health("local")["reason_code"] == "stale-result-ignored"


def test_superseded_attempt_cannot_publish_or_clear_newer_port(monkeypatch):
    plan = _local_plan()
    state = supervisor._provider_runtime_states["local"]
    state.latest_plan = plan
    state.latest_phase = "ready"
    state.desired_fingerprint = "fp-new"
    state.retry.attempt_count = 2
    state.generation = 2
    supervisor.write_service_port("local", 22222)
    write_runtime_health(
        _runtime_record(
            "local",
            phase="ready",
            fingerprint="fp-new",
            generation=2,
            attempt=2,
            process={
                "name": supervisor.LOCAL_SERVER_PROCESS_NAME,
                "pid": 12345,
                "ref": "ref-new",
                "port": 22222,
            },
        )
    )
    old_fence = supervisor.ProviderFence(
        incarnation=supervisor._PROVIDER_INCARNATION,
        generation=1,
        fingerprint="fp-old",
        attempt=1,
    )
    old_managed = _FakeManaged()
    state.start_fence = old_fence
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="ready",
            reason_code="probe-ready",
            detail={"port": 11111},
            managed=old_managed,
        )
    )
    published: list[tuple[str, int]] = []
    monkeypatch.setattr(
        supervisor,
        "write_service_port",
        lambda service, port: published.append((service, port)),
    )

    assert supervisor._handle_provider_start_result(state, []) is True

    assert published == []
    assert supervisor.read_service_port("local") == 22222
    old_managed.terminate.assert_called_once()
    old_managed.cleanup.assert_called_once_with()


@pytest.mark.parametrize(
    ("provider", "plan", "managed_name", "detail"),
    [
        (
            "local",
            _local_plan(),
            supervisor.LOCAL_SERVER_PROCESS_NAME,
            {"port": 11111},
        ),
        (
            "parakeet",
            _parakeet_plan("vulkan"),
            supervisor.PARAKEET_SERVER_PROCESS_NAME,
            {"port": 11111, "placement": "gpu"},
        ),
    ],
)
def test_superseded_ready_result_writes_no_context_or_placement(
    monkeypatch,
    provider: str,
    plan: supervisor.LocalServerLaunchPlan | supervisor.ParakeetServerLaunchPlan,
    managed_name: str,
    detail: dict[str, Any],
) -> None:
    from solstone.think.providers import local_server, parakeet_server

    state = supervisor._provider_runtime_states[provider]
    state.latest_plan = plan
    state.latest_phase = "ready"
    state.desired_fingerprint = "fp-new"
    state.retry.attempt_count = 2
    state.generation = 2
    state.start_fence = supervisor.ProviderFence(
        incarnation=supervisor._PROVIDER_INCARNATION,
        generation=1,
        fingerprint="fp-old",
        attempt=1,
    )
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="ready",
            reason_code="probe-ready",
            detail=detail,
            managed=_FakeManaged(managed_name),
        )
    )
    context_writes: list[int] = []
    placement_writes: list[str] = []
    monkeypatch.setattr(
        local_server,
        "write_local_context_window",
        lambda tokens: context_writes.append(tokens),
    )
    monkeypatch.setattr(
        parakeet_server,
        "write_parakeet_placement",
        lambda placement: placement_writes.append(placement),
    )

    assert supervisor._handle_provider_start_result(state, []) is True

    assert context_writes == []
    assert placement_writes == []


def test_provider_reconcilers_keep_local_and_parakeet_state_independent(
    monkeypatch,
) -> None:
    local_plan = _local_plan()
    parakeet_plan = _parakeet_plan()
    launches: list[tuple[str, int]] = []
    local = supervisor._provider_runtime_states["local"]
    parakeet = supervisor._provider_runtime_states["parakeet"]
    local.latest_phase = "starting"
    local.latest_plan = local_plan
    local.desired_fingerprint = local_plan.desired_fingerprint_sha256
    local.retry.desired_fingerprint = local_plan.desired_fingerprint_sha256
    local.next_truth_at = 9999.0
    parakeet.latest_phase = "starting"
    parakeet.latest_plan = parakeet_plan
    parakeet.desired_fingerprint = parakeet_plan.desired_fingerprint_sha256
    parakeet.retry.desired_fingerprint = parakeet_plan.desired_fingerprint_sha256
    parakeet.next_truth_at = 9999.0

    def start_worker(provider, _plan_arg, fence, _cancel_event):
        launches.append((provider, fence.attempt))
        return supervisor.ProviderLaunchOutcome(
            status="launch-failed",
            reason_code="launch-failed",
            detail={"provider": provider},
        )

    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(supervisor, "_provider_start_worker", start_worker)

    asyncio.run(supervisor._reconcile_local_provider_runtime([]))
    asyncio.run(supervisor._reconcile_local_provider_runtime([]))
    asyncio.run(supervisor._reconcile_parakeet_provider_runtime([]))
    asyncio.run(supervisor._reconcile_parakeet_provider_runtime([]))

    assert launches == [("local", 1), ("parakeet", 1)]
    assert local.latest_phase == "backoff"
    assert parakeet.latest_phase == "backoff"
    assert local.retry.desired_fingerprint == local_plan.desired_fingerprint_sha256
    assert parakeet.retry.desired_fingerprint == (
        parakeet_plan.desired_fingerprint_sha256
    )


def test_spawn_failure_leaves_no_port_file(monkeypatch):
    from solstone.think.providers import local_server

    plan = _local_plan()
    monkeypatch.setattr(
        local_server, "write_local_context_window", lambda _tokens: None
    )
    monkeypatch.setattr(
        supervisor,
        "_launch_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("spawn failed")),
    )

    outcome = supervisor.start_local_server(plan, _FakeReservation(port=34567))

    assert outcome.status == "launch-failed"
    assert supervisor.read_service_port("local") is None


@pytest.mark.parametrize(
    "backend",
    ["vulkan", "cuda", "mlx", "parakeet-vulkan", "parakeet-cpu"],
)
@pytest.mark.parametrize("status", ["warmup-timeout", "exited", "launch-failed"])
def test_non_ready_outcome_cleanup_runs_before_backoff_record(
    monkeypatch,
    backend: str,
    status: supervisor.LaunchOutcomeStatus,
) -> None:
    provider = "parakeet" if backend.startswith("parakeet") else "local"
    state = supervisor._provider_runtime_states[provider]
    state.latest_phase = "starting"
    state.desired_fingerprint = f"fp-{backend}"
    state.retry.attempt_count = 1
    state.retry.desired_fingerprint = state.desired_fingerprint
    state.generation = 1
    fence = supervisor._provider_fence(state, 1)
    managed_name = (
        supervisor.PARAKEET_SERVER_PROCESS_NAME
        if provider == "parakeet"
        else (
            supervisor.MLX_SERVER_PROCESS_NAME
            if backend == "mlx"
            else supervisor.LOCAL_SERVER_PROCESS_NAME
        )
    )
    managed = _FakeManaged(managed_name)
    state.start_fence = fence
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status=status,
            reason_code=(
                "warmup-timeout"
                if status == "warmup-timeout"
                else ("process-exited" if status == "exited" else "launch-failed")
            ),
            detail={"backend": backend, "port": 45678},
            managed=managed,
        )
    )
    order: list[str] = []

    monkeypatch.setattr(
        supervisor,
        "_terminate_cleanup_handle",
        lambda managed_arg, *, reason, state_name=None: order.append(
            f"cleanup:{managed_arg.name}:{reason}"
        ),
    )
    original_write = supervisor._write_provider_runtime

    def write_with_order(*args, **kwargs):
        order.append(f"write:{kwargs['phase']}")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(supervisor, "_write_provider_runtime", write_with_order)

    assert supervisor._handle_provider_start_result(state, []) is True

    assert order[0].startswith(f"cleanup:{managed_name}:")
    assert order[1] == "write:backoff"
    assert state.latest_phase == "backoff"


def test_non_ready_cleanup_runs_before_failed_record(monkeypatch):
    state = supervisor._provider_runtime_states["local"]
    state.latest_phase = "starting"
    state.desired_fingerprint = "fp-local"
    state.retry.attempt_count = len(supervisor.PROVIDER_RETRY_SCHEDULE_SECONDS)
    state.generation = 1
    fence = supervisor._provider_fence(
        state, len(supervisor.PROVIDER_RETRY_SCHEDULE_SECONDS)
    )
    managed = _FakeManaged()
    state.start_fence = fence
    state.start_future = _future_with(
        supervisor.ProviderLaunchOutcome(
            status="warmup-timeout",
            reason_code="warmup-timeout",
            detail={"port": 45678},
            managed=managed,
        )
    )
    order: list[str] = []
    monkeypatch.setattr(
        supervisor,
        "_terminate_cleanup_handle",
        lambda managed_arg, *, reason, state_name=None: order.append("cleanup"),
    )
    original_write = supervisor._write_provider_runtime

    def write_with_order(*args, **kwargs):
        order.append(f"write:{kwargs['phase']}")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(supervisor, "_write_provider_runtime", write_with_order)

    assert supervisor._handle_provider_start_result(state, []) is True

    assert order[:2] == ["cleanup", "write:failed"]
    assert state.latest_phase == "failed"


def test_launch_helper_returns_reserved_port_without_publishing(monkeypatch):
    from solstone.think.providers import local_server, local_vulkan

    plan = _local_plan()
    managed = _FakeManaged()
    ports: list[tuple[str, int]] = []
    spawned: list[list[str]] = []

    monkeypatch.setattr(
        supervisor,
        "write_service_port",
        lambda service, port: ports.append((service, port)),
    )
    monkeypatch.setattr(
        local_server,
        "write_local_context_window",
        lambda _tokens: None,
    )
    monkeypatch.setattr(
        local_server,
        "_probe_health",
        lambda _port: (local_server.STATE_READY, None),
    )
    monkeypatch.setattr(local_server, "fetch_props", lambda _port: None)
    monkeypatch.setattr(local_vulkan, "device_local_used_mib", lambda _index: None)
    monkeypatch.setattr(
        supervisor,
        "_launch_process",
        lambda name, cmd, **_kwargs: spawned.append(cmd) or managed,
    )

    assert not hasattr(plan, "port")

    outcome = supervisor.start_local_server(plan, _FakeReservation(port=45678))

    assert outcome.status == "ready"
    assert outcome.managed is managed
    assert ports == []
    assert spawned[0][spawned[0].index("--port") + 1] == "45678"
    assert RUNTIME_PHASES >= {"starting", "backoff", "ready"}
