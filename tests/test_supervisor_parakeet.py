# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

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

_LaunchRecord = dict[str, Any]


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
    library_dirs: tuple[Path, ...] = (Path("/parakeet/lib"),),
) -> supervisor.ParakeetServerLaunchPlan:
    return supervisor.ParakeetServerLaunchPlan(
        binary_backend=backend,
        env_updates=env_updates or {},
        gpu_index=gpu_index,
        binary_path=Path(f"/tmp/{backend}/parakeet-server"),
        model_path=Path("/tmp/model.gguf"),
        threads=6,
        library_dirs=library_dirs,
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
        supervisor.PARAKEET_SERVER_PROCESS_NAME in supervisor._LOCAL_SERVER_PROCTITLES
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


def test_parakeet_runtime_library_dirs_aliases_bundled_libgomp(
    tmp_path, monkeypatch
) -> None:
    site_dir = tmp_path / "site-packages"
    libs_dir = site_dir / "scikit_learn.libs"
    libs_dir.mkdir(parents=True)
    bundled = libs_dir / "libgomp-e985bcbb.so.1.0.0"
    bundled.write_text("runtime")
    journal = tmp_path / "journal"

    monkeypatch.setattr(supervisor, "_site_package_search_dirs", lambda: [site_dir])
    monkeypatch.setattr(supervisor, "get_journal", lambda: str(journal))

    result = supervisor._parakeet_runtime_library_dirs()

    assert result == [journal / "cache" / "providers" / "parakeet" / "lib"]
    alias = result[0] / "libgomp.so.1"
    assert alias.is_symlink()
    assert alias.resolve() == bundled.resolve()


def test_with_library_path_prepends_dirs() -> None:
    env = {"LD_LIBRARY_PATH": "/existing"}

    result = supervisor._with_library_path(env, [Path("/parakeet/lib")])

    assert result["LD_LIBRARY_PATH"] == "/parakeet/lib:/existing"
    assert env["LD_LIBRARY_PATH"] == "/existing"


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
    assert launches[0]["env"]["LD_LIBRARY_PATH"] == "/parakeet/lib"
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
    assert parakeet_server.read_parakeet_placement() == "cpu"
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
    assert parakeet_server.read_parakeet_placement() == "gpu"


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
    assert parakeet_server.read_parakeet_placement() == "cpu"


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
    assert parakeet_server.read_parakeet_placement() == "cpu"


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
    config = {"transcribe": transcribe_config} if transcribe_config else {}
    monkeypatch.setattr(supervisor, "read_journal_config", lambda: config)
    monkeypatch.setattr(supervisor, "read_available_bytes", lambda: available_bytes)
    monkeypatch.setattr(supervisor, "stt_local_floor_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(supervisor, "local_stt_backend", lambda: local_backend)
    monkeypatch.setattr(
        "solstone.think.services.spp.confidential_provenance",
        lambda: {"enabled_at": "2026-05-24T00:00:00Z"} if confidential else None,
    )
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

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
        lambda: {"transcribe": {"backend": "confidential"}},
    )
    monkeypatch.setattr(
        "solstone.think.services.spp.confidential_provenance",
        lambda: {"enabled_at": "2026-05-24T00:00:00Z"},
    )

    observation = supervisor._observe_parakeet_provider_truth()

    assert observation.phase == "not-desired"
    assert observation.reason_code == "confidential-backend-selected"
    assert parakeet_server.read_parakeet_placement() == "gpu"


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
    monkeypatch.setattr(
        "solstone.think.services.spp.confidential_provenance",
        lambda: None,
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

    supervisor._start_parakeet_bootstrap_if_needed("missing")

    reacquired = acquire_install_lease("parakeet", journal_path=journal)
    assert reacquired is not None
    reacquired.release()
    assert _LateThread.instances
    _LateThread.instances[0].target()
    assert install_parakeet == []


def test_parakeet_bootstrap_worker_requests_start_after_install(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    requests: list[str] = []
    lease = _FakeLease()
    attempt_status = {"attempt_id": "attempt"}
    ack = supervisor.threading.Event()

    monkeypatch.setattr(
        parakeet_install,
        "install_parakeet",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        supervisor, "_request_parakeet_server_start", lambda: requests.append("start")
    )

    supervisor._run_parakeet_bootstrap_worker(
        lease=lease,
        attempt_status=attempt_status,
        ack=ack,
    )

    assert calls == [
        {"journal_path": None, "lease": lease, "attempt_status": attempt_status}
    ]
    assert requests == ["start"]
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
    requests: list[str] = []
    journal_path = tmp_path / "original"
    lease = _FakeLease()
    attempt_status = {"attempt_id": "attempt"}
    ack = supervisor.threading.Event()

    def fake_install_parakeet(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(parakeet_install, "install_parakeet", fake_install_parakeet)
    monkeypatch.setattr(
        supervisor, "_request_parakeet_server_start", lambda: requests.append("start")
    )

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
    assert requests == ["start"]
    assert ack.is_set()
    assert lease.released is True
