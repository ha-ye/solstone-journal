# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ObjectIdentifier

from solstone.think.models import AttestationFailedError
from solstone.think.services.spp_attest.composite import CompositeVerdict
from solstone.think.services.spp_attest.errors import VerificationError
from solstone.think.services.spp_attest.nvgpu.claims import GpuAppraisal
from solstone.think.services.spp_attest.ratls import verify as ratls_verify
from solstone.think.services.spp_attest.ratls.contract import (
    CERTIFICATE_BINDING_DOMAIN,
    COMPOSITE_EVIDENCE_OID,
    CompositeEvidence,
    ExporterProof,
    exporter_binding,
)
from solstone.think.services.spp_attest.ratls.verify import (
    RatlsVerificationError,
    verify_certificate_evidence,
    verify_exporter_proof,
)
from solstone.think.services.spp_attest.snp import AppraisalStep, CpuAppraisal

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


def _cpu_appraisal() -> CpuAppraisal:
    return CpuAppraisal(
        steps=[AppraisalStep("cpu", "ok", "fixture")],
        hcla_version=1,
        report_version=3,
        cpuid={"family": 25, "model": 1, "step": 1},
        tcb={},
        pcr_sha256="00",
        host_data="11",
        measurement="22",
        chip_id="33",
    )


def _gpu_appraisal() -> GpuAppraisal:
    return GpuAppraisal(
        steps=[AppraisalStep("gpu", "ok", "fixture")],
        driver_version="570.0",
        vbios_version="1.0",
        hwmodel="B200",
        ueid="ueid",
        oemid="oem",
        eat_nonce="nonce",
        claims_version="1",
        arch="blackwell",
        envelope_gpu_uuid="gpu-uuid",
    )


def _verdict(now: datetime = NOW) -> CompositeVerdict:
    return CompositeVerdict(
        verified=True,
        legs=("cpu", "gpu"),
        substrate="fixture",
        checked_at=now,
        cpu_provenance=_cpu_appraisal(),
        gpu_provenance=_gpu_appraisal(),
    )


def _key_and_spki() -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    spki = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return key, spki


def _evidence(
    owner_nonce: bytes, spki: bytes, *, ak: bytes = b"ak-pem"
) -> CompositeEvidence:
    return CompositeEvidence(
        owner_nonce=owner_nonce,
        tls_spki_der=spki,
        amd_report=b"amd-report",
        hcl_report=b"hcl-report",
        ak_public_key_pem=ak,
        quote_message=b"p1-message",
        quote_signature=b"p1-signature",
        quote_pcrs=b"p1-pcrs",
        amd_ark_pem=b"ark",
        amd_ask_pem=b"ask",
        amd_vcek_pem=b"vcek",
        gpu_envelope=b"gpu-envelope",
    )


def _certificate_der(
    key: ec.EllipticCurvePrivateKey,
    evidence: CompositeEvidence,
    *,
    critical: bool = True,
) -> bytes:
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "spp-engine-test")]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(1001)
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=1))
        .add_extension(
            x509.UnrecognizedExtension(
                ObjectIdentifier(COMPOSITE_EVIDENCE_OID),
                evidence.to_der(),
            ),
            critical=critical,
        )
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


def test_verify_certificate_evidence_passes_bytes_bundle_to_composite_verifier(
    tmp_path: Path,
) -> None:
    nonce = b"n" * 32
    key, spki = _key_and_spki()
    evidence = _evidence(nonce, spki)
    seen: dict[str, Any] = {}

    def composite_verifier(bundle, **kwargs):
        seen["bundle"] = bundle
        seen["kwargs"] = kwargs
        return _verdict(kwargs["now"])

    verified = verify_certificate_evidence(
        certificate_der=_certificate_der(key, evidence),
        owner_nonce=nonce,
        now=NOW,
        nvattest_dir=tmp_path,
        quote_verifier="quote-verifier",
        composite_verifier=composite_verifier,
    )

    assert verified.evidence == evidence
    assert verified.tls_spki_der == spki
    assert seen["bundle"].hcl_report == evidence.hcl_report
    assert seen["bundle"].standalone_report == evidence.amd_report
    assert seen["bundle"].cert_pems == (
        evidence.amd_ark_pem,
        evidence.amd_ask_pem,
        evidence.amd_vcek_pem,
    )
    assert seen["bundle"].ak_public_key_pem == evidence.ak_public_key_pem
    assert seen["bundle"].nonce == nonce
    assert seen["kwargs"]["binding_domain"] == CERTIFICATE_BINDING_DOMAIN
    assert seen["kwargs"]["owner_nonce"] == nonce
    assert seen["kwargs"]["quote_verifier"] == "quote-verifier"


