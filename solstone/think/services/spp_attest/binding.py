# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Composite binding checks for SPP CPU/GPU attestation."""

from __future__ import annotations

import hashlib

from solstone.think.services.spp_attest.errors import VerificationError
from solstone.think.services.spp_attest.tlv import (
    SPDM_NONCE_SIZE,
    GpuEnvelope,
    extract_spdm_nonce,
)

BINDING_DOMAIN = "sol-spp-option-a-bind-v1"


def composite_binding_hash(
    *,
    nonce: bytes,
    channel_binding: bytes,
    envelope_tlv: bytes,
    domain: str = BINDING_DOMAIN,
) -> bytes:
    """Hash the verifier nonce, channel binding, and GPU envelope fingerprint."""

    if len(nonce) != SPDM_NONCE_SIZE:
        raise VerificationError(f"binding nonce is {len(nonce)} bytes, expected 32")
    if not channel_binding:
        raise VerificationError("channel binding is empty")
    if not envelope_tlv:
        raise VerificationError("GPU envelope TLV is empty")
    if not domain:
        raise VerificationError("binding domain is empty")

    envelope_digest = hashlib.sha256(envelope_tlv).digest()
    digest = hashlib.sha256()
    digest.update(domain.encode("utf-8"))
    digest.update(nonce)
    digest.update(channel_binding)
    digest.update(envelope_digest)
    return digest.digest()


def check_envelope_nonce(envelope: GpuEnvelope, owner_nonce: bytes) -> None:
    """Validate the owner nonce is spliced into both GPU envelope nonce locations."""

    if len(owner_nonce) != SPDM_NONCE_SIZE:
        raise VerificationError(f"owner nonce is {len(owner_nonce)} bytes, expected 32")
    if envelope.nonce != owner_nonce:
        raise VerificationError("GPU envelope field-1 nonce does not match owner nonce")

    spdm_nonce = extract_spdm_nonce(envelope.spdm_report)
    if spdm_nonce != envelope.nonce:
        raise VerificationError(
            "SPDM report nonce does not match GPU envelope field-1 nonce"
        )
