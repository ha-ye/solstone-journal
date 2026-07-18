# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.think.providers import local_vulkan
from solstone.think.providers.parakeet_placement import (
    CO_FIT_MARGIN_MIB,
    PARAKEET_WORST_CASE_MIB,
    cpu_placement_suffix,
    decide_parakeet_auto_placement,
    discrete_hardware_gpu_count,
    is_discrete,
)

PLACEMENT_LINE = (
    "sol thinks on your GPU; transcription runs on your CPU on this machine"
)


def _device(
    *,
    index: int = 0,
    name: str = "Test GPU",
    device_type: int = local_vulkan.VK_TYPE_DISCRETE,
    vram_mib: int = 6144,
) -> local_vulkan.VulkanDevice:
    return local_vulkan.VulkanDevice(
        index=index,
        name=name,
        device_type=device_type,
        vram_mib=vram_mib,
    )


def _decision(
    vram_mib: int | None,
    *,
    selected_device_is_discrete: bool = True,
    discrete_hardware_gpu_count: int = 1,
    unified_memory: bool = False,
    brain_lane_active: bool = True,
):
    return decide_parakeet_auto_placement(
        vram_mib=vram_mib,
        selected_device_is_discrete=selected_device_is_discrete,
        discrete_hardware_gpu_count=discrete_hardware_gpu_count,
        unified_memory=unified_memory,
        brain_lane_active=brain_lane_active,
    )


def test_floor_tier_small_cards_force_cpu() -> None:
    decision = _decision(6144)

    assert decision.force_cpu is True
    assert decision.reason_code == "co_location_requires_cpu"
    assert decision.tier_name == "floor"
    assert decision.tier_resident_mib == 4147
    assert decision.parakeet_worst_case_mib == PARAKEET_WORST_CASE_MIB
    assert decision.margin_mib == CO_FIT_MARGIN_MIB
    assert decision.required_mib == 8118
    assert decision.vram_mib == 6144


@pytest.mark.parametrize(
    ("vram_mib", "force_cpu"),
    [
        (6144, True),
        (8117, True),
        (8118, False),
        (8192, False),
    ],
)
def test_floor_tier_placement_boundary(vram_mib: int, force_cpu: bool) -> None:
    decision = _decision(vram_mib)

    assert decision.tier_name == "floor"
    assert decision.tier_resident_mib == 4147
    assert decision.parakeet_worst_case_mib == PARAKEET_WORST_CASE_MIB
    assert decision.margin_mib == CO_FIT_MARGIN_MIB
    assert decision.required_mib == 8118
    assert decision.vram_mib == vram_mib
    assert decision.force_cpu is force_cpu


def test_capable_tier_unmeasured_residency_keeps_gpu() -> None:
    decision = _decision(16000)

    assert decision.force_cpu is False
    assert decision.reason_code == "tier_residency_unmeasured"
    assert decision.tier_name == "capable"
    assert decision.tier_resident_mib is None
    assert decision.required_mib is None


@pytest.mark.parametrize(
    ("kwargs", "reason_code"),
    [
        ({"vram_mib": None}, "vram_unknown"),
        ({"vram_mib": 6144, "brain_lane_active": False}, "brain_lane_inactive"),
        (
            {"vram_mib": 6144, "discrete_hardware_gpu_count": 2},
            "discrete_gpu_count_not_one",
        ),
        (
            {"vram_mib": 6144, "selected_device_is_discrete": False},
            "selected_device_not_discrete",
        ),
        ({"vram_mib": 6144, "unified_memory": True}, "unified_memory"),
    ],
)
def test_non_matching_inputs_keep_today(kwargs: dict, reason_code: str) -> None:
    decision = _decision(**kwargs)

    assert decision.force_cpu is False
    assert decision.reason_code == reason_code


def test_is_discrete_centralizes_vulkan_classification() -> None:
    assert is_discrete(_device(), local_vulkan) is True
    assert (
        is_discrete(
            _device(device_type=local_vulkan.VK_TYPE_INTEGRATED),
            local_vulkan,
        )
        is False
    )


def test_discrete_hardware_gpu_count_ignores_integrated_and_software() -> None:
    devices = [
        _device(index=0),
        _device(index=1, device_type=local_vulkan.VK_TYPE_INTEGRATED),
        _device(index=2, name="llvmpipe", device_type=local_vulkan.VK_TYPE_CPU),
    ]

    assert discrete_hardware_gpu_count(devices, local_vulkan) == 1


def test_cpu_placement_suffix_owns_joiner_and_copy() -> None:
    selected = _device(vram_mib=6144)

    assert (
        cpu_placement_suffix(
            devices=[selected],
            selected=selected,
            local_vulkan=local_vulkan,
            unified_memory=False,
            brain_lane_active=True,
        )
        == f"; {PLACEMENT_LINE}"
    )


@pytest.mark.parametrize(
    ("selected", "unified_memory", "brain_lane_active"),
    [
        (None, False, True),
        (_device(vram_mib=6144), True, True),
        (_device(vram_mib=6144), False, False),
        (_device(vram_mib=12288), False, True),
    ],
)
def test_cpu_placement_suffix_absent_outside_predicate(
    selected: local_vulkan.VulkanDevice | None,
    unified_memory: bool,
    brain_lane_active: bool,
) -> None:
    devices = [selected] if selected is not None else []

    assert (
        cpu_placement_suffix(
            devices=devices,
            selected=selected,
            local_vulkan=local_vulkan,
            unified_memory=unified_memory,
            brain_lane_active=brain_lane_active,
        )
        == ""
    )
