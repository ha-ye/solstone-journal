# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations


class VerificationError(RuntimeError):
    """Raised when SPP attestation evidence fails appraisal."""


class PcrPinMismatchError(VerificationError):
    """Raised when a TPM PCR fingerprint is outside the pinned policy."""
