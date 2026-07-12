# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pure placement decision for supervised parakeet.cpp co-location."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from solstone.think.providers.local_server import select_server_tier

# Measured worst-case ordinary segment residency: 300 s is the observer-contract
# cap. The 1024 MiB margin covers display framebuffer, compositor allocations,
# driver overhead, and allocator fragmentation on the same monitor-driving GPU.
# It intentionally uses 1024, not 512, to put 10 GiB cards (10240 MiB) on the
# CPU side of the 10587 MiB threshold: without margin, only 677 MiB would remain
# after the two measured residents, which is not reliable display-attached
# operating slack.
PARAKEET_WORST_CASE_MIB = 5022
CO_FIT_MARGIN_MIB = 1024

CPU_PLACEMENT_COPY = (
    "sol thinks on your GPU; transcription runs on your CPU on this machine"
)
_DISCRETE_CLASSIFICATION = "discrete"


@dataclass(frozen=True)
class ParakeetPlacementDecision:
    force_cpu: bool
    reason_code: str
    tier_name: str | None
    tier_resident_mib: int | None
    parakeet_worst_case_mib: int
    margin_mib: int
    required_mib: int | None
    vram_mib: int | None


def _decision(
    *,
    force_cpu: bool,
    reason_code: str,
    tier_name: str | None,
    tier_resident_mib: int | None,
    required_mib: int | None,
    vram_mib: int | None,
) -> ParakeetPlacementDecision:
    return ParakeetPlacementDecision(
        force_cpu=force_cpu,
        reason_code=reason_code,
        tier_name=tier_name,
        tier_resident_mib=tier_resident_mib,
        parakeet_worst_case_mib=PARAKEET_WORST_CASE_MIB,
        margin_mib=CO_FIT_MARGIN_MIB,
        required_mib=required_mib,
        vram_mib=vram_mib,
    )


def is_discrete(device: Any, local_vulkan: Any) -> bool:
    """Return whether a pre-enumerated Vulkan device is classified discrete."""
    return local_vulkan.classify(device) == _DISCRETE_CLASSIFICATION


def discrete_hardware_gpu_count(
    devices: Sequence[Any],
    local_vulkan: Any,
) -> int:
    """Count hardware GPUs classified discrete from an existing Vulkan enumeration."""
    return sum(
        1
        for device in devices
        if local_vulkan.is_hardware_device(device) and is_discrete(device, local_vulkan)
    )


def cpu_placement_suffix(
    *,
    devices: Sequence[Any],
    selected: Any | None,
    local_vulkan: Any,
    unified_memory: bool,
    brain_lane_active: bool,
) -> str:
    """Return the advisory suffix when auto-placement resolves STT to CPU."""
    if selected is None:
        return ""
    decision = decide_parakeet_auto_placement(
        vram_mib=getattr(selected, "vram_mib", None),
        selected_device_is_discrete=is_discrete(selected, local_vulkan),
        discrete_hardware_gpu_count=discrete_hardware_gpu_count(devices, local_vulkan),
        unified_memory=unified_memory,
        brain_lane_active=brain_lane_active,
    )
    return f"; {CPU_PLACEMENT_COPY}" if decision.force_cpu else ""


def decide_parakeet_auto_placement(
    vram_mib: int | None,
    selected_device_is_discrete: bool,
    discrete_hardware_gpu_count: int,
    unified_memory: bool,
    brain_lane_active: bool,
) -> ParakeetPlacementDecision:
    """Return whether parakeet.cpp auto-placement must use CPU.

    This is intentionally pure: callers provide probe/config facts, and this
    function performs only tier selection and arithmetic.
    """
    if not brain_lane_active:
        return _decision(
            force_cpu=False,
            reason_code="brain_lane_inactive",
            tier_name=None,
            tier_resident_mib=None,
            required_mib=None,
            vram_mib=vram_mib,
        )
    if not selected_device_is_discrete:
        return _decision(
            force_cpu=False,
            reason_code="selected_device_not_discrete",
            tier_name=None,
            tier_resident_mib=None,
            required_mib=None,
            vram_mib=vram_mib,
        )
    if discrete_hardware_gpu_count != 1:
        return _decision(
            force_cpu=False,
            reason_code="discrete_gpu_count_not_one",
            tier_name=None,
            tier_resident_mib=None,
            required_mib=None,
            vram_mib=vram_mib,
        )
    if unified_memory:
        return _decision(
            force_cpu=False,
            reason_code="unified_memory",
            tier_name=None,
            tier_resident_mib=None,
            required_mib=None,
            vram_mib=vram_mib,
        )
    if vram_mib is None:
        return _decision(
            force_cpu=False,
            reason_code="vram_unknown",
            tier_name=None,
            tier_resident_mib=None,
            required_mib=None,
            vram_mib=None,
        )

    tier = select_server_tier(vram_mib)
    if tier.resident_mib is None:
        return _decision(
            force_cpu=False,
            reason_code="tier_residency_unmeasured",
            tier_name=tier.name,
            tier_resident_mib=None,
            required_mib=None,
            vram_mib=vram_mib,
        )

    required_mib = tier.resident_mib + PARAKEET_WORST_CASE_MIB + CO_FIT_MARGIN_MIB
    if vram_mib < required_mib:
        return _decision(
            force_cpu=True,
            reason_code="co_location_requires_cpu",
            tier_name=tier.name,
            tier_resident_mib=tier.resident_mib,
            required_mib=required_mib,
            vram_mib=vram_mib,
        )
    return _decision(
        force_cpu=False,
        reason_code="co_location_fits_gpu",
        tier_name=tier.name,
        tier_resident_mib=tier.resident_mib,
        required_mib=required_mib,
        vram_mib=vram_mib,
    )


__all__ = [
    "CO_FIT_MARGIN_MIB",
    "CPU_PLACEMENT_COPY",
    "PARAKEET_WORST_CASE_MIB",
    "ParakeetPlacementDecision",
    "cpu_placement_suffix",
    "decide_parakeet_auto_placement",
    "discrete_hardware_gpu_count",
    "is_discrete",
]
