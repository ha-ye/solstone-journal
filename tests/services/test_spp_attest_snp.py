# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from solstone.think.services.spp_attest import (
    Policy,
    VerificationError,
    appraise_cpu_leg,
    load_cpu_bundle,
)
from solstone.think.services.spp_attest import snp as snp_module

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "spp_attest"
PCR_SHA256_HEX = "b162f46105c80d3e45028e37cc649404c9d65297ad1cda8f953208582060b0e3"
EXPECTED_STEP_NAMES = [
    "hcla",
    "runtime-binding",
    "amd-chain",
    "amd-report-signature",
    "snp-policy",
    "ak-binding",
    "quote",
    "pcr-policy",
]
EXPECTED_TCB = {
    "boot_loader": 10,
    "microcode": 88,
    "snp": 27,
    "tee": 0,
    "fmc": None,
}


def _appraise(
    bundle_dir: Path = FIXTURE_DIR,
    *,
    envelope_tlv: bytes | None = None,
    channel_binding: bytes | None = None,
    roots_dir: Path | None = None,
    policy: Policy | None = None,
):
    kwargs = {
        "envelope_tlv": envelope_tlv
        if envelope_tlv is not None
        else (FIXTURE_DIR / "gpu-envelope.tlv").read_bytes(),
        "channel_binding": channel_binding
        if channel_binding is not None
        else (FIXTURE_DIR / "guest_x25519.pub.der").read_bytes(),
    }
    if roots_dir is not None:
        kwargs["roots_dir"] = roots_dir
    if policy is not None:
        kwargs["policy"] = policy
    return appraise_cpu_leg(load_cpu_bundle(bundle_dir), **kwargs)


def _copy_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    shutil.copytree(FIXTURE_DIR, bundle)
    return bundle


def _expected_steps_from_fixture() -> list[dict[str, str]]:
    data = json.loads((FIXTURE_DIR / "cpu-appraisal.json").read_text(encoding="utf-8"))
    return [step for step in data["steps"] if step["name"] != "key-release"]


def _actual_steps(result) -> list[dict[str, str]]:
    return [
        {"name": step.name, "status": step.status, "detail": step.detail}
        for step in result.steps
    ]


def _tlv_field_spans(data: bytes) -> dict[int, tuple[int, int, int]]:
    count = int.from_bytes(data[8:10], "big")
    offset = 10
    spans: dict[int, tuple[int, int, int]] = {}
    for _index in range(count):
        header_start = offset
        field_id = int.from_bytes(data[offset : offset + 2], "big")
        length = int.from_bytes(data[offset + 2 : offset + 6], "big")
        value_start = offset + 6
        value_end = value_start + length
        spans[field_id] = (header_start, value_start, value_end)
        offset = value_end
    return spans


def _mutate_tlv_field_one_nonce(tlv: bytes) -> bytes:
    data = bytearray(tlv)
    _header_start, value_start, _value_end = _tlv_field_spans(data)[1]
    data[value_start] ^= 0x01
    return bytes(data)


def _load_cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def _generated_cert_with_subject(subject: x509.Name, *, ca: bool) -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def test_appraise_cpu_leg_positive_matches_captured_cpu_appraisal() -> None:
    result = _appraise()

    assert len(result.steps) == 8
    assert [step.name for step in result.steps] == EXPECTED_STEP_NAMES
    assert _actual_steps(result) == _expected_steps_from_fixture()
    assert result.report_version == 5
    assert result.hcla_version == 2
    assert result.cpuid == {"family": 25, "model": 17, "step": 1}
    assert result.tcb == {
        "current": EXPECTED_TCB,
        "reported": EXPECTED_TCB,
        "committed": EXPECTED_TCB,
        "launch": EXPECTED_TCB,
    }
    assert result.pcr_sha256 == PCR_SHA256_HEX


def test_appraise_cpu_leg_records_standalone_report_difference(
    tmp_path: Path,
) -> None:
    bundle = _copy_bundle(tmp_path)
    (bundle / "report.bin").write_bytes(b"not the HCLA-embedded report")

    result = _appraise(bundle)

    assert len(result.steps) == 9
    assert [step.name for step in result.steps][:2] == ["hcla", "standalone-report"]
    assert result.report_version == 5
    assert result.cpuid == {"family": 25, "model": 17, "step": 1}


