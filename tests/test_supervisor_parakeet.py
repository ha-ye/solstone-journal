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
        self.cleanup_called = False

    def cleanup(self) -> None:
        self.cleanup_called = True


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
    def fake_ensure(backend: str):
        return Path(f"/tmp/{backend}/parakeet-server"), Path("/tmp/model.gguf")

    monkeypatch.setattr(parakeet_install, "ensure_artifacts_installed", fake_ensure)
    monkeypatch.setattr(supervisor, "find_available_port", lambda: 45123)
    ports: list[tuple[str, int]] = []
    monkeypatch.setattr(
        supervisor,
        "write_service_port",
        lambda service, port: ports.append((service, port)),
    )
    monkeypatch.setattr(supervisor, "parakeet_physical_thread_count", lambda: 6)
    monkeypatch.setattr(
        supervisor, "_parakeet_runtime_library_dirs", lambda: [Path("/parakeet/lib")]
    )
    monkeypatch.setattr(
        parakeet_server, "probe_state", lambda: (parakeet_server.STATE_READY, None)
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

    plan = supervisor.resolve_parakeet_server_launch_plan("cpu", gpu)

    assert plan == supervisor.ParakeetServerLaunchPlan("cpu", {}, None)


def test_resolve_launch_plan_auto_uses_selected_gpu() -> None:
    gpu = SimpleNamespace(index=2)

    plan = supervisor.resolve_parakeet_server_launch_plan("auto", gpu)

    assert plan == supervisor.ParakeetServerLaunchPlan(
        "vulkan", {"GGML_VK_VISIBLE_DEVICES": "2"}, 2
    )


def test_resolve_launch_plan_auto_without_gpu_uses_cpu() -> None:
    plan = supervisor.resolve_parakeet_server_launch_plan("auto", None)

    assert plan == supervisor.ParakeetServerLaunchPlan("cpu", {}, None)


def test_resolve_launch_plan_rejects_invalid_device() -> None:
    with pytest.raises(ValueError, match="auto"):
        supervisor.resolve_parakeet_server_launch_plan("bogus", None)


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


def test_start_parakeet_server_vulkan_crash_falls_back_to_cpu(
    monkeypatch,
    tmp_path,
    caplog,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    monkeypatch.delenv("GGML_VK_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor, "linux_stt_uses_parakeet_cpp", lambda: True)
    monkeypatch.setattr(supervisor, "_configured_parakeet_device", lambda: "auto")
    monkeypatch.setattr(supervisor, "is_local_provider_needed", lambda: True)
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.resolve_local_endpoint",
        lambda: SimpleNamespace(is_bundled=True),
    )
    gpu = local_vulkan.VulkanDevice(
        2,
        "NVIDIA Test GPU",
        local_vulkan.VK_TYPE_DISCRETE,
        12288,
    )
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [gpu])
    monkeypatch.setattr(local_vulkan, "select_device", lambda devices: devices[0])
    monkeypatch.setattr(local_vulkan, "classify", lambda _device: "discrete")
    monkeypatch.setattr(
        local_cuda, "probe_nvidia_gpu", lambda: _nvidia_probe(vram_mib=12288)
    )

    def fake_ensure(backend: str):
        return Path(f"/tmp/{backend}/parakeet-server"), Path("/tmp/model.gguf")

    monkeypatch.setattr(parakeet_install, "ensure_artifacts_installed", fake_ensure)
    monkeypatch.setattr(supervisor, "find_available_port", lambda: 45123)
    ports: list[tuple[str, int]] = []
    monkeypatch.setattr(
        supervisor,
        "write_service_port",
        lambda service, port: ports.append((service, port)),
    )
    monkeypatch.setattr(supervisor, "parakeet_physical_thread_count", lambda: 6)
    monkeypatch.setattr(
        supervisor, "_parakeet_runtime_library_dirs", lambda: [Path("/parakeet/lib")]
    )
    monkeypatch.setattr(
        parakeet_server, "probe_state", lambda: (parakeet_server.STATE_READY, None)
    )
    terminated = []
    monkeypatch.setattr(
        supervisor,
        "_terminate_managed",
        lambda managed, timeout, *, reason: terminated.append(
            (managed, timeout, reason)
        ),
    )

    launches: list[_LaunchRecord] = []

    def fake_launch_process(
        name, cmd, *, restart=False, shutdown_timeout=15, ref=None, env=None
    ):
        managed = _FakeManaged(9, 9) if not launches else _FakeManaged(None, None)
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

    caplog.set_level(logging.INFO)
    result = supervisor.start_parakeet_server()

    assert result is launches[1]["managed"]
    assert len(launches) == 2
    assert launches[0]["name"] == supervisor.PARAKEET_SERVER_PROCESS_NAME
    assert launches[0]["restart"] is True
    assert launches[0]["cmd"][0] == "/tmp/vulkan/parakeet-server"
    assert launches[0]["env"]["GGML_VK_VISIBLE_DEVICES"] == "2"
    assert launches[0]["env"]["LD_LIBRARY_PATH"] == "/parakeet/lib"
    _assert_att_context(launches[0])
    assert launches[1]["cmd"][0] == "/tmp/cpu/parakeet-server"
    assert "GGML_VK_VISIBLE_DEVICES" not in launches[1]["env"]
    assert launches[1]["env"]["LD_LIBRARY_PATH"] == "/parakeet/lib"
    _assert_att_context(launches[1])
    assert launches[0]["managed"].cleanup_called is True
    assert terminated[0][0] is launches[0]["managed"]
    assert ports == [("parakeet-cpp", 45123)]
    assert parakeet_server.read_parakeet_placement() == "cpu"
    assert _launch_log("vulkan") in caplog.text
    assert _launch_log("cpu") in caplog.text


def test_start_parakeet_server_forces_cpu_on_small_single_discrete_bundled_brain(
    monkeypatch,
    tmp_path,
    caplog,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    monkeypatch.delenv("GGML_VK_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor, "linux_stt_uses_parakeet_cpp", lambda: True)
    monkeypatch.setattr(supervisor, "_configured_parakeet_device", lambda: "auto")
    monkeypatch.setattr(supervisor, "is_local_provider_needed", lambda: True)
    monkeypatch.setattr(
        "solstone.think.providers.local_endpoint.resolve_local_endpoint",
        lambda: SimpleNamespace(is_bundled=True),
    )
    gpu = local_vulkan.VulkanDevice(
        2,
        "NVIDIA Test GPU",
        local_vulkan.VK_TYPE_DISCRETE,
        6144,
    )
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [gpu])
    monkeypatch.setattr(local_vulkan, "select_device", lambda devices: devices[0])
    monkeypatch.setattr(local_vulkan, "classify", lambda _device: "discrete")
    monkeypatch.setattr(
        local_cuda, "probe_nvidia_gpu", lambda: _nvidia_probe(vram_mib=6144)
    )
    launches: list[_LaunchRecord] = []
    ports = _patch_ready_parakeet_launch(monkeypatch, launches)

    caplog.set_level(logging.INFO)
    result = supervisor.start_parakeet_server()

    assert result is launches[0]["managed"]
    assert len(launches) == 1
    assert launches[0]["cmd"][0] == "/tmp/cpu/parakeet-server"
    assert "GGML_VK_VISIBLE_DEVICES" not in launches[0]["env"]
    _assert_att_context(launches[0])
    assert ports == [("parakeet-cpp", 45123)]
    assert parakeet_server.read_parakeet_placement() == "cpu"
    assert (
        "parakeet-server auto placement resolved to CPU: tier=floor "
        "tier_resident_mib=4147 parakeet_worst_case_mib=2947 margin_mib=1024 "
        "required_mib=8118 gpu_vram_mib=6144 placement=cpu"
    ) in caplog.text
    assert _launch_log("cpu") in caplog.text


def test_start_parakeet_server_brain_lane_inactive_keeps_auto_vulkan(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor, "linux_stt_uses_parakeet_cpp", lambda: True)
    monkeypatch.setattr(supervisor, "_configured_parakeet_device", lambda: "auto")
    monkeypatch.setattr(supervisor, "is_local_provider_needed", lambda: False)
    gpu = local_vulkan.VulkanDevice(
        2,
        "NVIDIA Test GPU",
        local_vulkan.VK_TYPE_DISCRETE,
        6144,
    )
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [gpu])
    monkeypatch.setattr(local_vulkan, "select_device", lambda devices: devices[0])
    monkeypatch.setattr(local_vulkan, "classify", lambda _device: "discrete")
    monkeypatch.setattr(
        local_cuda, "probe_nvidia_gpu", lambda: _nvidia_probe(vram_mib=6144)
    )
    launches: list[_LaunchRecord] = []
    _patch_ready_parakeet_launch(monkeypatch, launches)

    result = supervisor.start_parakeet_server()

    assert result is launches[0]["managed"]
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
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor, "linux_stt_uses_parakeet_cpp", lambda: True)
    monkeypatch.setattr(supervisor, "_configured_parakeet_device", lambda: "cpu")
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

    result = supervisor.start_parakeet_server()

    assert result is launches[0]["managed"]
    assert launches[0]["cmd"][0] == "/tmp/cpu/parakeet-server"
    _assert_att_context(launches[0])
    assert parakeet_server.read_parakeet_placement() == "cpu"


