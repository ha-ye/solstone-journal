# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Composite SPP verifier; envelope nonce and binding hash checks live in appraise_cpu_leg."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from solstone.think.models import AttestationFailedError
from solstone.think.services.spp_attest.errors import VerificationError
from solstone.think.services.spp_attest.nvgpu.appraise import appraise_gpu_leg
from solstone.think.services.spp_attest.nvgpu.claims import GpuAppraisal
from solstone.think.services.spp_attest.nvgpu.errors import GpuAppraisalError
from solstone.think.services.spp_attest.snp import (
    CpuAppraisal,
    Policy,
    appraise_cpu_leg,
    read_bundle_nonce,
)
from solstone.think.services.spp_attest.tlv import decode_gpu_envelope
from solstone.think.services.spp_attest.tpm_quote import TpmQuoteVerifier

log = logging.getLogger(__name__)

_GPU_REASONS = frozenset(
    {"nvattest_unavailable", "gpu_nonce_mismatch", "gpu_appraisal_failed"}
)


@dataclass(frozen=True, slots=True)
class CompositeVerdict:
    verified: bool
    legs: tuple[str, ...]
    substrate: str
    checked_at: datetime
    cpu_provenance: CpuAppraisal
    gpu_provenance: GpuAppraisal


def verify_composite(
    bundle_dir: Path,
    *,
    envelope_tlv: bytes,
    channel_binding: bytes,
    owner_nonce: bytes,
    now: datetime,
    nvattest_dir: Path,
    roots_dir: Path | None = None,
    policy: Policy | None = None,
    quote_verifier: TpmQuoteVerifier | None = None,
    gpu_appraiser: Callable[..., GpuAppraisal] | None = None,
) -> CompositeVerdict:
    try:
        bundle_nonce = read_bundle_nonce(bundle_dir / "nonce.hex")
    except (OSError, VerificationError) as exc:
        _raise_attestation_failed(
            "the CPU bundle nonce was invalid (nonce_invalid)",
            "confidential attestation nonce guard failed: nonce_invalid",
            exc,
        )
    if bundle_nonce != owner_nonce:
        log.warning("confidential attestation nonce guard failed: nonce_mismatch")
        raise AttestationFailedError(
            "the verifier nonce did not match the CPU bundle (nonce_mismatch)"
        )

    try:
        cpu_provenance = appraise_cpu_leg(
            bundle_dir,
            envelope_tlv=envelope_tlv,
            channel_binding=channel_binding,
            roots_dir=roots_dir,
            policy=policy,
            quote_verifier=quote_verifier,
        )
    except VerificationError as exc:
        _raise_attestation_failed(
            "the CPU leg rejected the evidence (cpu_verification_failed)",
            "confidential attestation CPU leg failed",
            exc,
        )

    envelope = decode_gpu_envelope(envelope_tlv)

    appraiser = gpu_appraiser or appraise_gpu_leg
    try:
        gpu_provenance = appraiser(
            envelope,
            owner_nonce,
            nvattest_dir=nvattest_dir,
        )
    except GpuAppraisalError as exc:
        reason = exc.reason if exc.reason in _GPU_REASONS else "gpu_appraisal_failed"
        _raise_attestation_failed(
            f"the GPU leg rejected the evidence ({reason})",
            "confidential attestation GPU leg failed",
            exc,
        )
    except Exception as exc:
        _raise_attestation_failed(
            "the verifier encountered an internal error (unexpected_error)",
            "confidential attestation GPU leg raised unexpectedly",
            exc,
        )

    return CompositeVerdict(
        verified=True,
        legs=("cpu", "gpu"),
        substrate=f"AMD SEV-SNP + NVIDIA {gpu_provenance.hwmodel}",
        checked_at=now,
        cpu_provenance=cpu_provenance,
        gpu_provenance=gpu_provenance,
    )


def _raise_attestation_failed(
    detail: str,
    log_message: str,
    exc: BaseException,
) -> NoReturn:
    log.warning(log_message, exc_info=True)
    raise AttestationFailedError(detail) from exc
