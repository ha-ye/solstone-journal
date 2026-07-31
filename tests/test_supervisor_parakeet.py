# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import concurrent.futures
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from solstone.think import supervisor
from solstone.think.providers import (
    local_cuda,
    local_vulkan,
    parakeet_install,
    parakeet_server,
)
from solstone.think.providers.artifact_proof import ReadinessOutcome
from solstone.think.providers.parakeet_placement import (
    PARAKEET_ATT_CONTEXT_ENV,
    PARAKEET_ATT_CONTEXT_FRAMES,
)
from tests.helpers.journal_config import seed_journal_config
from tests.helpers.module_mocks import module_mock

_LaunchRecord = dict[str, Any]


def _confidential_block() -> dict[str, Any]:
    return {
        "enabled_at": "2026-05-24T00:00:00Z",
        "account_id": "acct-test",
        "endpoint_url": "https://spp.example.test",
        "served_model_id": "confidential-model",
        "credential_fingerprint_sha256": "fingerprint",
    }


def _stranded_confidential_stt_config() -> dict[str, Any]:
    return {
        "services": {"confidential": _confidential_block()},
        "providers": {"local": {}},
        "transcribe": {},
    }


def _usable_confidential_stt_config(
    transcribe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "services": {"confidential": _confidential_block()},
        "providers": {
            "local": {
                "endpoint_url": "https://spp.example.test/v1",
                "served_model_id": "confidential-model",
                "credential": "confidential-credential",
            }
        },
        "transcribe": transcribe or {},
    }


def _install_supervisor_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, Any],
) -> None:
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    seed_journal_config(config, journal)
    monkeypatch.setattr(supervisor, "read_journal_config", lambda: config)


@pytest.fixture(autouse=True)
def _isolate_supervisor_threading(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "threading",
        module_mock(supervisor.threading),
    )


class _InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class _FakeLease:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


def _parakeet_readiness(
    *,
    install_state: str = "idle",
    binary_installed: bool = False,
    model_installed: bool = False,
) -> ReadinessOutcome:
    ready = binary_installed and model_installed
    return ReadinessOutcome(
        provider="parakeet",
        status="ready" if ready else "missing-or-mismatched",
        reason_code="ready" if ready else "manifest_missing",
        target={},
        install={
            "install_state": install_state,
            "install_error": None,
            "error_code": None,
            "attempt_id": None,
            "progress_bytes_received": None,
            "progress_bytes_total": None,
            "last_transition_at": None,
            "last_progress_at": None,
        },
        host={},
        artifacts={
            "binary_installed": binary_installed,
            "model_installed": model_installed,
        },
        proof={},
    )


def _assert_att_context(launch: _LaunchRecord) -> None:
    assert launch["env"][PARAKEET_ATT_CONTEXT_ENV] == str(PARAKEET_ATT_CONTEXT_FRAMES)


def _launch_log(backend: str) -> str:
    return (
        "parakeet-server launch "
        f"backend={backend} attention=local "
        f"att_context_frames={PARAKEET_ATT_CONTEXT_FRAMES}"
    )


@pytest.fixture(autouse=True)
def _reset_vulkan_detect_cache():
    local_vulkan.reset_detect_cache()
    yield
    local_vulkan.reset_detect_cache()


class _FakeProcess:
    def __init__(self, poll_value: int | None, returncode: int | None = None):
        self.pid = 12345
        self.returncode = returncode
        self._poll_value = poll_value

    def poll(self) -> int | None:
        return self._poll_value


class _FakeManaged:
    def __init__(self, poll_value: int | None, returncode: int | None = None):
        self.name = supervisor.PARAKEET_SERVER_PROCESS_NAME
        self.process = _FakeProcess(poll_value, returncode)
        self.ref = "ref-parakeet"
        self.cleanup_called = False

    def cleanup(self) -> None:
        self.cleanup_called = True


class _FakeReservation:
    def __init__(self, port: int = 45123):
        self.port = port
        self.closed = False
        self.released = False

    def release_for_spawn(self) -> int:
        self.closed = True
        self.released = True
        return self.port

    def close(self) -> None:
        self.closed = True


def _parakeet_plan(
    backend: str = "cpu",
    *,
    env_updates: dict[str, str] | None = None,
    gpu_index: int | None = None,
) -> supervisor.ParakeetServerLaunchPlan:
    return supervisor.ParakeetServerLaunchPlan(
        binary_backend=backend,
        env_updates=env_updates or {},
        gpu_index=gpu_index,
        binary_path=Path(f"/tmp/{backend}/parakeet-server"),
        model_path=Path("/tmp/model.gguf"),
        threads=6,
        desired_fingerprint_json='{"provider":"parakeet"}',
        desired_fingerprint_sha256="fp-parakeet",
        placement="gpu" if backend == "vulkan" else "cpu",
    )


