# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""NVIDIA GPU-leg appraisal for SPP attestation."""

from __future__ import annotations

from solstone.think.services.spp_attest.nvgpu.appraise import appraise_gpu_leg
from solstone.think.services.spp_attest.nvgpu.claims import GpuAppraisal
from solstone.think.services.spp_attest.nvgpu.errors import GpuAppraisalError
from solstone.think.services.spp_attest.nvgpu.evidence import to_nvattest_evidence

__all__ = [
    "GpuAppraisal",
    "GpuAppraisalError",
    "appraise_gpu_leg",
    "to_nvattest_evidence",
]
