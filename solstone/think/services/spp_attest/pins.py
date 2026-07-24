# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Production PCR pin policy for SPP composite attestation."""

from __future__ import annotations

from solstone.think.services.spp_attest.snp import Policy

# substrate: spp-engine-01 (processing.solstone.app:9443, Azure Standard_NCC40ads_H100_v5)
# pcr_sha256 pin: b162f46105c80d3e45028e37cc649404c9d65297ad1cda8f953208582060b0e3
# Provenance: captured live from the production substrate, 2026-07-24,
# operator decision record; observed identical across two fresh RA-TLS sessions
# via the journal-side CPU-leg appraisal.
PRODUCTION_PCR_SHA256_PINS: frozenset[str] = frozenset(
    {
        "b162f46105c80d3e45028e37cc649404c9d65297ad1cda8f953208582060b0e3",
    }
)


def production_policy() -> Policy:
    return Policy(pcr_mode="pin", pcr_pins=set(PRODUCTION_PCR_SHA256_PINS))