def test_verify_certificate_evidence_rejects_noncritical_extension(
    tmp_path: Path,
) -> None:
    nonce = b"n" * 32
    key, spki = _key_and_spki()
    evidence = _evidence(nonce, spki)

    with pytest.raises(RatlsVerificationError) as exc_info:
        verify_certificate_evidence(
            certificate_der=_certificate_der(key, evidence, critical=False),
            owner_nonce=nonce,
            now=NOW,
            nvattest_dir=tmp_path,
        )

    assert exc_info.value.reason_code == "certificate_extension_not_critical"


def test_verify_certificate_evidence_rejects_nonce_mismatch(tmp_path: Path) -> None:
    key, spki = _key_and_spki()
    evidence = _evidence(b"n" * 32, spki)

    with pytest.raises(RatlsVerificationError) as exc_info:
        verify_certificate_evidence(
            certificate_der=_certificate_der(key, evidence),
            owner_nonce=b"m" * 32,
            now=NOW,
            nvattest_dir=tmp_path,
        )

    assert exc_info.value.reason_code == "nonce_mismatch"


def test_verify_certificate_evidence_rejects_spki_mismatch(tmp_path: Path) -> None:
    nonce = b"n" * 32
    cert_key, _cert_spki = _key_and_spki()
    _foreign_key, foreign_spki = _key_and_spki()
    evidence = _evidence(nonce, foreign_spki)

    with pytest.raises(RatlsVerificationError) as exc_info:
        verify_certificate_evidence(
            certificate_der=_certificate_der(cert_key, evidence),
            owner_nonce=nonce,
            now=NOW,
            nvattest_dir=tmp_path,
        )

    assert exc_info.value.reason_code == "spki_mismatch"


def test_verify_certificate_evidence_maps_composite_failure(tmp_path: Path) -> None:
    nonce = b"n" * 32
    key, spki = _key_and_spki()
    evidence = _evidence(nonce, spki)

    def composite_verifier(_bundle, **_kwargs):
        raise AttestationFailedError(
            "the CPU leg rejected evidence (cpu_verification_failed)"
        )

    with pytest.raises(RatlsVerificationError) as exc_info:
        verify_certificate_evidence(
            certificate_der=_certificate_der(key, evidence),
            owner_nonce=nonce,
            now=NOW,
            nvattest_dir=tmp_path,
            composite_verifier=composite_verifier,
        )

    assert exc_info.value.reason_code == "cpu_verification_failed"


def test_verify_exporter_proof_binds_quote_to_exporter(monkeypatch) -> None:
    nonce = b"n" * 32
    _key, spki = _key_and_spki()
    tls_exporter = b"e" * 32
    evidence = _evidence(nonce, spki)
    proof = ExporterProof(
        owner_nonce=nonce,
        tls_spki_der=spki,
        tls_exporter=tls_exporter,
        quote_message=b"p2-message",
        quote_signature=b"p2-signature",
        quote_pcrs=b"p2-pcrs",
    )
    seen: dict[str, Any] = {}

    def verify_quote(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(ratls_verify, "verify_quote", verify_quote)

    verify_exporter_proof(
        proof_der=proof.to_der(),
        evidence=evidence,
        tls_exporter=tls_exporter,
        owner_nonce=nonce,
    )

    assert seen == {
        "ak_pub_pem": evidence.ak_public_key_pem,
        "quote_msg": proof.quote_message,
        "quote_sig": proof.quote_signature,
        "quote_pcrs": proof.quote_pcrs,
        "expected_binding": exporter_binding(
            nonce,
            spki,
            tls_exporter,
            evidence.gpu_envelope,
        ),
    }


def test_verify_exporter_proof_rejects_exporter_mismatch() -> None:
    nonce = b"n" * 32
    _key, spki = _key_and_spki()
    evidence = _evidence(nonce, spki)
    proof = ExporterProof(nonce, spki, b"foreign", b"msg", b"sig", b"pcrs")

    with pytest.raises(RatlsVerificationError) as exc_info:
        verify_exporter_proof(
            proof_der=proof.to_der(),
            evidence=evidence,
            tls_exporter=b"mine",
            owner_nonce=nonce,
        )

    assert exc_info.value.reason_code == "exporter_mismatch"


def test_verify_exporter_proof_rejects_quote_under_wrong_ak(monkeypatch) -> None:
    nonce = b"n" * 32
    _key, spki = _key_and_spki()
    tls_exporter = b"e" * 32
    evidence = _evidence(nonce, spki, ak=b"ak-a")
    proof = ExporterProof(nonce, spki, tls_exporter, b"msg", b"sig", b"pcrs")

    def verify_quote(**_kwargs):
        raise VerificationError("bad quote signature")

    monkeypatch.setattr(ratls_verify, "verify_quote", verify_quote)

    with pytest.raises(RatlsVerificationError) as exc_info:
        verify_exporter_proof(
            proof_der=proof.to_der(),
            evidence=evidence,
            tls_exporter=tls_exporter,
            owner_nonce=nonce,
        )

    assert exc_info.value.reason_code == "exporter_quote_failed"
