# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

from solstone.think.providers import fit_report, local_cuda, local_install, local_vulkan
from solstone.think.providers.local import LocalProviderError
from solstone.think.providers.memory import MemoryVerdict

PLACEMENT_LINE = (
    "sol thinks on your GPU; transcription runs on your CPU on this machine"
)


@pytest.fixture(autouse=True)
def _reset_vulkan_detect_cache():
    local_vulkan.reset_detect_cache()
    yield
    local_vulkan.reset_detect_cache()


def _nvidia_probe(
    *,
    vram_mib: int,
    memory_source: str = local_cuda.MEMORY_SOURCE_NVIDIA_VRAM,
) -> local_cuda.NvidiaProbe:
    return local_cuda.NvidiaProbe(
        index=0,
        compute_cap="sm_89",
        driver_cuda_version=13,
        vram_mib=vram_mib,
        tiering_memory_mib=vram_mib,
        memory_source=memory_source,
        detected=True,
    )


def _vulkan_device(
    *,
    index: int = 0,
    vram_mib: int,
) -> local_vulkan.VulkanDevice:
    return local_vulkan.VulkanDevice(
        index=index,
        name=f"Test GPU {index}",
        device_type=local_vulkan.VK_TYPE_DISCRETE,
        vram_mib=vram_mib,
    )


def _choice(backend: str = "cuda") -> local_cuda.BackendChoice:
    return local_cuda.BackendChoice(backend=backend, reason="test choice")


def _undetected_nvidia_probe() -> local_cuda.NvidiaProbe:
    return local_cuda.NvidiaProbe(
        index=None,
        compute_cap=None,
        driver_cuda_version=None,
        vram_mib=None,
        tiering_memory_mib=None,
        memory_source=local_cuda.MEMORY_SOURCE_UNAVAILABLE,
        detected=False,
    )


def test_overall_collapses_unknown_to_warning() -> None:
    report = fit_report.FitReport(
        artifact="artifact",
        checks=(
            fit_report.FitCheck("platform", "ok", "ok"),
            fit_report.FitCheck("probe", "unknown", "unknown"),
        ),
    )

    assert report.overall == "warning"
    assert "[unknown] probe: unknown" in fit_report.render_fit_report(report)


def test_overall_blocked_wins() -> None:
    report = fit_report.FitReport(
        artifact="artifact",
        checks=(
            fit_report.FitCheck("disk", "warning", "warning"),
            fit_report.FitCheck("platform", "blocked", "blocked"),
        ),
    )

    assert report.overall == "blocked"


def test_local_gpu_check_mentions_cpu_transcription_on_small_bundled_brain() -> None:
    check = fit_report._local_gpu_check(
        _nvidia_probe(vram_mib=6144),
        _choice(),
        [_vulkan_device(vram_mib=6144)],
        local_vulkan,
        brain_lane_active=True,
    )

    assert check.severity == "ok"
    assert check.detail == f"CUDA backend selected: test choice; {PLACEMENT_LINE}"


@pytest.mark.parametrize(
    ("probe", "devices", "brain_lane_active"),
    [
        (_nvidia_probe(vram_mib=6144), [_vulkan_device(vram_mib=6144)], False),
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
    ],
)
def test_local_gpu_check_omits_cpu_transcription_line_outside_predicate(
    probe: local_cuda.NvidiaProbe,
    devices: list[local_vulkan.VulkanDevice],
    brain_lane_active: bool,
) -> None:
    check = fit_report._local_gpu_check(
        probe,
        _choice(),
        devices,
        local_vulkan,
        brain_lane_active=brain_lane_active,
    )

    assert check.severity == "ok"
    assert PLACEMENT_LINE not in check.detail


def test_local_gpu_check_uses_vulkan_when_nvidia_probe_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    devices = [
        _vulkan_device(index=0, vram_mib=8176),
        _vulkan_device(index=1, vram_mib=2048),
    ]
    monkeypatch.setattr(local_vulkan, "gpu_probe_ok", lambda: True)

    check = fit_report._local_gpu_check(
        _undetected_nvidia_probe(),
        _choice("vulkan"),
        devices,
        local_vulkan,
        brain_lane_active=False,
        override_index=1,
    )

    assert check.severity == "ok"
    assert check.detail == (
        "Vulkan GPU selected: Test GPU 1; resolved backend is vulkan: test choice"
    )


def test_disk_unknown_size_warns_when_known_size_fits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fit_report, "free_bytes", lambda _path: 10)

    check = fit_report._disk_check(
        "disk",
        tmp_path,
        (("known", 5),),
        ("server tarball",),
    )

    assert check.severity == "warning"
    assert check.required_bytes == 5
    assert check.available_bytes == 10
    assert "unknown download size for server tarball" in check.detail


def test_disk_read_error_reports_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_free(_path: Path) -> int:
        raise OSError("disk unavailable")

    monkeypatch.setattr(fit_report, "free_bytes", fail_free)

    check = fit_report._disk_check("disk", tmp_path, (("known", 5),), ())

    assert check.severity == "unknown"
    assert check.required_bytes == 5
    assert check.available_bytes is None
    assert "could not be verified" in check.detail


def test_ram_unavailable_reports_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fit_report,
        "assess_memory",
        lambda required, *, block_below_floor: MemoryVerdict(
            available_bytes=None,
            required_bytes=required,
            severity="warning",
        ),
    )

    check = fit_report._ram_check(
        "ram",
        1024,
        block_below_floor=True,
        artifact_label="model",
    )

    assert check.severity == "warning"
    assert check.available_bytes is None
    assert "available memory could not be verified" in check.detail


def test_local_platform_unsupported_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_install, "llama_server_artifact_key", lambda: "bad")

    def fail_pin() -> None:
        raise LocalProviderError("unsupported_platform", "unsupported test platform")

    monkeypatch.setattr(local_install, "pin_for_current_platform", fail_pin)

    check = fit_report._local_platform_check()

    assert check.severity == "blocked"
    assert check.detail == "unsupported test platform"