def test_appraise_cpu_leg_rejects_tampered_snp_report_signature(
    tmp_path: Path,
) -> None:
    bundle = _copy_bundle(tmp_path)
    hcl_path = bundle / "hcl_report.bin"
    hcl = bytearray(hcl_path.read_bytes())
    signed_measurement_offset = (
        snp_module.HCL_REPORT_OFFSET + snp_module.SNP_OFF_MEASUREMENT
    )
    hcl[signed_measurement_offset] ^= 0x01
    hcl_path.write_bytes(bytes(hcl))

    with pytest.raises(VerificationError, match="VCEK did not sign"):
        _appraise(bundle)


def test_appraise_cpu_leg_rejects_foreign_root_set_selection(
    tmp_path: Path,
) -> None:
    roots_dir = tmp_path / "roots"
    shutil.copytree(snp_module.DEFAULT_ROOTS_DIR / "Milan", roots_dir / "Milan")

    with pytest.raises(VerificationError, match="no pinned AMD ASK"):
        _appraise(roots_dir=roots_dir)


def test_appraise_cpu_leg_rejects_broken_amd_chain(
    tmp_path: Path,
) -> None:
    roots_dir = tmp_path / "roots"
    genoa_dir = roots_dir / "Genoa"
    genoa_dir.mkdir(parents=True)
    shutil.copy2(
        snp_module.DEFAULT_ROOTS_DIR / "Genoa" / "ask.pem", genoa_dir / "ask.pem"
    )
    shutil.copy2(
        snp_module.DEFAULT_ROOTS_DIR / "Milan" / "ark.pem", genoa_dir / "ark.pem"
    )

    with pytest.raises(VerificationError, match="certificate signature invalid"):
        _appraise(roots_dir=roots_dir)


@pytest.mark.parametrize(
    ("root_file", "error_label"),
    [("ark.pem", "ARK"), ("ask.pem", "ASK")],
)
def test_appraise_cpu_leg_rejects_non_ca_pinned_root(
    tmp_path: Path,
    root_file: str,
    error_label: str,
) -> None:
    roots_dir = tmp_path / "roots"
    genoa_dir = roots_dir / "Genoa"
    shutil.copytree(snp_module.DEFAULT_ROOTS_DIR / "Genoa", genoa_dir)
    original = _load_cert(genoa_dir / root_file)
    (genoa_dir / root_file).write_bytes(
        _generated_cert_with_subject(original.subject, ca=False)
    )

    with pytest.raises(VerificationError, match=f"{error_label} is not a CA"):
        _appraise(roots_dir=roots_dir)


@pytest.mark.parametrize("bundle_ca_file", ["ark.pem", "ask.pem"])
def test_appraise_cpu_leg_rejects_bundle_ca_override_same_cn(
    tmp_path: Path,
    bundle_ca_file: str,
) -> None:
    bundle = _copy_bundle(tmp_path)
    bundle_ca_path = bundle / "certs" / bundle_ca_file
    original = _load_cert(bundle_ca_path)
    bundle_ca_path.write_bytes(_generated_cert_with_subject(original.subject, ca=True))

    with pytest.raises(VerificationError, match="does not match pinned root material"):
        _appraise(bundle)


def test_appraise_cpu_leg_rejects_foreign_ak_public_key(
    tmp_path: Path,
) -> None:
    bundle = _copy_bundle(tmp_path)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (bundle / "akpub.pem").write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    with pytest.raises(VerificationError, match="does not match AMD-bound HCLAkPub"):
        _appraise(bundle)


def test_appraise_cpu_leg_rejects_non_matching_pcr_pin() -> None:
    policy = Policy(pcr_mode="pin", pcr_pins={"00" * 32})

    with pytest.raises(VerificationError, match="not in pinned policy"):
        _appraise(policy=policy)


def test_appraise_cpu_leg_rejects_tlv_splice_before_appraisal_steps() -> None:
    envelope_tlv = _mutate_tlv_field_one_nonce(
        (FIXTURE_DIR / "gpu-envelope.tlv").read_bytes()
    )

    with pytest.raises(VerificationError, match="field-1 nonce"):
        _appraise(envelope_tlv=envelope_tlv)
