# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from solstone.think.providers import local_cuda, local_install

ARCH_SET = frozenset({"sm_86", "sm_89", "sm_120a", "sm_121a"})
pytestmark = pytest.mark.real_local_backend_probe


def _completed(stdout: str, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def _patch_nvidia_smi(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gpu_stdout: str = "0, NVIDIA GeForce RTX 4090, 8.9, 580.95.05, 24564\n",
    name_stdout: str = "NVIDIA GeForce RTX 4090\n",
    version_stdout: str = "CUDA Version        : 13.0\n",
    gpu_returncode: int = 0,
    name_returncode: int = 0,
    version_returncode: int = 0,
    gpu_exc: BaseException | None = None,
    name_exc: BaseException | None = None,
    version_exc: BaseException | None = None,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs) -> SimpleNamespace:
        calls.append(cmd)
        if "--query-gpu=index,name,compute_cap,driver_version,memory.total" in cmd:
            if gpu_exc is not None:
                raise gpu_exc
            return _completed(gpu_stdout, gpu_returncode)
        if "--query-gpu=name" in cmd:
            if name_exc is not None:
                raise name_exc
            return _completed(name_stdout, name_returncode)
        if "--version" in cmd:
            if version_exc is not None:
                raise version_exc
            return _completed(version_stdout, version_returncode)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(local_cuda.shutil, "which", lambda _name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(local_cuda.subprocess, "run", fake_run)
    return calls


def _patch_memavailable(monkeypatch: pytest.MonkeyPatch, value_mib: int | None) -> None:
    monkeypatch.setattr(
        local_cuda,
        "_read_linux_memavailable_mib",
        lambda: value_mib,
    )


def test_arch_helpers() -> None:
    assert local_cuda._base_arch("sm_121a") == "sm_121"
    assert local_cuda._base_arch("sm_86") == "sm_86"
    assert local_cuda._compute_cap_to_arch("7.5") == "sm_75"
    assert local_cuda._compute_cap_to_arch("12.1") == "sm_121"
    assert local_cuda._compute_cap_to_arch("bad") is None
    assert local_cuda._compute_cap_to_arch("8") is None
    assert local_cuda.has_unified_memory_name("NVIDIA GB10") is True
    assert local_cuda.has_unified_memory_name("NVIDIA GeForce RTX 4090") is False


def test_detect_nvidia_unified_memory_absent_binary_spawns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_nvidia_smi(monkeypatch)
    monkeypatch.setattr(local_cuda.shutil, "which", lambda _name: None)

    assert local_cuda.detect_nvidia_unified_memory() is False
    assert calls == []


def test_detect_nvidia_unified_memory_single_name_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_nvidia_smi(monkeypatch, name_stdout="NVIDIA GB10\n")
    monkeypatch.setattr(local_cuda.shutil, "which", lambda _name: "/usr/bin/nvidia-smi")

    assert local_cuda.detect_nvidia_unified_memory() is True
    assert calls == [["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]]


def test_detect_nvidia_unified_memory_non_gb10_name_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_nvidia_smi(monkeypatch, name_stdout="NVIDIA GeForce RTX 4090\n")
    monkeypatch.setattr(local_cuda.shutil, "which", lambda _name: "/usr/bin/nvidia-smi")

    assert local_cuda.detect_nvidia_unified_memory() is False
    assert calls == [["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]]


def test_probe_nvidia_gpu_parses_normal_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_nvidia_smi(monkeypatch)

    probe = local_cuda.probe_nvidia_gpu()

    assert probe == local_cuda.NvidiaProbe(
        index=0,
        compute_cap="sm_89",
        driver_cuda_version=13,
        vram_mib=24564,
        tiering_memory_mib=24564,
        memory_source=local_cuda.MEMORY_SOURCE_NVIDIA_VRAM,
        detected=True,
    )


def test_probe_nvidia_gpu_parses_this_box_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_nvidia_smi(
        monkeypatch,
        gpu_stdout="0, NVIDIA GeForce RTX 2060, 7.5, 580.142, 6144\n",
        version_stdout="CUDA Version        : 13.0\n",
    )

    probe = local_cuda.probe_nvidia_gpu()

    assert probe == local_cuda.NvidiaProbe(
        index=0,
        compute_cap="sm_75",
        driver_cuda_version=13,
        vram_mib=6144,
        tiering_memory_mib=6144,
        memory_source=local_cuda.MEMORY_SOURCE_NVIDIA_VRAM,
        detected=True,
    )


def test_probe_nvidia_gpu_parses_gb10_unified_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_nvidia_smi(
        monkeypatch,
        gpu_stdout="0, NVIDIA GB10, 12.1, 580.142, [N/A]\n",
        version_stdout="CUDA Version        : 13.0\n",
    )
    _patch_memavailable(monkeypatch, 26724)

    probe = local_cuda.probe_nvidia_gpu()

    assert probe.detected is True
    assert probe.index == 0
    assert probe.compute_cap == "sm_121"
    assert probe.driver_cuda_version == 13
    assert probe.vram_mib is None
    assert probe.tiering_memory_mib == 26724
    assert probe.memory_source == local_cuda.MEMORY_SOURCE_SYSTEM_AVAILABLE


def test_probe_nvidia_gpu_gb10_unified_memory_unavailable_stays_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_nvidia_smi(
        monkeypatch,
        gpu_stdout="0, NVIDIA GB10, 12.1, 580.142, [N/A]\n",
        version_stdout="CUDA Version        : 13.0\n",
    )
    _patch_memavailable(monkeypatch, None)

    probe = local_cuda.probe_nvidia_gpu()

    assert probe.detected is True
    assert probe.index == 0
    assert probe.compute_cap == "sm_121"
    assert probe.driver_cuda_version == 13
    assert probe.vram_mib is None
    assert probe.tiering_memory_mib is None
    assert probe.memory_source == local_cuda.MEMORY_SOURCE_UNAVAILABLE


def test_probe_nvidia_gpu_unknown_missing_vram_stays_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_nvidia_smi(
        monkeypatch,
        gpu_stdout="0, NVIDIA Mystery, 12.1, 580.142, [N/A]\n",
        version_stdout="CUDA Version        : 13.0\n",
    )

    def fail_memavailable() -> int | None:
        raise AssertionError("non-GB10 missing VRAM must not use system memory")

    monkeypatch.setattr(local_cuda, "_read_linux_memavailable_mib", fail_memavailable)

    probe = local_cuda.probe_nvidia_gpu()

    assert probe.detected is True
    assert probe.index == 0
    assert probe.compute_cap == "sm_121"
    assert probe.driver_cuda_version == 13
    assert probe.vram_mib is None
    assert probe.tiering_memory_mib is None
    assert probe.memory_source == local_cuda.MEMORY_SOURCE_UNAVAILABLE


def test_parse_memavailable_mib() -> None:
    assert (
        local_cuda._parse_memavailable_mib(
            "MemTotal:       127601388 kB\nMemAvailable:   27365908 kB\n"
        )
        == 26724
    )
    assert local_cuda._parse_memavailable_mib("MemAvailable: nope kB\n") is None
    assert local_cuda._parse_memavailable_mib("MemAvailable: 1234 MB\n") is None
    assert local_cuda._parse_memavailable_mib("MemTotal: 1234 kB\n") is None


def test_probe_nvidia_gpu_absent_binary_spawns_nothing_and_logs_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(local_cuda.shutil, "which", lambda _name: None)

    def fail_run(*_args, **_kwargs) -> SimpleNamespace:
        raise AssertionError("nvidia-smi should not be spawned when absent")

    monkeypatch.setattr(local_cuda.subprocess, "run", fail_run)
    caplog.set_level(logging.DEBUG, logger=local_cuda.LOG.name)

    probe = local_cuda.probe_nvidia_gpu()

    assert probe == local_cuda.NvidiaProbe(
        index=None,
        compute_cap=None,
        driver_cuda_version=None,
        vram_mib=None,
        tiering_memory_mib=None,
        memory_source=local_cuda.MEMORY_SOURCE_UNAVAILABLE,
        detected=False,
    )
    # Forbidding only WARNING would permit an INFO demotion, leaving each line in
    # INFO-inclusive service logs; absent binaries should say nothing at any level.
    assert caplog.records == []


def test_probe_nvidia_gpu_permission_error_keeps_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _patch_nvidia_smi(monkeypatch, gpu_exc=PermissionError("denied"))
    caplog.set_level(logging.DEBUG, logger=local_cuda.LOG.name)

    probe = local_cuda.probe_nvidia_gpu()

    assert probe.detected is False
    assert len(calls) == 1
    assert "NVIDIA GPU probe could not start: denied" in caplog.text


def test_probe_nvidia_gpu_timeout_keeps_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _patch_nvidia_smi(
        monkeypatch,
        gpu_exc=local_cuda.subprocess.TimeoutExpired(
            cmd="nvidia-smi",
            timeout=local_cuda._PROBE_TIMEOUT_S,
        ),
    )
    caplog.set_level(logging.DEBUG, logger=local_cuda.LOG.name)

    probe = local_cuda.probe_nvidia_gpu()

    assert probe.detected is False
    assert len(calls) == 1
    assert "NVIDIA GPU probe timed out after" in caplog.text


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
        tiering_memory_mib=None,
        memory_source=local_cuda.MEMORY_SOURCE_UNAVAILABLE,
        detected=False,
    )


def test_select_local_backend_matrix() -> None:
    cases = [
        (
            False,
            None,
            None,
            local_cuda.ArtifactTrust.TRUSTED,
            True,
            "vulkan",
            "no NVIDIA GPU detected",
        ),
        (
            True,
            None,
            13,
            local_cuda.ArtifactTrust.ABSENT,
            False,
            "vulkan",
            "NVIDIA compute capability unreadable",
        ),
        (
            True,
            "sm_75",
            13,
            local_cuda.ArtifactTrust.UNAVAILABLE,
            True,
            "vulkan",
            "compute_cap sm_75 not in CUDA image arch set",
        ),
        (
            True,
            "sm_75",
            12,
            local_cuda.ArtifactTrust.ABSENT,
            True,
            "vulkan",
            "compute_cap sm_75 not in CUDA image arch set",
        ),
        (
            True,
            "sm_86",
            13,
            local_cuda.ArtifactTrust.TRUSTED,
            False,
            "cuda",
            "compute_cap sm_86 covered; driver CUDA 13 >= 13",
        ),
        (
            True,
            "sm_86",
            12,
            local_cuda.ArtifactTrust.ABSENT,
            True,
            "vulkan",
            "driver CUDA 12 < required 13",
        ),
        (
            True,
            "sm_121",
            13,
            local_cuda.ArtifactTrust.TRUSTED,
            False,
            "cuda",
            "compute_cap sm_121 covered; driver CUDA 13 >= 13",
        ),
        (
            True,
            "sm_121",
            12,
            local_cuda.ArtifactTrust.UNAVAILABLE,
            True,
            "vulkan",
            "driver CUDA 12 < required 13",
        ),
        (
            True,
            None,
            13,
            local_cuda.ArtifactTrust.ABSENT,
            False,
            "vulkan",
            "NVIDIA compute capability unreadable",
        ),
        (
            True,
            None,
            12,
            local_cuda.ArtifactTrust.UNAVAILABLE,
            True,
            "vulkan",
            "NVIDIA compute capability unreadable",
        ),
        # These rows pin select_local_backend's raw trust contract; resolver
        # tests cover the production pin-present and pin-absent states.
        (
            True,
            "sm_86",
            13,
            local_cuda.ArtifactTrust.ABSENT,
            False,
            "vulkan",
            (
                "compute_cap sm_86 covered; driver CUDA 13 >= 13; "
                "no trusted CUDA runtime artifact present"
            ),
        ),
        (
            True,
            "sm_121",
            13,
            local_cuda.ArtifactTrust.UNAVAILABLE,
            True,
            "cuda",
            "compute_cap sm_121 covered; driver CUDA 13 >= 13",
        ),
        (
            True,
            "sm_121",
            13,
            local_cuda.ArtifactTrust.UNAVAILABLE,
            False,
            "vulkan",
            (
                "compute_cap sm_121 covered; driver CUDA 13 >= 13; "
                "no trusted CUDA runtime artifact present"
            ),
        ),
    ]

    for detected, compute_cap, driver_cuda, trust, persisted, backend, reason in cases:
        probe = local_cuda.NvidiaProbe(
            index=0 if detected else None,
            compute_cap=compute_cap,
            driver_cuda_version=driver_cuda,
            vram_mib=24564,
            tiering_memory_mib=24564,
            memory_source=local_cuda.MEMORY_SOURCE_NVIDIA_VRAM,
            detected=detected,
        )

        choice = local_cuda.select_local_backend(
            probe,
            ARCH_SET,
            13,
            trust,
            persisted_installed_cuda=persisted,
        )

        assert choice == local_cuda.BackendChoice(backend, reason)


def test_resolve_local_backend_uses_pinned_cuda_artifact_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        local_cuda,
        "probe_nvidia_gpu",
        lambda: local_cuda.NvidiaProbe(
            index=0,
            compute_cap="sm_86",
            driver_cuda_version=13,
            vram_mib=24564,
            tiering_memory_mib=24564,
            memory_source=local_cuda.MEMORY_SOURCE_NVIDIA_VRAM,
            detected=True,
        ),
    )
    monkeypatch.setattr(local_install.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(local_install.sys, "platform", "linux")

    assert local_cuda.resolve_local_backend(local_install.CUDA_SERVER_PIN) == (
        local_cuda.BackendChoice(
            "cuda",
            "compute_cap sm_86 covered; driver CUDA 13 >= 13",
        )
    )


def test_resolve_local_backend_uses_byte_identical_vulkan_reason_without_platform_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        local_cuda,
        "probe_nvidia_gpu",
        lambda: local_cuda.NvidiaProbe(
            index=0,
            compute_cap="sm_86",
            driver_cuda_version=13,
            vram_mib=24564,
            tiering_memory_mib=24564,
            memory_source=local_cuda.MEMORY_SOURCE_NVIDIA_VRAM,
            detected=True,
        ),
    )
    monkeypatch.setattr(local_install.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(local_install.sys, "platform", "linux")
    pin = replace(local_install.CUDA_SERVER_PIN, artifacts_by_key={})

    assert local_cuda.resolve_local_backend(pin) == local_cuda.BackendChoice(
        "vulkan",
        (
            "compute_cap sm_86 covered; driver CUDA 13 >= 13; "
            "no trusted CUDA runtime artifact present"
        ),
    )


def test_select_local_backend_no_gpu_detected() -> None:
    choice = local_cuda.select_local_backend(
        local_cuda.NvidiaProbe(
            index=None,
            compute_cap=None,
            driver_cuda_version=None,
            vram_mib=None,
            tiering_memory_mib=None,
            memory_source=local_cuda.MEMORY_SOURCE_UNAVAILABLE,
            detected=False,
        ),
        ARCH_SET,
        13,
        local_cuda.ArtifactTrust.TRUSTED,
        persisted_installed_cuda=True,
    )

    assert choice == local_cuda.BackendChoice("vulkan", "no NVIDIA GPU detected")


def test_select_local_backend_driver_cuda_unreadable() -> None:
    choice = local_cuda.select_local_backend(
        local_cuda.NvidiaProbe(
            index=0,
            compute_cap="sm_86",
            driver_cuda_version=None,
            vram_mib=24564,
            tiering_memory_mib=24564,
            memory_source=local_cuda.MEMORY_SOURCE_NVIDIA_VRAM,
            detected=True,
        ),
        ARCH_SET,
        13,
        local_cuda.ArtifactTrust.TRUSTED,
        persisted_installed_cuda=False,
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
