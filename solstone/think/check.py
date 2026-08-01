# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Readiness verdict for bundled local journal models."""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Sequence

from solstone.think.providers.fit_report import FitCheck, FitReport, FitSeverity

GPU_MIN_BYTES = 6 * 1024**3
DISK_MIN_BYTES = 20 * 1024**3
MACOS_MEMORY_FLOOR_BYTES = 16 * 1024**3
LINUX_RAM_WARN_BYTES = 8 * 1024**3
FEEDBACK_URL = "https://github.com/solpbc/solstone-journal"
_RENDER_NODE_INACCESSIBLE_HINT = (
    "a GPU render node exists under /dev/dri but this user cannot open it — "
    "add yourself to the render group with `sudo usermod -aG render $USER`, "
    "then log out and back in and run `sol check` again"
)


@dataclass(frozen=True)
class PlatformInfo:
    os: str
    os_version: str
    arch: str
    python: str
    supported: bool


@dataclass(frozen=True)
class CheckReport:
    platform: PlatformInfo
    report: FitReport
    recommended_package: str | None


def build_check_report() -> CheckReport:
    from solstone.think.providers import local_cuda

    os_name = platform.system()
    arch = platform.machine()
    python_version = platform.python_version()
    mac_version = platform.mac_ver()[0]
    os_version = (
        mac_version if os_name == "Darwin" and mac_version else platform.release()
    )
    supported = (os_name == "Darwin" and arch == "arm64") or (
        os_name == "Linux" and arch in {"x86_64", "aarch64"}
    )
    platform_info = PlatformInfo(
        os=os_name,
        os_version=os_version,
        arch=arch,
        python=python_version,
        supported=supported,
    )

    if not supported:
        check = _unsupported_platform_check(os_name, arch)
        return CheckReport(
            platform=platform_info,
            report=FitReport("host readiness", (check,)),
            recommended_package=None,
        )

    detail = "Apple Silicon macOS (arm64)" if os_name == "Darwin" else f"Linux ({arch})"
    platform_check = FitCheck("platform", "ok", detail)
    if os_name == "Darwin":
        checks = (platform_check, _macos_memory_check(), _disk_check())
        recommended_package = "solstone-journal"
    else:
        probe = local_cuda.probe_nvidia_gpu()
        checks = (
            platform_check,
            _linux_gpu_check(probe),
            _linux_ram_check(),
            _disk_check(),
        )
        recommended_package = (
            "solstone-journal-cuda"
            if arch == "x86_64" and probe.detected
            else "solstone-journal"
        )

    return CheckReport(
        platform=platform_info,
        report=FitReport("host readiness", tuple(checks)),
        recommended_package=recommended_package,
    )


def _unsupported_platform_check(os_name: str, arch: str) -> FitCheck:
    if os_name == "Windows":
        detail = (
            "Windows isn't supported for the bundled local models yet — run the "
            "journal on Linux or an Apple Silicon Mac."
        )
    elif os_name == "Darwin":
        detail = (
            "Intel Macs aren't supported — use an Apple Silicon Mac, or run the "
            "journal on supported Linux."
        )
    else:
        detail = (
            f"{os_name}/{arch} can't run the bundled local models yet — only Apple "
            "Silicon macOS and x86_64/aarch64 Linux are supported."
        )
    return FitCheck("platform", "blocked", detail)


def _macos_memory_check() -> FitCheck:
    from solstone.think.providers import memory

    total = memory.read_total_bytes()
    available = memory.read_available_bytes()
    if total is None:
        return FitCheck(
            "memory",
            "unknown",
            "available memory could not be verified",
            required_bytes=MACOS_MEMORY_FLOOR_BYTES,
            available_bytes=None,
        )
    if total < MACOS_MEMORY_FLOOR_BYTES:
        return FitCheck(
            "memory",
            "blocked",
            (
                "Apple Silicon needs at least 16 GB of memory for the bundled "
                f"local models (this Mac has {memory.gb_label(total)} GB)"
            ),
            required_bytes=MACOS_MEMORY_FLOOR_BYTES,
            available_bytes=total,
        )
    if available is not None and available < memory.MLX_AVAILABLE_FLOOR_BYTES:
        return FitCheck(
            "memory",
            "warning",
            (
                "enough total memory, but only "
                f"{memory.gb_label(available)} GB is free right now — close some "
                "apps before the local models load"
            ),
            required_bytes=memory.MLX_AVAILABLE_FLOOR_BYTES,
            available_bytes=available,
        )
    return FitCheck(
        "memory",
        "ok",
        f"{memory.gb_label(total)} GB of memory meets the 16 GB minimum",
        required_bytes=MACOS_MEMORY_FLOOR_BYTES,
        available_bytes=total,
    )


