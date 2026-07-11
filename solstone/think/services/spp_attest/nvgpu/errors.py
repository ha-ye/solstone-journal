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
STDERR_TAIL_CHARS = 4000


class GpuAppraisalError(VerificationError):
    """Raised when NVIDIA GPU appraisal fails."""

    reason: GpuAppraisalReason
    stderr: str

    def __init__(
        self,
        reason: GpuAppraisalReason,
        message: str,
        *,
        stderr: str = "",
    ) -> None:
        self.reason = reason
        self.stderr = _stderr_tail(stderr)
        detail = f"{reason}: {message}"
        if self.stderr:
            detail = f"{detail}\nnvattest stderr tail:\n{self.stderr}"
        super().__init__(detail)


def _stderr_tail(stderr: str) -> str:
    if not stderr:
        return ""
    if len(stderr) <= STDERR_TAIL_CHARS:
        return stderr
    return "[truncated to last 4000 chars]\n" + stderr[-STDERR_TAIL_CHARS:]
