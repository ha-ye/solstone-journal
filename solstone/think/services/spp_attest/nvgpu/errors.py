# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""GPU appraisal error types."""

from __future__ import annotations

from typing import Literal

from solstone.think.services.spp_attest.errors import VerificationError

GpuAppraisalReason = Literal[
    "nvattest_unavailable",
    "gpu_nonce_mismatch",
    "gpu_appraisal_failed",
]


class GpuAppraisalError(VerificationError):
    """Raised when NVIDIA GPU appraisal fails."""

    reason: GpuAppraisalReason

    def __init__(self, reason: GpuAppraisalReason) -> None:
        self.reason = reason
        super().__init__(reason)
