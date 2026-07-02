# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CUDA GPU discovery and backend selection for the bundled local provider."""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from solstone.think.providers.local_install import CudaServerPin

LOG = logging.getLogger(__name__)

_PROBE_TIMEOUT_S = 10
_ARCH_RE = re.compile(r"\bsm_\d+a?\b")
_CUDA_VERSION_RE = re.compile(r"CUDA Version\s*:?\s*(\d+)(?:\.\d+)?")


class LocalCudaError(RuntimeError):
    """CUDA local-provider failure with a recovery reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class NvidiaProbe:
    index: int | None
    compute_cap: str | None
    driver_cuda_version: int | None
    vram_mib: int | None
    detected: bool


@dataclass(frozen=True)
class BackendChoice:
    backend: str
    reason: str


def _base_arch(arch: str) -> str:
    return arch[:-1] if arch.endswith("a") else arch


def _compute_cap_to_arch(cap: str) -> str | None:
    parts = cap.strip().split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    return f"sm_{parts[0]}{parts[1]}"


def _undetected() -> NvidiaProbe:
    return NvidiaProbe(
        index=None,
        compute_cap=None,
        driver_cuda_version=None,
        vram_mib=None,
        detected=False,
    )


def _parse_nvidia_smi_row(line: str) -> tuple[int, str | None, int | None] | None:
    fields = [field.strip() for field in line.split(",")]
    if len(fields) != 4:
        return None
    try:
        index = int(fields[0])
    except ValueError:
        return None

    compute_cap = _compute_cap_to_arch(fields[1])
    try:
        vram_mib = int(fields[3])
    except ValueError:
        vram_mib = None
    return index, compute_cap, vram_mib


def _probe_driver_cuda_version() -> int | None:
    # TODO(AC10): driver_version→CUDA-version threshold fallback (candidate floor
    # 580.65.06) is unverified; None here fails safe to Vulkan.
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--version"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        LOG.warning(
            "NVIDIA driver CUDA version probe timed out after %.0fs", _PROBE_TIMEOUT_S
        )
        return None
    except OSError as exc:
        LOG.warning("NVIDIA driver CUDA version probe could not start: %s", exc)
        return None

    if completed.returncode != 0:
        LOG.warning(
            "NVIDIA driver CUDA version probe exited with status %s",
            completed.returncode,
        )
        return None

    match = _CUDA_VERSION_RE.search(completed.stdout)
    if match is None:
        LOG.warning("NVIDIA driver CUDA version probe returned unparseable output")
        return None
    return int(match.group(1))


def probe_nvidia_gpu() -> NvidiaProbe:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,compute_cap,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        LOG.warning("NVIDIA GPU probe timed out after %.0fs", _PROBE_TIMEOUT_S)
        return _undetected()
    except OSError as exc:
        LOG.warning("NVIDIA GPU probe could not start: %s", exc)
        return _undetected()

    if completed.returncode != 0:
        LOG.warning("NVIDIA GPU probe exited with status %s", completed.returncode)
        return _undetected()

    first_row = next(
        (line.strip() for line in completed.stdout.splitlines() if line.strip()),
        None,
    )
    if first_row is None:
        LOG.warning("NVIDIA GPU probe returned no rows")
        return _undetected()

    parsed = _parse_nvidia_smi_row(first_row)
    if parsed is None:
        LOG.warning("NVIDIA GPU probe returned invalid CSV")
        return _undetected()

    index, compute_cap, vram_mib = parsed
    return NvidiaProbe(
        index=index,
        compute_cap=compute_cap,
        driver_cuda_version=_probe_driver_cuda_version(),
        vram_mib=vram_mib,
        detected=True,
    )


def select_local_backend(
    probe: NvidiaProbe,
    arch_set: frozenset[str],
    cuda_version: int,
) -> BackendChoice:
    if not probe.detected:
        return BackendChoice("vulkan", "no NVIDIA GPU detected")
    if probe.compute_cap is None:
        return BackendChoice("vulkan", "NVIDIA compute capability unreadable")

    base_arches = {_base_arch(arch) for arch in arch_set}
    if _base_arch(probe.compute_cap) not in base_arches:
        return BackendChoice(
            "vulkan",
            f"compute_cap {probe.compute_cap} not in CUDA image arch set",
        )

    if probe.driver_cuda_version is None:
        return BackendChoice("vulkan", "driver CUDA version unreadable")
    if probe.driver_cuda_version < cuda_version:
        return BackendChoice(
            "vulkan",
            f"driver CUDA {probe.driver_cuda_version} < required {cuda_version}",
        )

    return BackendChoice(
        "cuda",
        (
            f"compute_cap {probe.compute_cap} covered; "
            f"driver CUDA {probe.driver_cuda_version} >= {cuda_version}"
        ),
    )


def resolve_local_backend(pin: CudaServerPin) -> BackendChoice:
    return select_local_backend(
        probe_nvidia_gpu(),
        pin.embedded_arch_set,
        pin.cuda_version,
    )


def parse_embedded_arch_set(cuobjdump_list_elf_text: str) -> frozenset[str]:
    # TODO(AC10): live cuobjdump --list-elf on the extracted libggml-cuda.so
    # runs on hardware; in-lode uses a synthetic fixture.
    return frozenset(_ARCH_RE.findall(cuobjdump_list_elf_text))


def verify_cuda_pin_arch_set(text: str, declared: frozenset[str]) -> None:
    actual = parse_embedded_arch_set(text)
    if actual != declared:
        raise LocalCudaError(
            "arch_set_mismatch",
            f"CUDA embedded arch set mismatch: declared={sorted(declared)}, actual={sorted(actual)}",
        )


__all__ = [
    "BackendChoice",
    "LocalCudaError",
    "NvidiaProbe",
    "parse_embedded_arch_set",
    "probe_nvidia_gpu",
    "resolve_local_backend",
    "select_local_backend",
    "verify_cuda_pin_arch_set",
]
