# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest

from solstone.think import check
from solstone.think import utils as think_utils
from solstone.think.providers import local_cuda, local_vulkan, memory

GB = 1024**3
PLACEMENT_LINE = (
    "sol thinks on your GPU; transcription runs on your CPU on this machine"
)


@pytest.fixture(autouse=True)
def _reset_vulkan_detect_cache():
    local_vulkan.reset_detect_cache()
    yield
    local_vulkan.reset_detect_cache()


def _patch_platform(
    monkeypatch: pytest.MonkeyPatch,
    *,
    os_name: str = "Linux",
    arch: str = "x86_64",
    release: str = "6.8.0",
    mac_version: str = "",
) -> None:
    monkeypatch.setattr(check.platform, "system", lambda: os_name)
    monkeypatch.setattr(check.platform, "machine", lambda: arch)
    monkeypatch.setattr(check.platform, "release", lambda: release)
    monkeypatch.setattr(
        check.platform, "mac_ver", lambda: (mac_version, ("", "", ""), "")
    )
    monkeypatch.setattr(check.platform, "python_version", lambda: "3.12.0")


def _patch_memory(
    monkeypatch: pytest.MonkeyPatch,
    *,
    total: int | None = 64 * GB,
    available: int | None = 64 * GB,
) -> None:
    monkeypatch.setattr(memory, "read_total_bytes", lambda: total)
    monkeypatch.setattr(memory, "read_available_bytes", lambda: available)


def _patch_disk(monkeypatch: pytest.MonkeyPatch, *, free: int = 500 * GB) -> None:
    monkeypatch.setattr(think_utils, "get_journal_info", lambda: ("/journal", "test"))
    monkeypatch.setattr(memory, "free_bytes", lambda _path: free)


def _patch_linux_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_platform(monkeypatch)
    _patch_memory(monkeypatch)
    _patch_disk(monkeypatch)
    monkeypatch.setattr(
        local_vulkan, "detect_gpus", lambda: [_vulkan_device(vram_mib=24576)]
    )
    monkeypatch.setattr(local_vulkan, "gpu_probe_ok", lambda: True)


def _nvidia_probe(
    *,
    compute_cap: str | None = "sm_89",
    driver_cuda_version: int | None = 13,
    vram_mib: int | None = 24576,
    tiering_memory_mib: int | None = None,
    memory_source: str = local_cuda.MEMORY_SOURCE_NVIDIA_VRAM,
    detected: bool = True,
) -> local_cuda.NvidiaProbe:
    return local_cuda.NvidiaProbe(
        index=0 if detected else None,
        compute_cap=compute_cap,
        driver_cuda_version=driver_cuda_version,
        vram_mib=vram_mib,
        tiering_memory_mib=vram_mib
        if tiering_memory_mib is None
        else tiering_memory_mib,
        memory_source=memory_source,
        detected=detected,
    )


def _undetected_probe() -> local_cuda.NvidiaProbe:
    return local_cuda.NvidiaProbe(
        index=None,
        compute_cap=None,
        driver_cuda_version=None,
        vram_mib=None,
        tiering_memory_mib=None,
        memory_source=local_cuda.MEMORY_SOURCE_UNAVAILABLE,
        detected=False,
    )


def _vulkan_device(
    *,
    index: int = 0,
    name: str = "Vulkan GPU",
    device_type: int = local_vulkan.VK_TYPE_DISCRETE,
    vram_mib: int = 8192,
) -> local_vulkan.VulkanDevice:
    return local_vulkan.VulkanDevice(
        index=index,
        name=name,
        device_type=device_type,
        vram_mib=vram_mib,
    )


def _checks(report: check.CheckReport) -> dict[str, check.FitCheck]:
    return {item.name: item for item in report.report.checks}


