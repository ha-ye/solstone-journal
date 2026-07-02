# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from types import SimpleNamespace

import pytest

from solstone.think.providers import local_cuda

ARCH_SET = frozenset({"sm_86", "sm_89", "sm_120a", "sm_121a"})


def _completed(stdout: str, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def _patch_nvidia_smi(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gpu_stdout: str = "0, 8.9, 580.95.05, 24564\n",
    version_stdout: str = "CUDA Version        : 13.0\n",
    gpu_returncode: int = 0,
    version_returncode: int = 0,
    gpu_exc: BaseException | None = None,
    version_exc: BaseException | None = None,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs) -> SimpleNamespace:
        calls.append(cmd)
        if "--query-gpu=index,compute_cap,driver_version,memory.total" in cmd:
            if gpu_exc is not None:
                raise gpu_exc
            return _completed(gpu_stdout, gpu_returncode)
        if "--version" in cmd:
            if version_exc is not None:
                raise version_exc
            return _completed(version_stdout, version_returncode)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(local_cuda.subprocess, "run", fake_run)
    return calls


def test_arch_helpers() -> None:
    assert local_cuda._base_arch("sm_121a") == "sm_121"
    assert local_cuda._base_arch("sm_86") == "sm_86"
    assert local_cuda._compute_cap_to_arch("7.5") == "sm_75"
    assert local_cuda._compute_cap_to_arch("12.1") == "sm_121"
    assert local_cuda._compute_cap_to_arch("bad") is None
    assert local_cuda._compute_cap_to_arch("8") is None


def test_probe_nvidia_gpu_parses_normal_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_nvidia_smi(monkeypatch)

    probe = local_cuda.probe_nvidia_gpu()

    assert probe == local_cuda.NvidiaProbe(
        index=0,
        compute_cap="sm_89",
        driver_cuda_version=13,
        vram_mib=24564,
        detected=True,
    )


def test_probe_nvidia_gpu_parses_this_box_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_nvidia_smi(
        monkeypatch,
        gpu_stdout="0, 7.5, 580.142, 6144\n",
        version_stdout="CUDA Version        : 13.0\n",
    )

    probe = local_cuda.probe_nvidia_gpu()

    assert probe == local_cuda.NvidiaProbe(
        index=0,
        compute_cap="sm_75",
        driver_cuda_version=13,
        vram_mib=6144,
        detected=True,
    )


def test_probe_nvidia_gpu_parses_gb10_unified_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_nvidia_smi(
        monkeypatch,
        gpu_stdout="0, 12.1, 580.95.05, [N/A]\n",
        version_stdout="CUDA Version        : 13.0\n",
    )

    probe = local_cuda.probe_nvidia_gpu()

    assert probe.detected is True
    assert probe.index == 0
    assert probe.compute_cap == "sm_121"
    assert probe.driver_cuda_version == 13
    assert probe.vram_mib is None


def test_probe_nvidia_gpu_missing_binary_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_nvidia_smi(monkeypatch, gpu_exc=FileNotFoundError("missing"))

    probe = local_cuda.probe_nvidia_gpu()

    assert probe == local_cuda.NvidiaProbe(
        index=None,
        compute_cap=None,
        driver_cuda_version=None,
        vram_mib=None,
        detected=False,
    )
    assert len(calls) == 1


def test_probe_nvidia_gpu_garbled_output_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_nvidia_smi(monkeypatch, gpu_stdout="garbled\n")

    probe = local_cuda.probe_nvidia_gpu()

    assert probe == local_cuda.NvidiaProbe(
        index=None,
        compute_cap=None,
        driver_cuda_version=None,
        vram_mib=None,
        detected=False,
    )


def test_select_local_backend_matrix() -> None:
    cases = [
        ("sm_75", 13, "vulkan", "compute_cap sm_75 not in CUDA image arch set"),
        ("sm_75", 12, "vulkan", "compute_cap sm_75 not in CUDA image arch set"),
        ("sm_86", 13, "cuda", "compute_cap sm_86 covered; driver CUDA 13 >= 13"),
        ("sm_86", 12, "vulkan", "driver CUDA 12 < required 13"),
        ("sm_121", 13, "cuda", "compute_cap sm_121 covered; driver CUDA 13 >= 13"),
        ("sm_121", 12, "vulkan", "driver CUDA 12 < required 13"),
        (None, 13, "vulkan", "NVIDIA compute capability unreadable"),
        (None, 12, "vulkan", "NVIDIA compute capability unreadable"),
    ]

    for compute_cap, driver_cuda, backend, reason in cases:
        probe = local_cuda.NvidiaProbe(
            index=0,
            compute_cap=compute_cap,
            driver_cuda_version=driver_cuda,
            vram_mib=24564,
            detected=True,
        )

        choice = local_cuda.select_local_backend(probe, ARCH_SET, 13)

        assert choice == local_cuda.BackendChoice(backend, reason)


def test_select_local_backend_no_gpu_detected() -> None:
    choice = local_cuda.select_local_backend(
        local_cuda.NvidiaProbe(
            index=None,
            compute_cap=None,
            driver_cuda_version=None,
            vram_mib=None,
            detected=False,
        ),
        ARCH_SET,
        13,
    )

    assert choice == local_cuda.BackendChoice("vulkan", "no NVIDIA GPU detected")


def test_select_local_backend_driver_cuda_unreadable() -> None:
    choice = local_cuda.select_local_backend(
        local_cuda.NvidiaProbe(
            index=0,
            compute_cap="sm_86",
            driver_cuda_version=None,
            vram_mib=24564,
            detected=True,
        ),
        ARCH_SET,
        13,
    )

    assert choice == local_cuda.BackendChoice(
        "vulkan",
        "driver CUDA version unreadable",
    )


def test_parse_embedded_arch_set_and_verify_match() -> None:
    text = """
    code for sm_86
    arch = sm_89
    code for sm_120a
    code for sm_121a
    """

    assert local_cuda.parse_embedded_arch_set(text) == ARCH_SET
    local_cuda.verify_cuda_pin_arch_set(text, ARCH_SET)


def test_verify_cuda_pin_arch_set_mismatch_raises() -> None:
    with pytest.raises(local_cuda.LocalCudaError) as exc_info:
        local_cuda.verify_cuda_pin_arch_set("code for sm_86\n", ARCH_SET)

    assert exc_info.value.reason_code == "arch_set_mismatch"