def _nvidia_probe(
    *,
    vram_mib: int,
    memory_source: str = local_cuda.MEMORY_SOURCE_NVIDIA_VRAM,
) -> local_cuda.NvidiaProbe:
    return local_cuda.NvidiaProbe(
        index=0,
        compute_cap="sm_75",
        driver_cuda_version=13,
        vram_mib=vram_mib,
        tiering_memory_mib=vram_mib,
        memory_source=memory_source,
        detected=True,
    )


def _patch_ready_parakeet_launch(
    monkeypatch,
    launches: list[_LaunchRecord],
    *,
    poll_sequence: list[tuple[int | None, int | None]] | None = None,
) -> list[tuple[str, int]]:
    ports: list[tuple[str, int]] = []
    monkeypatch.setattr(
        supervisor,
        "write_service_port",
        lambda service, port: ports.append((service, port)),
    )
    monkeypatch.setattr(
        parakeet_server,
        "_probe_health",
        lambda _port: (parakeet_server.STATE_READY, None),
    )

    sequence = poll_sequence or [(None, None)]

    def fake_launch_process(
        name, cmd, *, restart=False, shutdown_timeout=15, ref=None, env=None
    ):
        index = min(len(launches), len(sequence) - 1)
        poll_value, returncode = sequence[index]
        managed = _FakeManaged(poll_value, returncode)
        launches.append(
            {
                "name": name,
                "cmd": cmd,
                "restart": restart,
                "env": env,
                "managed": managed,
            }
        )
        return managed

    monkeypatch.setattr(supervisor, "_launch_process", fake_launch_process)
    return ports


def test_parakeet_server_is_sweepable_orphan_name() -> None:
    assert (
        supervisor.PARAKEET_SERVER_PROCESS_NAME
        in supervisor._SWEEPABLE_PROVIDER_PROCTITLES
    )
    assert supervisor._is_sweepable_orphan_name("parakeet-server") is True


def test_resolve_launch_plan_cpu_ignores_gpu() -> None:
    gpu = SimpleNamespace(index=2)

    plan = supervisor.resolve_parakeet_server_launch_plan(
        "cpu",
        gpu,
        binary_path=Path("/tmp/cpu/parakeet-server"),
        model_path=Path("/tmp/model.gguf"),
        threads=6,
    )

    assert plan.binary_backend == "cpu"
    assert plan.env_updates == {}
    assert plan.gpu_index is None
    assert plan.placement == "cpu"


def test_resolve_launch_plan_auto_uses_selected_gpu() -> None:
    gpu = SimpleNamespace(index=2)

    plan = supervisor.resolve_parakeet_server_launch_plan(
        "auto",
        gpu,
        binary_path=Path("/tmp/vulkan/parakeet-server"),
        model_path=Path("/tmp/model.gguf"),
        threads=6,
    )

    assert plan.binary_backend == "vulkan"
    assert plan.env_updates == {"GGML_VK_VISIBLE_DEVICES": "2"}
    assert plan.gpu_index == 2
    assert plan.placement == "gpu"


def test_resolve_launch_plan_auto_without_gpu_uses_cpu() -> None:
    plan = supervisor.resolve_parakeet_server_launch_plan(
        "auto",
        None,
        binary_path=Path("/tmp/cpu/parakeet-server"),
        model_path=Path("/tmp/model.gguf"),
        threads=6,
    )

    assert plan.binary_backend == "cpu"
    assert plan.env_updates == {}
    assert plan.gpu_index is None
    assert plan.placement == "cpu"


def test_resolve_launch_plan_rejects_invalid_device() -> None:
    with pytest.raises(ValueError, match="auto"):
        supervisor.resolve_parakeet_server_launch_plan(
            "bogus",
            None,
            binary_path=Path("/tmp/cpu/parakeet-server"),
            model_path=Path("/tmp/model.gguf"),
            threads=6,
        )


def test_parakeet_physical_thread_count_uses_physical(monkeypatch) -> None:
    monkeypatch.setattr(supervisor.psutil, "cpu_count", lambda logical=False: 6)
    monkeypatch.setattr(
        supervisor.os,
        "cpu_count",
        lambda: pytest.fail("logical count should not be primary"),
    )

    assert supervisor.parakeet_physical_thread_count() == 6


@pytest.mark.parametrize(
    ("logical_count", "expected"),
    [
        (8, 4),
        (None, 1),
    ],
)
def test_parakeet_physical_thread_count_fallback_halves_logical(
    monkeypatch, logical_count, expected
) -> None:
    monkeypatch.setattr(supervisor.psutil, "cpu_count", lambda logical=False: None)
    monkeypatch.setattr(supervisor.os, "cpu_count", lambda: logical_count)

    assert supervisor.parakeet_physical_thread_count() == expected


