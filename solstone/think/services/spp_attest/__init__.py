# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.think.services.spp_attest.binding import (
    BINDING_DOMAIN,
    check_envelope_nonce,
    composite_binding_hash,
)
from solstone.think.services.spp_attest.errors import VerificationError
from solstone.think.services.spp_attest.nvgpu.appraise import appraise_gpu_leg
from solstone.think.services.spp_attest.nvgpu.claims import GpuAppraisal
from solstone.think.services.spp_attest.nvgpu.errors import GpuAppraisalError
from solstone.think.services.spp_attest.nvgpu.evidence import to_nvattest_evidence
from solstone.think.services.spp_attest.snp import (
    AppraisalStep,
    CpuAppraisal,
    Policy,
    appraise_cpu_leg,
)
from solstone.think.services.spp_attest.tlv import (
    GpuEnvelope,
    decode_gpu_envelope,
    extract_spdm_nonce,
)
from solstone.think.services.spp_attest.tpm_quote import TpmQuoteVerifier, verify_quote

__all__ = [
    "BINDING_DOMAIN",
    "AppraisalStep",
    "CpuAppraisal",
    "GpuAppraisal",
    "GpuAppraisalError",
    "GpuEnvelope",
    "Policy",
    "TpmQuoteVerifier",
    "VerificationError",
    "appraise_cpu_leg",
    "appraise_gpu_leg",
    "check_envelope_nonce",
    "composite_binding_hash",
    "decode_gpu_envelope",
    "extract_spdm_nonce",
    "to_nvattest_evidence",
    "verify_quote",
]
