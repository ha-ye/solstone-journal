# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from solstone.think import supervisor
from solstone.think.providers import local_vulkan, parakeet_install, parakeet_server


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


def test_start_parakeet_server_vulkan_crash_falls_back_to_cpu(
    monkeypatch,
) -> None:
    monkeypatch.delenv("GGML_VK_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor, "linux_stt_uses_parakeet_cpp", lambda: True)
    monkeypatch.setattr(supervisor, "_configured_parakeet_device", lambda: "auto")
    gpu = local_vulkan.VulkanDevice(
        2,
        "NVIDIA Test GPU",
        local_vulkan.VK_TYPE_DISCRETE,
        8192,
    )
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [gpu])
    monkeypatch.setattr(local_vulkan, "select_device", lambda devices: devices[0])
    monkeypatch.setattr(local_vulkan, "classify", lambda _device: "discrete")

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

    launches = []

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

    result = supervisor.start_parakeet_server()

    assert result is launches[1]["managed"]
    assert len(launches) == 2
    assert launches[0]["name"] == supervisor.PARAKEET_SERVER_PROCESS_NAME
    assert launches[0]["restart"] is True
    assert launches[0]["cmd"][0] == "/tmp/vulkan/parakeet-server"
    assert launches[0]["env"]["GGML_VK_VISIBLE_DEVICES"] == "2"
    assert launches[1]["cmd"][0] == "/tmp/cpu/parakeet-server"
    assert "GGML_VK_VISIBLE_DEVICES" not in launches[1]["env"]
    assert launches[0]["managed"].cleanup_called is True
    assert terminated[0][0] is launches[0]["managed"]
    assert ports == [("parakeet-cpp", 45123)]


@pytest.mark.parametrize(
    ("sys_platform", "machine", "backend", "available_bytes", "google_key", "expected"),
    [
        ("linux", "x86_64", None, 5 * 1024**3, False, True),
        ("linux", "x86_64", None, 3 * 1024**3, False, False),
        ("linux", "x86_64", None, 3 * 1024**3, True, False),
        ("linux", "x86_64", "parakeet", 3 * 1024**3, False, True),
        ("linux", "x86_64", "parakeet-cpp", 3 * 1024**3, False, True),
        ("linux", "x86_64", "whisper", 5 * 1024**3, False, False),
        ("linux", "x86_64", "revai", 5 * 1024**3, False, False),
        ("linux", "x86_64", "gemini", 5 * 1024**3, False, False),
        ("linux", "aarch64", None, 5 * 1024**3, False, False),
        ("darwin", "arm64", None, 5 * 1024**3, False, False),
    ],
)
def test_linux_stt_uses_parakeet_cpp_truth_table(
    monkeypatch,
    sys_platform: str,
    machine: str,
    backend: str | None,
    available_bytes: int,
    google_key: bool,
    expected: bool,
):
    monkeypatch.setattr(supervisor.sys, "platform", sys_platform)
    monkeypatch.setattr(supervisor.platform, "machine", lambda: machine)
    config = {"transcribe": {"backend": backend}} if backend is not None else {}
    monkeypatch.setattr(supervisor, "read_journal_config", lambda: config)
    monkeypatch.setattr(supervisor, "read_available_bytes", lambda: available_bytes)
    monkeypatch.setattr(supervisor, "stt_local_floor_bytes", lambda: 4 * 1024**3)
    monkeypatch.setattr(supervisor, "local_stt_backend", lambda: "parakeet")
    if google_key:
        monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    else:
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    assert supervisor.linux_stt_uses_parakeet_cpp() is expected


def test_start_parakeet_server_early_returns_for_non_linux(monkeypatch) -> None:
    monkeypatch.setattr(supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(supervisor.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        supervisor,
        "read_journal_config",
        lambda: pytest.fail("config should not be read off-linux"),
    )

    assert supervisor.start_parakeet_server() is None


def test_start_parakeet_server_early_returns_for_other_backend(monkeypatch) -> None:
    monkeypatch.setattr(supervisor.sys, "platform", "linux")
    monkeypatch.setattr(supervisor.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        supervisor,
        "read_journal_config",
        lambda: {"transcribe": {"backend": "gemini"}},
    )

    assert supervisor.start_parakeet_server() is None