def _render_nodes_present_but_inaccessible() -> bool:
    """True iff >=1 /dev/dri render node exists and none are R/W-openable here."""
    nodes = glob.glob("/dev/dri/renderD*")
    if not nodes:
        return False
    return not any(os.access(node, os.R_OK | os.W_OK) for node in nodes)


def _linux_gpu_check(probe: object) -> FitCheck:
    try:
        from solstone.think.providers import local_cuda, local_vulkan, memory
        from solstone.think.providers.parakeet_placement import cpu_placement_suffix

        try:
            devices = local_vulkan.detect_gpus()
            probe_ok = local_vulkan.gpu_probe_ok()
        except Exception:
            if not bool(getattr(probe, "detected")):
                raise
            # Fail toward current behavior: Vulkan failure on NVIDIA yields no placement line.
            devices = []
            probe_ok = False
        selected = local_vulkan.select_device(devices) if probe_ok else None
        if not bool(getattr(probe, "detected")):
            inaccessible = (
                not probe_ok or selected is None
            ) and _render_nodes_present_but_inaccessible()
            if inaccessible:
                return FitCheck("gpu", "unknown", _RENDER_NODE_INACCESSIBLE_HINT)
            if not probe_ok:
                return FitCheck(
                    "gpu",
                    "unknown",
                    (
                        "no NVIDIA GPU found and the Vulkan probe did not complete — "
                        "GPU readiness is unknown"
                    ),
                )
            if selected is None:
                return FitCheck(
                    "gpu",
                    "blocked",
                    (
                        "no usable GPU found — the bundled local models need a "
                        "hardware GPU with at least 6 GB"
                    ),
                    required_bytes=GPU_MIN_BYTES,
                    available_bytes=None,
                )
            vram_bytes = selected.vram_mib * 1024 * 1024
            if vram_bytes < GPU_MIN_BYTES:
                return FitCheck(
                    "gpu",
                    "blocked",
                    (
                        f"GPU {selected.name} has {memory.gb_label(vram_bytes)} GB — "
                        "the bundled local models need at least 6 GB"
                    ),
                    required_bytes=GPU_MIN_BYTES,
                    available_bytes=vram_bytes,
                )
            return FitCheck(
                "gpu",
                "ok",
                (
                    f"Vulkan GPU {selected.name} with {memory.gb_label(vram_bytes)} GB"
                    + cpu_placement_suffix(
                        devices=devices,
                        selected=selected,
                        local_vulkan=local_vulkan,
                        unified_memory=False,
                        # sol check runs before install and cannot rely on journal
                        # config; use the bundled-brain default for this advisory.
                        brain_lane_active=True,
                    )
                ),
                required_bytes=GPU_MIN_BYTES,
                available_bytes=vram_bytes,
            )

        vram_mib = getattr(probe, "vram_mib")
        tiering_memory_mib = getattr(probe, "tiering_memory_mib")
        effective_mib = vram_mib if vram_mib is not None else tiering_memory_mib
        if effective_mib is None:
            return FitCheck(
                "gpu",
                "unknown",
                (
                    "NVIDIA GPU detected but its memory could not be read — GPU "
                    "readiness is unknown"
                ),
                required_bytes=GPU_MIN_BYTES,
                available_bytes=None,
            )
        gpu_bytes = effective_mib * 1024 * 1024
        if gpu_bytes < GPU_MIN_BYTES:
            return FitCheck(
                "gpu",
                "blocked",
                (
                    f"the NVIDIA GPU has {memory.gb_label(gpu_bytes)} GB — the "
                    "bundled local models need at least 6 GB"
                ),
                required_bytes=GPU_MIN_BYTES,
                available_bytes=gpu_bytes,
            )
        detail = f"NVIDIA GPU with {memory.gb_label(gpu_bytes)} GB"
        if getattr(probe, "memory_source") == local_cuda.MEMORY_SOURCE_SYSTEM_AVAILABLE:
            detail = f"{detail} (unified memory)"
        detail += cpu_placement_suffix(
            devices=devices,
            selected=selected,
            local_vulkan=local_vulkan,
            unified_memory=(
                getattr(probe, "memory_source")
                == local_cuda.MEMORY_SOURCE_SYSTEM_AVAILABLE
            ),
            # sol check runs before install and cannot rely on journal config; use
            # the bundled-brain default for this advisory.
            brain_lane_active=True,
        )
        return FitCheck(
            "gpu",
            "ok",
            detail,
            required_bytes=GPU_MIN_BYTES,
            available_bytes=gpu_bytes,
        )
    except Exception as exc:
        return FitCheck(
            "gpu",
            "unknown",
            f"GPU readiness could not be determined: {exc}",
        )


