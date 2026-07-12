# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pure RA-TLS evidence verification for SPP confidential transport."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ObjectIdentifier

from solstone.think.models import AttestationFailedError
from solstone.think.services.spp_attest.composite import (
    CompositeVerdict,
    verify_composite,
)
from solstone.think.services.spp_attest.errors import VerificationError
from solstone.think.services.spp_attest.ratls.contract import (
    CERTIFICATE_BINDING_DOMAIN,
    COMPOSITE_EVIDENCE_OID,
    CompositeEvidence,
    ExporterProof,
    exporter_binding,
)
from solstone.think.services.spp_attest.snp import CpuBundle, Policy
from solstone.think.services.spp_attest.tpm_quote import verify_quote


class RatlsVerificationError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"confidential attestation rejected ({reason_code})")


@dataclass(frozen=True, slots=True)
class VerifiedCertificateEvidence:
    evidence: CompositeEvidence
    verdict: CompositeVerdict
    tls_spki_der: bytes


def _cpu_bundle_from_evidence(evidence: CompositeEvidence) -> CpuBundle:
    return CpuBundle(
        hcl_report=evidence.hcl_report,
        standalone_report=evidence.amd_report,
        cert_pems=(
            evidence.amd_ark_pem,
            evidence.amd_ask_pem,
            evidence.amd_vcek_pem,
        ),
        ak_public_key_pem=evidence.ak_public_key_pem,
        nonce=evidence.owner_nonce,
        quote_message=evidence.quote_message,
        quote_signature=evidence.quote_signature,
        quote_pcrs=evidence.quote_pcrs,
    )


def verify_certificate_evidence(
    *,
    certificate_der: bytes,
    owner_nonce: bytes,
    now: datetime,
    nvattest_dir: Path,
    roots_dir: Path | None = None,
    policy: Policy | None = None,
    quote_verifier: Callable[..., None] | None = None,
    composite_verifier: Callable[..., CompositeVerdict] = verify_composite,
) -> VerifiedCertificateEvidence:
    try:
        certificate = x509.load_der_x509_certificate(certificate_der)
    except ValueError:
        raise RatlsVerificationError("certificate_invalid")

    try:
        extension = certificate.extensions.get_extension_for_oid(
            ObjectIdentifier(COMPOSITE_EVIDENCE_OID)
        )
    except x509.ExtensionNotFound:
        raise RatlsVerificationError("certificate_extension_missing")
    if not extension.critical:
        raise RatlsVerificationError("certificate_extension_not_critical")
    if not isinstance(extension.value, x509.UnrecognizedExtension):
        raise RatlsVerificationError("certificate_extension_invalid")

    try:
        evidence = CompositeEvidence.from_der(extension.value.value)
    except ValueError:
        raise RatlsVerificationError("certificate_evidence_invalid")
    if evidence.owner_nonce != owner_nonce:
        raise RatlsVerificationError("nonce_mismatch")

    tls_spki_der = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if evidence.tls_spki_der != tls_spki_der:
        raise RatlsVerificationError("spki_mismatch")

    try:
        verdict = composite_verifier(
            _cpu_bundle_from_evidence(evidence),
            envelope_tlv=evidence.gpu_envelope,
            channel_binding=hashlib.sha256(tls_spki_der).digest(),
            owner_nonce=owner_nonce,
            now=now,
            nvattest_dir=nvattest_dir,
            binding_domain=CERTIFICATE_BINDING_DOMAIN,
            roots_dir=roots_dir,
            policy=policy,
            quote_verifier=quote_verifier,
        )
    except AttestationFailedError as exc:
        reason = getattr(exc, "detail", "")
        if "nonce_mismatch" in reason:
            code = "nonce_mismatch"
        elif "cpu_verification_failed" in reason:
            code = "cpu_verification_failed"
        elif "gpu_nonce_mismatch" in reason:
            code = "gpu_nonce_mismatch"
        elif "nvattest_unavailable" in reason:
            code = "nvattest_unavailable"
        elif "gpu_appraisal_failed" in reason:
            code = "gpu_appraisal_failed"
        else:
            code = "composite_appraisal_failed"
        raise RatlsVerificationError(code)

    return VerifiedCertificateEvidence(
        evidence=evidence,
        verdict=verdict,
        tls_spki_der=tls_spki_der,
    )


def verify_exporter_proof(
    *,
    proof_der: bytes,
    evidence: CompositeEvidence,
    tls_exporter: bytes,
    owner_nonce: bytes,
) -> None:
    try:
        proof = ExporterProof.from_der(proof_der)
    except ValueError:
        raise RatlsVerificationError("exporter_proof_invalid")
    if proof.owner_nonce != owner_nonce:
        raise RatlsVerificationError("nonce_mismatch")
    if proof.tls_spki_der != evidence.tls_spki_der:
        raise RatlsVerificationError("spki_mismatch")
    if proof.tls_exporter != tls_exporter:
        raise RatlsVerificationError("exporter_mismatch")

    try:
        verify_quote(
            ak_pub_pem=evidence.ak_public_key_pem,
            quote_msg=proof.quote_message,
            quote_sig=proof.quote_signature,
            quote_pcrs=proof.quote_pcrs,
            expected_binding=exporter_binding(
                owner_nonce,
                evidence.tls_spki_der,
                tls_exporter,
                evidence.gpu_envelope,
            ),
        )
    except VerificationError:
        raise RatlsVerificationError("exporter_quote_failed")
