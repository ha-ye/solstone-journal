# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ctypes
import subprocess
from pathlib import Path

from solstone.think.providers import local_vulkan

FIXTURE_DIR = Path("tests/fixtures/llama_server")


def _device(index: int, name: str, device_type: int, vram_mib: int = 1024):
    return local_vulkan.VulkanDevice(index, name, device_type, vram_mib)


def test_json_fixture_selects_discrete_despite_lower_vram():
    devices = local_vulkan._devices_from_json(
        (FIXTURE_DIR / "vulkan_devices.json").read_text(encoding="utf-8")
    )

    selected = local_vulkan.select_device(devices)

    assert selected is not None
    assert selected.index == 1
    assert selected.name == "NVIDIA GeForce GTX 1660 Ti"
    assert selected.vram_mib == 6390
    assert not local_vulkan.is_hardware_device(devices[2])
    assert local_vulkan.classify(devices[2]) == "software"


def test_is_hardware_device_accepts_discrete_and_integrated():
    assert local_vulkan.is_hardware_device(
        _device(0, "NVIDIA GeForce RTX", local_vulkan.VK_TYPE_DISCRETE)
    )
    assert local_vulkan.is_hardware_device(
        _device(1, "Intel(R) Graphics", local_vulkan.VK_TYPE_INTEGRATED)
    )


def test_is_hardware_device_rejects_cpu_virtual_other_and_software_names():
    rejected = [
        _device(0, "llvmpipe (LLVM)", local_vulkan.VK_TYPE_CPU),
        _device(1, "lavapipe", local_vulkan.VK_TYPE_INTEGRATED),
        _device(2, "SwiftShader Device", local_vulkan.VK_TYPE_DISCRETE),
        _device(3, "Virtual GPU", local_vulkan.VK_TYPE_VIRTUAL),
        _device(4, "Other GPU", local_vulkan.VK_TYPE_OTHER),
    ]

    assert [local_vulkan.is_hardware_device(device) for device in rejected] == [
        False,
        False,
        False,
        False,
        False,
    ]


def test_select_device_prefers_lowest_index_discrete_then_integrated():
    devices = [
        _device(0, "Intel(R) Graphics", local_vulkan.VK_TYPE_INTEGRATED),
        _device(3, "NVIDIA B", local_vulkan.VK_TYPE_DISCRETE),
        _device(2, "NVIDIA A", local_vulkan.VK_TYPE_DISCRETE),
    ]

    assert local_vulkan.select_device(devices) == devices[2]

    integrated = [devices[0]]
    assert local_vulkan.select_device(integrated) == devices[0]


def test_select_device_override_must_name_present_hardware_device():
    devices = [
        _device(0, "Intel(R) Graphics", local_vulkan.VK_TYPE_INTEGRATED),
        _device(1, "llvmpipe", local_vulkan.VK_TYPE_CPU),
    ]

    assert local_vulkan.select_device(devices, override_index=0) == devices[0]
    assert local_vulkan.select_device(devices, override_index=1) is None
    assert local_vulkan.select_device(devices, override_index=99) is None


def test_select_device_returns_none_when_empty_or_non_hardware():
    assert local_vulkan.select_device([]) is None
    assert (
        local_vulkan.select_device([_device(0, "llvmpipe", local_vulkan.VK_TYPE_CPU)])
        is None
    )


def test_enumerate_gpus_parses_subprocess_json(monkeypatch):
    output = (FIXTURE_DIR / "vulkan_devices.json").read_text(encoding="utf-8")

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(_args[0], 0, stdout=output, stderr="")

    monkeypatch.setattr(local_vulkan.subprocess, "run", fake_run)

    devices, probe_ok = local_vulkan._enumerate_gpus()

    assert [device.index for device in devices] == [0, 1, 2]
    assert probe_ok is True


def test_enumerate_gpus_returns_empty_on_timeout(monkeypatch):
    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("probe", 10)

    monkeypatch.setattr(local_vulkan.subprocess, "run", fake_run)

    devices, probe_ok = local_vulkan._enumerate_gpus()
    assert devices == []
    assert probe_ok is False


def test_enumerate_gpus_returns_empty_on_nonzero_or_bad_json(monkeypatch):
    monkeypatch.setattr(
        local_vulkan.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            _args[0], 1, stdout="", stderr="boom"
        ),
    )

    devices, probe_ok = local_vulkan._enumerate_gpus()
    assert devices == []
    assert probe_ok is False

    monkeypatch.setattr(
        local_vulkan.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            _args[0], 0, stdout="{not-json", stderr=""
        ),
    )

    devices, probe_ok = local_vulkan._enumerate_gpus()
    assert devices == []
    assert probe_ok is False


def test_enumerate_gpus_valid_empty_result_keeps_probe_ok(monkeypatch):
    monkeypatch.setattr(
        local_vulkan.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            _args[0], 0, stdout="[]", stderr=""
        ),
    )

    devices, probe_ok = local_vulkan._enumerate_gpus()

    assert devices == []
    assert probe_ok is True


def test_budget_instance_create_info_requests_vulkan_1_1():
    """The VRAM-budget probe must build its instance with a non-NULL
    pApplicationInfo requesting Vulkan >= 1.1, so vkGetPhysicalDeviceMemoryProperties2
    (a 1.1 core call) is conformant and the VK_EXT_memory_budget pNext chain is honored.
    """
    create_info, app_info = local_vulkan._make_instance_create_info()

    # pApplicationInfo must be non-NULL...
    assert create_info.pApplicationInfo
    # ...and must address the very app-info object returned, proving the caller
    # can retain it (pApplicationInfo stores only an address, not a reference).
    assert create_info.pApplicationInfo == ctypes.addressof(app_info)
    # ...and that app-info must request at least Vulkan 1.1.
    referenced = ctypes.cast(
        create_info.pApplicationInfo,
        ctypes.POINTER(local_vulkan._VkApplicationInfo),
    ).contents
    assert referenced.sType == local_vulkan._VK_STRUCTURE_TYPE_APPLICATION_INFO
    assert referenced.apiVersion >= 4198400


def test_device_local_used_mib_parses_subprocess_json(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="4685", stderr="")

    monkeypatch.setattr(local_vulkan.subprocess, "run", fake_run)

    assert local_vulkan.device_local_used_mib(1) == 4685
    cmd, kwargs = calls[0]
    assert cmd[-2:] == ["budget", "1"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == local_vulkan._PROBE_TIMEOUT_S
    assert kwargs["check"] is False
    assert "env" not in kwargs


def test_device_local_used_mib_returns_none_on_probe_failures(monkeypatch):
    def completed(stdout: str, returncode: int = 0):
        return lambda cmd, **_kwargs: subprocess.CompletedProcess(
            cmd, returncode, stdout=stdout, stderr=""
        )

    for stdout in ("null", "{not-json", '"4685"', "true", "-1"):
        monkeypatch.setattr(local_vulkan.subprocess, "run", completed(stdout))
        assert local_vulkan.device_local_used_mib(1) is None

    monkeypatch.setattr(local_vulkan.subprocess, "run", completed("", returncode=1))
    assert local_vulkan.device_local_used_mib(1) is None

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("probe", 10)

    monkeypatch.setattr(local_vulkan.subprocess, "run", timeout)
    assert local_vulkan.device_local_used_mib(1) is None