def test_parakeet_attention_context_overrides_ambient_env(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    monkeypatch.setenv(PARAKEET_ATT_CONTEXT_ENV, "0")
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor, "linux_stt_uses_parakeet_cpp", lambda: True)
    monkeypatch.setattr(supervisor, "_configured_parakeet_device", lambda: "cpu")
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

    result = supervisor.start_parakeet_server()

    assert result is launches[0]["managed"]
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

    assert supervisor.start_parakeet_server() is None
    assert parakeet_server.read_parakeet_placement() is None


def test_start_parakeet_server_early_returns_for_other_backend(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    parakeet_server.write_parakeet_placement("gpu")
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        supervisor,
        "read_journal_config",
        lambda: {"transcribe": {"backend": "confidential"}},
    )
    monkeypatch.setattr(
        "solstone.think.services.spp.confidential_provenance",
        lambda: {"enabled_at": "2026-05-24T00:00:00Z"},
    )

    assert supervisor.start_parakeet_server() is None
    assert parakeet_server.read_parakeet_placement() is None


def test_start_parakeet_server_starts_background_install_when_missing(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    parakeet_server.write_parakeet_placement("gpu")
    monkeypatch.setattr(supervisor, "linux_stt_uses_parakeet_cpp", lambda: True)
    monkeypatch.setattr(supervisor, "_configured_parakeet_device", lambda: "cpu")
    started: list[str] = []

    class _Thread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def is_alive(self):
            return False

        def start(self):
            started.append(self.name)

    class _Event:
        def set(self):
            return None

        def wait(self, timeout=None):
            return True

    monkeypatch.setattr(supervisor.threading, "Thread", _Thread)
    monkeypatch.setattr(supervisor.threading, "Event", _Event)
    monkeypatch.setattr(
        parakeet_install,
        "ensure_artifacts_installed",
        lambda _backend: (_ for _ in ()).throw(
            parakeet_install.ParakeetProviderError(
                "model_missing", "Parakeet model is not installed."
            )
        ),
    )
    monkeypatch.setattr(
        parakeet_install,
        "inspect_readiness",
        lambda: _parakeet_readiness(),
    )

    assert supervisor.start_parakeet_server() is None
    assert started == ["parakeet-cpp-provider-bootstrap"]
    assert parakeet_server.read_parakeet_placement() is None


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