def test_linux_cuda_ok(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(local_cuda, "probe_nvidia_gpu", lambda: _nvidia_probe())

    result = check.build_check_report()

    checks = _checks(result)
    assert {name: item.severity for name, item in checks.items()} == {
        "platform": "ok",
        "gpu": "ok",
        "ram": "ok",
        "disk": "ok",
    }
    assert result.report.overall == "ok"
    assert result.recommended_package == "solstone-journal-cuda"

    assert check.main([]) == 0
    output = capsys.readouterr().out
    assert "solstone-journal-cuda" in output
    assert check.FEEDBACK_URL in output


def test_linux_cuda_too_small_blocks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(
        local_cuda,
        "probe_nvidia_gpu",
        lambda: _nvidia_probe(vram_mib=4096),
    )

    result = check.build_check_report()

    assert _checks(result)["gpu"].severity == "blocked"
    assert result.report.overall == "blocked"
    assert check.main([]) == 2
    capsys.readouterr()


def test_linux_x86_64_nvidia_recommends_cuda_package(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(
        local_cuda,
        "probe_nvidia_gpu",
        lambda: _nvidia_probe(compute_cap="sm_75", vram_mib=12288),
    )

    result = check.build_check_report()

    assert _checks(result)["gpu"].severity == "ok"
    assert result.report.overall == "ok"
    assert result.recommended_package == "solstone-journal-cuda"
    assert check.main([]) == 0
    output = capsys.readouterr().out
    assert "solstone-journal-cuda" in output


def test_linux_aarch64_nvidia_recommends_base_package(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_linux_ok(monkeypatch)
    _patch_platform(monkeypatch, arch="aarch64")
    monkeypatch.setattr(local_cuda, "probe_nvidia_gpu", lambda: _nvidia_probe())

    result = check.build_check_report()

    assert _checks(result)["gpu"].severity == "ok"
    assert result.report.overall == "ok"
    assert result.recommended_package == "solstone-journal"
    assert check.main([]) == 0
    output = capsys.readouterr().out
    assert "solstone-journal" in output
    assert "solstone-journal-cuda" not in output


def test_linux_recommendation_does_not_select_local_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(local_cuda, "probe_nvidia_gpu", lambda: _nvidia_probe())
    monkeypatch.setattr(
        local_cuda,
        "select_local_backend",
        lambda *_args, **_kwargs: pytest.fail("backend selection is not a check input"),
    )

    result = check.build_check_report()

    assert result.recommended_package == "solstone-journal-cuda"


def test_linux_nvidia_small_single_discrete_mentions_cpu_transcription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(
        local_cuda,
        "probe_nvidia_gpu",
        lambda: _nvidia_probe(vram_mib=6144),
    )
    monkeypatch.setattr(
        local_vulkan, "detect_gpus", lambda: [_vulkan_device(vram_mib=6144)]
    )

    result = check.build_check_report()

    gpu = _checks(result)["gpu"]
    assert gpu.severity == "ok"
    assert gpu.detail == f"NVIDIA GPU with 6 GB; {PLACEMENT_LINE}"


@pytest.mark.parametrize(
    ("probe", "devices", "probe_ok"),
    [
        (_nvidia_probe(vram_mib=12288), [_vulkan_device(vram_mib=12288)], True),
        (
            _nvidia_probe(vram_mib=6144),
            [
                _vulkan_device(index=0, vram_mib=6144),
                _vulkan_device(index=1, vram_mib=6144),
            ],
            True,
        ),
        (
            _nvidia_probe(
                vram_mib=6144,
                memory_source=local_cuda.MEMORY_SOURCE_SYSTEM_AVAILABLE,
            ),
            [_vulkan_device(vram_mib=6144)],
            True,
        ),
        (_nvidia_probe(vram_mib=16384), [_vulkan_device(vram_mib=16384)], True),
        (_nvidia_probe(vram_mib=6144), [], False),
    ],
)
def test_linux_nvidia_cpu_transcription_line_absent_outside_predicate(
    monkeypatch: pytest.MonkeyPatch,
    probe: local_cuda.NvidiaProbe,
    devices: list[local_vulkan.VulkanDevice],
    probe_ok: bool,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(local_cuda, "probe_nvidia_gpu", lambda: probe)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: devices)
    monkeypatch.setattr(local_vulkan, "gpu_probe_ok", lambda: probe_ok)

    result = check.build_check_report()

    gpu = _checks(result)["gpu"]
    assert gpu.severity == "ok"
    assert PLACEMENT_LINE not in gpu.detail


def test_linux_nvidia_vulkan_detection_exception_keeps_current_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(
        local_cuda,
        "probe_nvidia_gpu",
        lambda: _nvidia_probe(vram_mib=6144),
    )
    monkeypatch.setattr(
        local_vulkan,
        "detect_gpus",
        lambda: (_ for _ in ()).throw(RuntimeError("vulkan failed")),
    )

    result = check.build_check_report()

    gpu = _checks(result)["gpu"]
    assert gpu.severity == "ok"
    assert gpu.detail == "NVIDIA GPU with 6 GB"


def test_linux_vulkan_ok(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(local_cuda, "probe_nvidia_gpu", _undetected_probe)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [_vulkan_device()])
    monkeypatch.setattr(local_vulkan, "gpu_probe_ok", lambda: True)

    result = check.build_check_report()

    assert _checks(result)["gpu"].severity == "ok"
    assert result.recommended_package == "solstone-journal"
    assert check.main([]) == 0
    capsys.readouterr()


def test_linux_no_usable_gpu_blocks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(local_cuda, "probe_nvidia_gpu", _undetected_probe)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])
    monkeypatch.setattr(local_vulkan, "gpu_probe_ok", lambda: True)
    monkeypatch.setattr(check, "_render_nodes_present_but_inaccessible", lambda: False)

    result = check.build_check_report()

    assert _checks(result)["gpu"].severity == "blocked"
    assert result.report.overall == "blocked"
    assert check.main([]) == 2
    capsys.readouterr()


def test_linux_vulkan_probe_failed_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(local_cuda, "probe_nvidia_gpu", _undetected_probe)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])
    monkeypatch.setattr(local_vulkan, "gpu_probe_ok", lambda: False)
    monkeypatch.setattr(check, "_render_nodes_present_but_inaccessible", lambda: False)

    result = check.build_check_report()

    assert _checks(result)["gpu"].severity == "unknown"
    assert result.report.overall == "warning"
    assert check.main([]) == 1
    assert "Traceback" not in capsys.readouterr().out