def test_build_parakeet_cmd_load_bearing_invariants() -> None:
    cmd = supervisor._build_parakeet_cmd(
        Path("/tmp/parakeet-server"),
        Path("/tmp/model.gguf"),
        45123,
        6,
    )

    assert "127.0.0.1" in cmd
    assert "0.0.0.0" not in cmd
    assert cmd[cmd.index("--model") + 1] == "/tmp/model.gguf"
    assert cmd[cmd.index("--threads") + 1] == "6"


def test_start_parakeet_server_vulkan_crash_returns_exited_cleanup_handle(
    monkeypatch,
    tmp_path,
    caplog,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    monkeypatch.delenv("GGML_VK_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    terminated = []
    monkeypatch.setattr(
        supervisor,
        "_terminate_managed",
        lambda managed, timeout, *, reason: terminated.append(
            (managed, timeout, reason)
        ),
    )

    launches: list[_LaunchRecord] = []
    ports = _patch_ready_parakeet_launch(
        monkeypatch,
        launches,
        poll_sequence=[(9, 9)],
    )
    plan = _parakeet_plan(
        "vulkan",
        env_updates={"GGML_VK_VISIBLE_DEVICES": "2"},
        gpu_index=2,
    )

    caplog.set_level(logging.INFO)
    result = supervisor.start_parakeet_server(plan, _FakeReservation())

    assert result.status == "exited"
    assert result.reason_code == "process-exited"
    assert result.managed is launches[0]["managed"]
    assert len(launches) == 1
    assert launches[0]["name"] == supervisor.PARAKEET_SERVER_PROCESS_NAME
    assert launches[0]["restart"] is False
    assert launches[0]["cmd"][0] == "/tmp/vulkan/parakeet-server"
    assert launches[0]["env"]["GGML_VK_VISIBLE_DEVICES"] == "2"
    assert "LD_LIBRARY_PATH" not in launches[0]["env"]
    _assert_att_context(launches[0])
    assert ports == []
    assert terminated == []
    assert launches[0]["managed"].cleanup_called is False
    assert parakeet_server.read_parakeet_placement() is None
    assert _launch_log("vulkan") in caplog.text


def test_start_parakeet_server_forces_cpu_on_small_single_discrete_bundled_brain(
    monkeypatch,
    tmp_path,
    caplog,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    monkeypatch.delenv("GGML_VK_VISIBLE_DEVICES", raising=False)
    launches: list[_LaunchRecord] = []
    ports = _patch_ready_parakeet_launch(monkeypatch, launches)
    plan = _parakeet_plan("cpu")

    caplog.set_level(logging.INFO)
    result = supervisor.start_parakeet_server(plan, _FakeReservation())

    assert result.status == "ready"
    assert result.managed is launches[0]["managed"]
    assert len(launches) == 1
    assert launches[0]["cmd"][0] == "/tmp/cpu/parakeet-server"
    assert "GGML_VK_VISIBLE_DEVICES" not in launches[0]["env"]
    _assert_att_context(launches[0])
    assert ports == []
    assert result.detail["placement"] == "cpu"
    assert parakeet_server.read_parakeet_placement() is None
    assert _launch_log("cpu") in caplog.text


def test_start_parakeet_server_brain_lane_inactive_keeps_auto_vulkan(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    launches: list[_LaunchRecord] = []
    _patch_ready_parakeet_launch(monkeypatch, launches)
    plan = _parakeet_plan(
        "vulkan",
        env_updates={"GGML_VK_VISIBLE_DEVICES": "2"},
        gpu_index=2,
    )

    result = supervisor.start_parakeet_server(plan, _FakeReservation())

    assert result.status == "ready"
    assert result.managed is launches[0]["managed"]
    assert len(launches) == 1
    assert launches[0]["cmd"][0] == "/tmp/vulkan/parakeet-server"
    assert launches[0]["env"]["GGML_VK_VISIBLE_DEVICES"] == "2"
    _assert_att_context(launches[0])
    assert result.detail["placement"] == "gpu"
    assert parakeet_server.read_parakeet_placement() is None


def test_start_parakeet_server_explicit_cpu_skips_auto_placement(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    monkeypatch.setattr(
        local_vulkan,
        "detect_gpus",
        lambda: pytest.fail("explicit CPU should not enumerate Vulkan devices"),
    )
    monkeypatch.setattr(
        local_cuda,
        "probe_nvidia_gpu",
        lambda: pytest.fail("explicit CPU should not probe NVIDIA"),
    )
    launches: list[_LaunchRecord] = []
    _patch_ready_parakeet_launch(monkeypatch, launches)
    plan = _parakeet_plan("cpu")

    result = supervisor.start_parakeet_server(plan, _FakeReservation())

    assert result.status == "ready"
    assert result.managed is launches[0]["managed"]
    assert launches[0]["cmd"][0] == "/tmp/cpu/parakeet-server"
    _assert_att_context(launches[0])
    assert result.detail["placement"] == "cpu"
    assert parakeet_server.read_parakeet_placement() is None


def test_parakeet_attention_context_overrides_ambient_env(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    monkeypatch.setenv(PARAKEET_ATT_CONTEXT_ENV, "0")
    monkeypatch.setattr(
        local_vulkan,
        "detect_gpus",
        lambda: pytest.fail("explicit CPU should not enumerate Vulkan devices"),
    )
    monkeypatch.setattr(
        local_cuda,
        "probe_nvidia_gpu",
        lambda: pytest.fail("explicit CPU should not probe NVIDIA"),
    )
    launches: list[_LaunchRecord] = []
    _patch_ready_parakeet_launch(monkeypatch, launches)
    plan = _parakeet_plan("cpu")

    result = supervisor.start_parakeet_server(plan, _FakeReservation())

    assert result.status == "ready"
    assert result.managed is launches[0]["managed"]
    assert launches[0]["cmd"][0] == "/tmp/cpu/parakeet-server"
    _assert_att_context(launches[0])
    assert result.detail["placement"] == "cpu"
    assert parakeet_server.read_parakeet_placement() is None


@pytest.mark.parametrize(
    (
        "sys_platform",
        "machine",
        "backend",
        "available_bytes",
        "confidential",
        "confidential_audio",
        "local_backend",
        "expected",
    ),
    [
        ("linux", "x86_64", None, 5 * 1024**3, False, True, "parakeet", True),
        ("linux", "x86_64", None, 3 * 1024**3, False, True, "parakeet", False),
        (
            "linux",
            "x86_64",
            "parakeet",
            3 * 1024**3,
            False,
            True,
            "parakeet",
            True,
        ),
        (
            "linux",
            "x86_64",
            "parakeet-cpp",
            3 * 1024**3,
            False,
            True,
            "parakeet",
            True,
        ),
        ("linux", "aarch64", None, 5 * 1024**3, False, True, "parakeet", True),
        (
            "linux",
            "aarch64",
            "parakeet",
            3 * 1024**3,
            False,
            True,
            "parakeet",
            True,
        ),
        ("darwin", "arm64", None, 5 * 1024**3, False, True, "parakeet", False),
        (
            "linux",
            "x86_64",
            "parakeet",
            3 * 1024**3,
            True,
            True,
            "parakeet",
            True,
        ),
        ("linux", "x86_64", None, 3 * 1024**3, True, True, "parakeet", False),
        ("linux", "x86_64", None, 3 * 1024**3, True, False, "parakeet", True),
        ("linux", "x86_64", None, 3 * 1024**3, True, True, None, False),
    ],
)
def test_linux_stt_uses_parakeet_cpp_truth_table(
    monkeypatch,
    sys_platform: str,
    machine: str,
    backend: str | None,
    available_bytes: int,
    confidential: bool,
    confidential_audio: bool,
    local_backend: str | None,
    expected: bool,
):
    monkeypatch.setattr(supervisor.sys, "platform", sys_platform)
    monkeypatch.setattr(supervisor.platform, "machine", lambda: machine)
    transcribe_config = {}
    if backend is not None:
        transcribe_config["backend"] = backend
    if not confidential_audio:
        transcribe_config["confidential_audio"] = False
    if confidential:
        config = _usable_confidential_stt_config(transcribe_config)
    else:
        config = {"transcribe": transcribe_config} if transcribe_config else {}
    monkeypatch.setattr(supervisor, "read_journal_config", lambda: config)
    monkeypatch.setattr(supervisor, "read_available_bytes", lambda: available_bytes)
    monkeypatch.setattr(supervisor, "stt_local_floor_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(supervisor, "local_stt_backend", lambda: local_backend)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    assert supervisor.linux_stt_uses_parakeet_cpp() is expected


@pytest.mark.parametrize(
    ("available_bytes", "expected"),
    [
        (2 * 1024**3, False),
        (8 * 1024**3, True),
    ],
)
def test_linux_stt_uses_parakeet_cpp_stranded_config_follows_local_resources(
    monkeypatch,
    tmp_path,
    available_bytes: int,
    expected: bool,
) -> None:
    config = _stranded_confidential_stt_config()
    _install_supervisor_config(tmp_path, monkeypatch, config)
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(supervisor, "read_available_bytes", lambda: available_bytes)
    monkeypatch.setattr(supervisor, "stt_local_floor_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(supervisor, "local_stt_backend", lambda: "parakeet")

    assert supervisor.linux_stt_uses_parakeet_cpp() is expected


def test_start_parakeet_server_early_returns_for_non_linux(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    parakeet_server.write_parakeet_placement("gpu")
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(supervisor.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        supervisor,
        "read_journal_config",
        lambda: pytest.fail("config should not be read off-linux"),
    )

    observation = supervisor._observe_parakeet_provider_truth()

    assert observation.phase == "not-desired"
    assert observation.reason_code == "provider-not-needed"
    assert parakeet_server.read_parakeet_placement() == "gpu"


def test_start_parakeet_server_early_returns_for_other_backend(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    parakeet_server.write_parakeet_placement("gpu")
    monkeypatch.setattr(supervisor, "_parakeet_platform_can_host", lambda: True)
    monkeypatch.setattr(
        supervisor,
        "read_journal_config",
        lambda: _usable_confidential_stt_config({"backend": "confidential"}),
    )

    observation = supervisor._observe_parakeet_provider_truth()

    assert observation.phase == "not-desired"
    assert observation.reason_code == "confidential-backend-selected"
    assert parakeet_server.read_parakeet_placement() == "gpu"


def test_parakeet_truth_stranded_low_ram_reports_host_blocked(
    monkeypatch, tmp_path
) -> None:
    _install_supervisor_config(
        tmp_path,
        monkeypatch,
        _stranded_confidential_stt_config(),
    )
    monkeypatch.setattr(supervisor, "_parakeet_platform_can_host", lambda: True)
    monkeypatch.setattr(supervisor, "read_available_bytes", lambda: 2 * 1024**3)
    monkeypatch.setattr(supervisor, "stt_local_floor_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(supervisor, "local_stt_backend", lambda: "parakeet")

    observation = supervisor._observe_parakeet_provider_truth()

    assert observation.phase == "host-blocked"
    assert observation.reason_code == "host-admission-blocked"
    assert observation.detail["stt_admission_latch"]["blocked"] is True


def test_parakeet_truth_stranded_adequate_ram_desires_parakeet(
    monkeypatch, tmp_path
) -> None:
    _install_supervisor_config(
        tmp_path,
        monkeypatch,
        _stranded_confidential_stt_config(),
    )
    monkeypatch.setattr(supervisor, "_parakeet_platform_can_host", lambda: True)
    monkeypatch.setattr(supervisor, "read_available_bytes", lambda: 8 * 1024**3)
    monkeypatch.setattr(supervisor, "stt_local_floor_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(supervisor, "local_stt_backend", lambda: "parakeet")
    monkeypatch.setattr(
        parakeet_install,
        "target_fingerprint",
        lambda *, journal_path=None: {"provider": "parakeet"},
    )
    monkeypatch.setattr(
        parakeet_install,
        "inspect_readiness",
        lambda journal_path=None: _parakeet_readiness(),
    )

    observation = supervisor._observe_parakeet_provider_truth()

    assert observation.phase == "artifact-not-ready"
    assert observation.reason_code == "artifact-missing"


def test_parakeet_truth_reports_artifact_not_ready_when_missing(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    parakeet_server.write_parakeet_placement("gpu")
    monkeypatch.setattr(supervisor, "_parakeet_platform_can_host", lambda: True)
    monkeypatch.setattr(
        supervisor,
        "read_journal_config",
        lambda: {"transcribe": {"backend": "parakeet"}},
    )
    monkeypatch.setattr(
        parakeet_install,
        "target_fingerprint",
        lambda *, journal_path=None: {"provider": "parakeet"},
    )
    monkeypatch.setattr(
        parakeet_install,
        "inspect_readiness",
        lambda journal_path=None: _parakeet_readiness(),
    )

    observation = supervisor._observe_parakeet_provider_truth()

    assert observation.phase == "artifact-not-ready"
    assert observation.reason_code == "artifact-missing"
    assert observation.plan is None
    assert parakeet_server.read_parakeet_placement() == "gpu"


def test_parakeet_bootstrap_ack_timeout_cancels_late_worker(
    monkeypatch,
    tmp_path,
) -> None:
    from solstone.think.providers.install_lease import acquire_install_lease

    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr(
        parakeet_install, "inspect_readiness", lambda: _parakeet_readiness()
    )
    monkeypatch.setattr(
        parakeet_install,
        "target_fingerprint",
        lambda journal_path=None: {"provider": "parakeet", "unit": "test"},
    )
    monkeypatch.setattr(
        "solstone.think.providers.install_state.begin_or_replace_install_attempt",
        lambda *_args, **_kwargs: {
            "provider": "parakeet",
            "install_state": "downloading",
            "attempt_id": "attempt",
        },
    )

    class _LateThread:
        instances = []

        def __init__(self, *, target, name, daemon):
            type(self).instances.append(self)
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            return None

    class _Event:
        def __init__(self):
            self._set = False

        def set(self):
            self._set = True

        def is_set(self):
            return self._set

        def wait(self, timeout=None):
            return False

    install_parakeet = []
    monkeypatch.setattr(supervisor.threading, "Thread", _LateThread)
    monkeypatch.setattr(supervisor.threading, "Event", _Event)
    monkeypatch.setattr(
        parakeet_install,
        "install_parakeet",
        lambda **kwargs: install_parakeet.append(kwargs),
    )

    assert supervisor._start_parakeet_bootstrap_if_needed("missing") is False

    reacquired = acquire_install_lease("parakeet", journal_path=journal)
    assert reacquired is not None
    reacquired.release()
    assert _LateThread.instances
    _LateThread.instances[0].target()
    assert install_parakeet == []


def test_parakeet_bootstrap_lease_held_is_retryable_noop(monkeypatch, tmp_path) -> None:
    from solstone.think.providers.install_lease import acquire_install_lease

    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr(
        parakeet_install, "inspect_readiness", lambda: _parakeet_readiness()
    )
    lease = acquire_install_lease("parakeet", journal_path=journal)
    assert lease is not None
    try:
        assert supervisor._start_parakeet_bootstrap_if_needed("missing") is False
    finally:
        lease.release()


def test_parakeet_bootstrap_prepare_failure_is_retryable_noop(
    monkeypatch,
    tmp_path,
) -> None:
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr(
        parakeet_install, "inspect_readiness", lambda: _parakeet_readiness()
    )
    monkeypatch.setattr(
        parakeet_install,
        "target_fingerprint",
        lambda journal_path=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert supervisor._start_parakeet_bootstrap_if_needed("missing") is False


def test_parakeet_bootstrap_thread_start_failure_is_retryable_noop(
    monkeypatch,
    tmp_path,
) -> None:
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr(
        parakeet_install, "inspect_readiness", lambda: _parakeet_readiness()
    )
    monkeypatch.setattr(
        parakeet_install,
        "target_fingerprint",
        lambda journal_path=None: {"provider": "parakeet", "unit": "test"},
    )
    monkeypatch.setattr(
        "solstone.think.providers.install_state.begin_or_replace_install_attempt",
        lambda *_args, **_kwargs: {
            "provider": "parakeet",
            "install_state": "downloading",
            "attempt_id": "attempt",
        },
    )

    class _BrokenThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            raise RuntimeError("thread failed")

    monkeypatch.setattr(supervisor.threading, "Thread", _BrokenThread)

    assert supervisor._start_parakeet_bootstrap_if_needed("missing") is False


def test_parakeet_bootstrap_abandons_before_lease_when_target_changed(
    monkeypatch,
    tmp_path,
) -> None:
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr(
        parakeet_install, "inspect_readiness", lambda: _parakeet_readiness()
    )
    state = supervisor.ProviderRuntimeState("parakeet")
    state.latest_phase = "artifact-not-ready"
    state.desired_fingerprint = "fp-old"
    state.generation = 1
    monkeypatch.setattr(
        supervisor,
        "_provider_runtime_states",
        {
            "local": supervisor.ProviderRuntimeState("local"),
            "parakeet": state,
        },
    )
    fence = supervisor._provider_fence(state, 0)
    state.latest_phase = "not-desired"
    state.desired_fingerprint = None
    state.generation = 2
    monkeypatch.setattr(
        "solstone.think.providers.install_lease.acquire_install_lease",
        lambda *_args, **_kwargs: pytest.fail("lease must not be acquired"),
    )

    assert (
        supervisor._start_parakeet_bootstrap_if_needed("missing", fence, "fp-old")
        is False
    )


def test_parakeet_bootstrap_abandons_before_lease_when_fingerprint_changed(
    monkeypatch,
    tmp_path,
) -> None:
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr(
        parakeet_install, "inspect_readiness", lambda: _parakeet_readiness()
    )
    state = supervisor.ProviderRuntimeState("parakeet")
    state.latest_phase = "artifact-not-ready"
    state.desired_fingerprint = "fp-old"
    state.generation = 1
    monkeypatch.setattr(
        supervisor,
        "_provider_runtime_states",
        {
            "local": supervisor.ProviderRuntimeState("local"),
            "parakeet": state,
        },
    )
    fence = supervisor._provider_fence(state, 0)
    state.desired_fingerprint = "fp-new"
    state.generation = 2
    monkeypatch.setattr(
        "solstone.think.providers.install_lease.acquire_install_lease",
        lambda *_args, **_kwargs: pytest.fail("lease must not be acquired"),
    )

    assert (
        supervisor._start_parakeet_bootstrap_if_needed("missing", fence, "fp-old")
        is False
    )


def test_parakeet_bootstrap_rechecks_matching_target_before_lease(
    monkeypatch,
    tmp_path,
) -> None:
    journal = tmp_path / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr(
        parakeet_install, "inspect_readiness", lambda: _parakeet_readiness()
    )
    state = supervisor.ProviderRuntimeState("parakeet")
    state.latest_phase = "artifact-not-ready"
    state.desired_fingerprint = "fp-parakeet"
    state.generation = 1
    monkeypatch.setattr(
        supervisor,
        "_provider_runtime_states",
        {
            "local": supervisor.ProviderRuntimeState("local"),
            "parakeet": state,
        },
    )
    fence = supervisor._provider_fence(state, 0)
    lease_calls = []
    monkeypatch.setattr(
        "solstone.think.providers.install_lease.acquire_install_lease",
        lambda *_args, **_kwargs: lease_calls.append("lease") or None,
    )

    assert (
        supervisor._start_parakeet_bootstrap_if_needed(
            "missing",
            fence,
            "fp-parakeet",
        )
        is False
    )
    assert lease_calls == ["lease"]


def test_parakeet_bootstrap_noop_result_retries_on_later_observation(
    monkeypatch,
) -> None:
    calls: list[str] = []
    state = supervisor.ProviderRuntimeState("parakeet")
    plan = _parakeet_plan("cpu")
    monkeypatch.setattr(
        supervisor,
        "_provider_runtime_states",
        {
            "local": supervisor.ProviderRuntimeState("local"),
            "parakeet": state,
        },
    )
    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(
        supervisor, "_write_provider_runtime", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        supervisor,
        "_start_parakeet_bootstrap_if_needed",
        lambda reason, *_args: calls.append(reason) and False,
    )

    for _ in range(2):
        state.truth_fence = supervisor._provider_fence(state, 0)
        state.truth_future = _InlineExecutor().submit(
            lambda: supervisor.ProviderTruthObservation(
                provider="parakeet",
                phase="artifact-not-ready",
                reason_code="artifact-missing",
                detail={
                    "install_state": "idle",
                    "install_acquisition_allowed": True,
                },
                desired_fingerprint_json=plan.desired_fingerprint_json,
                desired_fingerprint_sha256=plan.desired_fingerprint_sha256,
                boot_required=True,
            )
        )
        assert supervisor._handle_provider_truth_result(state) is True

    assert calls == ["artifact-missing", "artifact-missing"]
    assert state.parakeet_bootstrap_requested_fingerprint is None


def test_parakeet_bootstrap_worker_publishes_install_fact_without_direct_start(
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []
    lease = _FakeLease()
    attempt_status = {"attempt_id": "attempt"}
    ack = supervisor.threading.Event()

    monkeypatch.setattr(
        parakeet_install,
        "install_parakeet",
        lambda **kwargs: calls.append(kwargs),
    )

    supervisor._run_parakeet_bootstrap_worker(
        lease=lease,
        attempt_status=attempt_status,
        ack=ack,
    )

    assert calls == [
        {"journal_path": None, "lease": lease, "attempt_status": attempt_status}
    ]
    assert not hasattr(supervisor, "_request_parakeet_server_start")
    assert ack.is_set()
    assert lease.released is True


def test_parakeet_bootstrap_worker_cancel_before_ack_does_not_install_or_release(
    monkeypatch,
) -> None:
    lease = _FakeLease()
    attempt_status = {"attempt_id": "attempt"}
    ack = supervisor.threading.Event()
    cancel = supervisor.threading.Event()
    cancel.set()
    transfer_lock = supervisor.threading.Lock()
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        parakeet_install,
        "install_parakeet",
        lambda **kwargs: calls.append(kwargs),
    )

    supervisor._run_parakeet_bootstrap_worker(
        lease=lease,
        attempt_status=attempt_status,
        ack=ack,
        cancel=cancel,
        transfer_lock=transfer_lock,
    )

    assert not ack.is_set()
    assert calls == []
    assert lease.released is False


def test_parakeet_bootstrap_worker_uses_captured_journal_path(
    monkeypatch, tmp_path
) -> None:
    calls: list[dict[str, Any]] = []
    journal_path = tmp_path / "original"
    lease = _FakeLease()
    attempt_status = {"attempt_id": "attempt"}
    ack = supervisor.threading.Event()

    def fake_install_parakeet(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(parakeet_install, "install_parakeet", fake_install_parakeet)

    supervisor._run_parakeet_bootstrap_worker(
        journal_path,
        lease,
        attempt_status,
        ack,
    )

    assert calls == [
        {
            "journal_path": journal_path,
            "lease": lease,
            "attempt_status": attempt_status,
        }
    ]
    assert not hasattr(supervisor, "_request_parakeet_server_start")
    assert ack.is_set()
    assert lease.released is True


def test_parakeet_bootstrap_launches_only_via_reconciliation(monkeypatch) -> None:
    bootstrap_reasons: list[str] = []
    launches: list[supervisor.ParakeetServerLaunchPlan] = []
    state = supervisor.ProviderRuntimeState("parakeet")
    plan = _parakeet_plan("cpu")

    monkeypatch.setattr(
        supervisor,
        "_provider_runtime_states",
        {
            "local": supervisor.ProviderRuntimeState("local"),
            "parakeet": state,
        },
    )
    monkeypatch.setattr(supervisor, "_provider_executor", lambda: _InlineExecutor())
    monkeypatch.setattr(
        supervisor, "_write_provider_runtime", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        supervisor,
        "_start_parakeet_bootstrap_if_needed",
        lambda reason, *_args: bootstrap_reasons.append(reason) or True,
    )

    def start_worker(provider, plan_arg, _fence, _cancel_event):
        assert provider == "parakeet"
        launches.append(plan_arg)
        return supervisor.ProviderLaunchOutcome(
            status="launch-failed",
            reason_code="launch-failed",
            detail={},
        )

    monkeypatch.setattr(supervisor, "_provider_start_worker", start_worker)

    state.truth_fence = supervisor._provider_fence(state, 0)
    state.truth_future = _InlineExecutor().submit(
        lambda: supervisor.ProviderTruthObservation(
            provider="parakeet",
            phase="artifact-not-ready",
            reason_code="artifact-missing",
            detail={
                "install_state": "idle",
                "install_acquisition_allowed": True,
            },
            desired_fingerprint_json=plan.desired_fingerprint_json,
            desired_fingerprint_sha256=plan.desired_fingerprint_sha256,
            boot_required=True,
        )
    )

    assert supervisor._handle_provider_truth_result(state) is True
    assert bootstrap_reasons == ["artifact-missing"]
    assert launches == []

    state.truth_fence = supervisor._provider_fence(state, 0)
    state.truth_future = _InlineExecutor().submit(
        lambda: supervisor.ProviderTruthObservation(
            provider="parakeet",
            phase="artifact-not-ready",
            reason_code="artifact-missing",
            detail={
                "install_state": "idle",
                "install_acquisition_allowed": True,
            },
            desired_fingerprint_json=plan.desired_fingerprint_json,
            desired_fingerprint_sha256=plan.desired_fingerprint_sha256,
            boot_required=True,
        )
    )

    assert supervisor._handle_provider_truth_result(state) is True
    assert bootstrap_reasons == ["artifact-missing"]
    assert launches == []

    state.truth_fence = supervisor._provider_fence(state, 0)
    state.truth_future = _InlineExecutor().submit(
        lambda: supervisor.ProviderTruthObservation(
            provider="parakeet",
            phase="starting",
            reason_code="launch-requested",
            detail={},
            desired_fingerprint_json=plan.desired_fingerprint_json,
            desired_fingerprint_sha256=plan.desired_fingerprint_sha256,
            plan=plan,
            boot_required=True,
        )
    )

    assert supervisor._handle_provider_truth_result(state) is True
    supervisor._submit_provider_start_if_needed(state, [])

    assert launches == [plan]


def test_ready_parakeet_host_admission_blocked_still_defers_admission_exclusive_stop(
    monkeypatch,
) -> None:
    """Pin deliberately-unchanged parakeet host-blocked stop deferral."""
    state = supervisor.ProviderRuntimeState("parakeet")
    plan = _parakeet_plan("cpu")
    state.latest_phase = "ready"
    state.latest_plan = plan
    state.desired_fingerprint = plan.desired_fingerprint_sha256
    state.retry.desired_fingerprint = plan.desired_fingerprint_sha256
    monkeypatch.setattr(
        supervisor,
        "_provider_runtime_states",
        {
            "local": supervisor.ProviderRuntimeState("local"),
            "parakeet": state,
        },
    )
    monkeypatch.setattr(
        supervisor, "_write_provider_runtime", lambda *_args, **_kwargs: None
    )
    state.truth_fence = supervisor._provider_fence(state, 0)
    state.truth_future = _InlineExecutor().submit(
        lambda: supervisor.ProviderTruthObservation(
            provider="parakeet",
            phase="host-blocked",
            reason_code="host-admission-blocked",
            detail={"host": {"reason": "stt admission pressure"}},
            desired_fingerprint_json=plan.desired_fingerprint_json,
            desired_fingerprint_sha256=plan.desired_fingerprint_sha256,
            boot_required=True,
        )
    )

    assert supervisor._handle_provider_truth_result(state) is True

    assert state.latest_phase == "stop-deferred"
    assert state.pending_stop_admission_exclusive is True
    assert state.pending_stop_target_phase == "host-blocked"
    assert state.pending_stop_target_reason_code == "host-admission-blocked"
