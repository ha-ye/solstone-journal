# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.think.services.spp_attest.pins import (
    PRODUCTION_PCR_SHA256_PINS,
    production_policy,
)


def test_production_pcr_pin_registry_literals() -> None:
    assert PRODUCTION_PCR_SHA256_PINS == frozenset(
        {"b162f46105c80d3e45028e37cc649404c9d65297ad1cda8f953208582060b0e3"}
    )

    first = production_policy()
    second = production_policy()

    assert first.pcr_mode == "pin"
    assert first.pcr_pins == set(PRODUCTION_PCR_SHA256_PINS)
    assert second.pcr_pins == set(PRODUCTION_PCR_SHA256_PINS)
    assert first.pcr_pins is not second.pcr_pins
    assert first.pcr_pins is not PRODUCTION_PCR_SHA256_PINS
    assert second.pcr_pins is not PRODUCTION_PCR_SHA256_PINS

    first.pcr_pins.add("00" * 32)
    assert first.pcr_pins != second.pcr_pins
    assert PRODUCTION_PCR_SHA256_PINS == frozenset(
        {"b162f46105c80d3e45028e37cc649404c9d65297ad1cda8f953208582060b0e3"}
    )