def test_linux_nvidia_smi_absent_path_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(local_cuda, "probe_nvidia_gpu", _undetected_probe)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])
    monkeypatch.setattr(local_vulkan, "gpu_probe_ok", lambda: True)
    monkeypatch.setattr(check, "_render_nodes_present_but_inaccessible", lambda: False)

    result = check.build_check_report()

    assert result.report.overall == "blocked"
    assert _checks(result)["gpu"].severity == "blocked"


def test_linux_inaccessible_render_node_hints(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(local_cuda, "probe_nvidia_gpu", _undetected_probe)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])
    monkeypatch.setattr(local_vulkan, "gpu_probe_ok", lambda: True)
    monkeypatch.setattr(check, "_render_nodes_present_but_inaccessible", lambda: True)

    result = check.build_check_report()

    gpu_check = _checks(result)["gpu"]
    assert gpu_check.severity == "unknown"
    assert "usermod -aG render" in gpu_check.detail
    assert result.report.overall == "warning"
    assert check.main([]) == 1
    capsys.readouterr()


def test_linux_probe_failed_with_inaccessible_node_hints(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(local_cuda, "probe_nvidia_gpu", _undetected_probe)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])
    monkeypatch.setattr(local_vulkan, "gpu_probe_ok", lambda: False)
    monkeypatch.setattr(check, "_render_nodes_present_but_inaccessible", lambda: True)

    result = check.build_check_report()

    gpu_check = _checks(result)["gpu"]
    assert gpu_check.severity == "unknown"
    assert "usermod -aG render" in gpu_check.detail
    assert result.report.overall == "warning"
    assert check.main([]) == 1
    capsys.readouterr()


def test_linux_accessible_render_node_still_blocks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(local_cuda, "probe_nvidia_gpu", _undetected_probe)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])
    monkeypatch.setattr(local_vulkan, "gpu_probe_ok", lambda: True)
    monkeypatch.setattr(check, "_render_nodes_present_but_inaccessible", lambda: False)

    result = check.build_check_report()

    gpu_check = _checks(result)["gpu"]
    assert gpu_check.severity == "blocked"
    assert "no usable GPU" in gpu_check.detail
    assert result.report.overall == "blocked"
    assert check.main([]) == 2
    capsys.readouterr()


def test_render_nodes_present_but_inaccessible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(check.glob, "glob", lambda _pat: [])
    assert check._render_nodes_present_but_inaccessible() is False

    monkeypatch.setattr(check.glob, "glob", lambda _pat: ["/dev/dri/renderD128"])
    monkeypatch.setattr(check.os, "access", lambda _path, _mode: True)
    assert check._render_nodes_present_but_inaccessible() is False

    monkeypatch.setattr(check.os, "access", lambda _path, _mode: False)
    assert check._render_nodes_present_but_inaccessible() is True