def _linux_ram_check() -> FitCheck:
    from solstone.think.providers import memory

    total = memory.read_total_bytes()
    if total is None:
        return FitCheck("ram", "warning", "system memory could not be verified")
    if total < LINUX_RAM_WARN_BYTES:
        return FitCheck(
            "ram",
            "warning",
            (
                f"{memory.gb_label(total)} GB of system memory is on the low side — "
                "8 GB or more is recommended"
            ),
            required_bytes=LINUX_RAM_WARN_BYTES,
            available_bytes=total,
        )
    return FitCheck(
        "ram",
        "ok",
        f"{memory.gb_label(total)} GB of system memory",
        available_bytes=total,
    )


def _disk_check() -> FitCheck:
    from pathlib import Path

    from solstone.think.providers import memory
    from solstone.think.utils import get_journal_info

    path, _source = get_journal_info()
    try:
        free = memory.free_bytes(Path(path))
    except OSError as exc:
        return FitCheck(
            "disk",
            "unknown",
            f"free space at {path} could not be verified: {exc}",
            required_bytes=DISK_MIN_BYTES,
            available_bytes=None,
        )
    if free < DISK_MIN_BYTES:
        return FitCheck(
            "disk",
            "blocked",
            (
                "the journal and local models need at least 20 GB free — "
                f"{path} has {memory.gb_label(free)} GB"
            ),
            required_bytes=DISK_MIN_BYTES,
            available_bytes=free,
        )
    return FitCheck(
        "disk",
        "ok",
        f"{memory.gb_label(free)} GB free at {path} (need 20 GB)",
        required_bytes=DISK_MIN_BYTES,
        available_bytes=free,
    )


def _exit_code(overall: FitSeverity) -> int:
    return {"ok": 0, "warning": 1, "blocked": 2}[overall]


def _package_version() -> str | None:
    try:
        return _pkg_version("solstone")
    except PackageNotFoundError:
        return None


def _json_payload(result: CheckReport) -> dict:
    return {
        "platform": {
            "os": result.platform.os,
            "os_version": result.platform.os_version,
            "arch": result.platform.arch,
            "python": result.platform.python,
            "supported": result.platform.supported,
        },
        "checks": [
            {
                "name": check.name,
                "severity": check.severity,
                "detail": check.detail,
                "required_bytes": check.required_bytes,
                "available_bytes": check.available_bytes,
            }
            for check in result.report.checks
        ],
        "overall": result.report.overall,
        "feedback_url": FEEDBACK_URL,
        "version": _package_version(),
    }


def _render_human(result: CheckReport) -> str:
    markers = {
        "ok": "[ok]",
        "warning": "[warn]",
        "blocked": "[blocked]",
        "unknown": "[unknown]",
    }
    lines = [
        "sol check — can this computer run the journal with the bundled local models?"
    ]
    lines.extend(
        f"  {markers[check.severity]:<9} {check.name:<9} {check.detail}"
        for check in result.report.checks
    )

    overall = result.report.overall
    if overall == "ok":
        lines.append(
            "Ready — install the journal next:  "
            f"uv tool install {result.recommended_package}"
        )
    elif overall == "warning":
        if result.recommended_package is None:
            lines.append("Mostly ready — see the warnings above.")
        else:
            lines.append(
                "Mostly ready (see the warnings above) — you can install the "
                f"journal:  uv tool install {result.recommended_package}"
            )
    else:
        lines.append(
            "Not ready — this computer can't run the bundled local models yet."
        )

    lines.extend(
        (
            "",
            "Think this readout is wrong for your machine? We'd love a patch — "
            f"{FEEDBACK_URL}",
        )
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the readiness verdict as JSON for agents",
    )
    args = parser.parse_args(argv)

    result = build_check_report()
    if args.json:
        print(json.dumps(_json_payload(result), indent=2))
    else:
        print(_render_human(result))
    return _exit_code(result.report.overall)


if __name__ == "__main__":
    raise SystemExit(main())