def test_macos_arm64_ok(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_platform(
        monkeypatch,
        os_name="Darwin",
        arch="arm64",
        release="25.0.0",
        mac_version="16.0",
    )
    _patch_memory(monkeypatch, total=32 * GB, available=20 * GB)
    _patch_disk(monkeypatch)

    result = check.build_check_report()

    assert _checks(result)["memory"].severity == "ok"
    assert _checks(result)["disk"].severity == "ok"
    assert result.report.overall == "ok"
    assert result.recommended_package == "solstone-journal"
    assert check.main([]) == 0
    capsys.readouterr()


def test_macos_arm64_tight_availability_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_platform(monkeypatch, os_name="Darwin", arch="arm64", mac_version="16.0")
    _patch_memory(monkeypatch, total=16 * GB, available=10 * GB)
    _patch_disk(monkeypatch)

    result = check.build_check_report()

    assert _checks(result)["memory"].severity == "warning"
    assert result.report.overall == "warning"
    assert check.main([]) == 1
    capsys.readouterr()


def test_macos_arm64_low_total_blocks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_platform(monkeypatch, os_name="Darwin", arch="arm64", mac_version="16.0")
    _patch_memory(monkeypatch, total=8 * GB, available=8 * GB)
    _patch_disk(monkeypatch)

    result = check.build_check_report()

    assert _checks(result)["memory"].severity == "blocked"
    assert result.report.overall == "blocked"
    assert check.main([]) == 2
    capsys.readouterr()


def test_macos_arm64_memory_unverifiable_warns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_platform(monkeypatch, os_name="Darwin", arch="arm64", mac_version="16.0")
    _patch_memory(monkeypatch, total=None, available=None)
    _patch_disk(monkeypatch)

    result = check.build_check_report()

    assert _checks(result)["memory"].severity == "unknown"
    assert result.report.overall == "warning"
    assert check.main([]) == 1
    capsys.readouterr()


def test_macos_intel_blocks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _patch_platform(monkeypatch, os_name="Darwin", arch="x86_64", mac_version="13.0")

    result = check.build_check_report()

    assert result.platform.supported is False
    assert len(result.report.checks) == 1
    assert result.report.checks[0].severity == "blocked"
    assert "Apple Silicon" in result.report.checks[0].detail
    assert check.main([]) == 2
    assert "Apple Silicon" in capsys.readouterr().out


def test_windows_blocks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_platform(monkeypatch, os_name="Windows", arch="AMD64", release="11")

    result = check.build_check_report()

    assert result.platform.supported is False
    assert result.report.checks[0].severity == "blocked"
    assert check.main([]) == 2
    capsys.readouterr()


def test_low_disk_blocks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(local_cuda, "probe_nvidia_gpu", lambda: _nvidia_probe())
    _patch_disk(monkeypatch, free=10 * GB)

    result = check.build_check_report()

    assert _checks(result)["disk"].severity == "blocked"
    assert result.report.overall == "blocked"
    assert check.main([]) == 2
    capsys.readouterr()


def test_json_schema_lock(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(local_cuda, "probe_nvidia_gpu", lambda: _nvidia_probe())

    assert check.main(["--json"]) == 0
    stdout = capsys.readouterr().out
    payload = json.loads(stdout)

    assert set(payload) == {"platform", "checks", "overall", "feedback_url", "version"}
    assert set(payload["platform"]) == {
        "os",
        "os_version",
        "arch",
        "python",
        "supported",
    }
    assert payload["checks"]
    for item in payload["checks"]:
        assert set(item) == {
            "name",
            "severity",
            "detail",
            "required_bytes",
            "available_bytes",
        }
    assert "recommended_package" not in payload
    assert stdout.strip().startswith("{")
    assert stdout.strip().endswith("}")


def test_contribution_callout_present_across_verdicts(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _patch_linux_ok(monkeypatch)
    monkeypatch.setattr(local_cuda, "probe_nvidia_gpu", lambda: _nvidia_probe())

    assert check.main([]) == 0
    assert check.FEEDBACK_URL in capsys.readouterr().out

    _patch_platform(monkeypatch, os_name="Windows", arch="AMD64", release="11")
    assert check.main([]) == 2
    assert check.FEEDBACK_URL in capsys.readouterr().out

    _patch_platform(monkeypatch, os_name="Darwin", arch="arm64", mac_version="16.0")
    _patch_memory(monkeypatch, total=None, available=None)
    _patch_disk(monkeypatch)
    assert check.main([]) == 1
    assert check.FEEDBACK_URL in capsys.readouterr().out
