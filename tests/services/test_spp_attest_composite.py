# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from solstone.think.models import AttestationFailedError
from solstone.think.services.spp_attest import snp as snp_module
from solstone.think.services.spp_attest.composite import verify_composite
from solstone.think.services.spp_attest.nvgpu.claims import GpuAppraisal
from solstone.think.services.spp_attest.nvgpu.errors import GpuAppraisalError
from solstone.think.services.spp_attest.snp import AppraisalStep
from solstone.think.services.spp_attest.tlv import GpuEnvelope

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "spp_attest"
NOW = datetime(2026, 7, 11, 18, 0, tzinfo=timezone.utc)


def _owner_nonce() -> bytes:
    return bytes.fromhex("".join((FIXTURE_DIR / "nonce.hex").read_text().split()))


def _envelope_tlv() -> bytes:
    return (FIXTURE_DIR / "gpu-envelope.tlv").read_bytes()


def _channel_binding() -> bytes:
    return (FIXTURE_DIR / "guest_x25519.pub.der").read_bytes()


def _copy_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    shutil.copytree(FIXTURE_DIR, bundle)
    return bundle


def _fake_nvattest_dir(tmp_path: Path) -> Path:
    root = tmp_path / "nvattest"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "nvattest").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "lib").mkdir()
    return root


def _gpu_appraisal(envelope: GpuEnvelope) -> GpuAppraisal:
    return GpuAppraisal(
        steps=[AppraisalStep("nvattest", "ok", "test")],
        driver_version="595.71.05",
        vbios_version="96.00.88.00.11",
        hwmodel="GH100 A01 GSP BROM",
        ueid="test-ueid",
        oemid="5703",
        eat_nonce=_owner_nonce().hex(),
        claims_version="3.0",
        arch=envelope.field(7).decode("utf-8").upper(),
        envelope_gpu_uuid="GPU-test-machine-id",
    )


def _safe_gpu_appraiser(
    envelope: GpuEnvelope,
    owner_nonce: bytes,
    *,
    nvattest_dir: Path,
) -> GpuAppraisal:
    assert owner_nonce == _owner_nonce()
    assert nvattest_dir
    return _gpu_appraisal(envelope)


def _assert_owner_message_safe(exc: BaseException) -> None:
    text = str(exc)
    forbidden = {
        _owner_nonce().hex(),
        "00" * 32,
        "-----BEGIN CERTIFICATE-----",
        "nonce_from_ar",
        "nvattest stderr tail",
        "TPM quote extraData mismatch",
        "VCEK did not sign",
        "GPU evidence",
    }
    for item in forbidden:
        assert item not in text
    assert text.startswith("Confidential attestation failed:")


def test_verify_composite_positive_fixture_with_injected_gpu_appraiser(
    tmp_path: Path,
) -> None:
    verdict = verify_composite(
        FIXTURE_DIR,
        envelope_tlv=_envelope_tlv(),
        channel_binding=_channel_binding(),
        owner_nonce=_owner_nonce(),
        now=NOW,
        nvattest_dir=tmp_path / "unused",
        gpu_appraiser=_safe_gpu_appraiser,
    )

    assert verdict.verified is True
    assert verdict.legs == ("cpu", "gpu")
    assert verdict.substrate == "AMD SEV-SNP + NVIDIA HOPPER"
    assert verdict.checked_at == NOW
    assert verdict.cpu_provenance.report_version == 5
    assert verdict.gpu_provenance.arch == "HOPPER"


def test_verify_composite_positive_fixture_through_real_gpu_appraiser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nvattest_dir = _fake_nvattest_dir(tmp_path)
    observed: dict[str, object] = {}

    def positive_run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv,
            0,
            (FIXTURE_DIR / "nvattest" / "positive.stdout").read_text(encoding="utf-8"),
            (FIXTURE_DIR / "nvattest" / "positive.stderr").read_text(encoding="utf-8"),
        )

    monkeypatch.setattr(
        "solstone.think.services.spp_attest.nvgpu.appraise.subprocess.run",
        positive_run,
    )

    verdict = verify_composite(
        FIXTURE_DIR,
        envelope_tlv=_envelope_tlv(),
        channel_binding=_channel_binding(),
        owner_nonce=_owner_nonce(),
        now=NOW,
        nvattest_dir=nvattest_dir,
    )

    assert verdict.substrate == "AMD SEV-SNP + NVIDIA HOPPER"
    argv = observed["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--nonce") + 1] == _owner_nonce().hex()


def test_verify_composite_rejects_tampered_embedded_cpu_report_without_leak(
    tmp_path: Path,
) -> None:
    bundle = _copy_bundle(tmp_path)
    hcl_path = bundle / "hcl_report.bin"
    hcl = bytearray(hcl_path.read_bytes())
    hcl[snp_module.HCL_REPORT_OFFSET + snp_module.SNP_OFF_MEASUREMENT] ^= 0x01
    hcl_path.write_bytes(bytes(hcl))

    with pytest.raises(AttestationFailedError) as exc_info:
        verify_composite(
            bundle,
            envelope_tlv=_envelope_tlv(),
            channel_binding=_channel_binding(),
            owner_nonce=_owner_nonce(),
            now=NOW,
            nvattest_dir=tmp_path / "unused",
            gpu_appraiser=_safe_gpu_appraiser,
        )

    assert exc_info.value.detail == (
        "the CPU leg rejected the evidence (cpu_verification_failed)"
    )
    _assert_owner_message_safe(exc_info.value)


def test_verify_composite_rejects_foreign_owner_nonce_before_legs_without_leak(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    shutil.copy2(FIXTURE_DIR / "nonce.hex", bundle / "nonce.hex")

    with pytest.raises(AttestationFailedError) as exc_info:
        verify_composite(
            bundle,
            envelope_tlv=_envelope_tlv(),
            channel_binding=_channel_binding(),
            owner_nonce=b"\x00" * 32,
            now=NOW,
            nvattest_dir=tmp_path / "unused",
            gpu_appraiser=_safe_gpu_appraiser,
        )

    assert exc_info.value.detail == (
        "the verifier nonce did not match the CPU bundle (nonce_mismatch)"
    )
    _assert_owner_message_safe(exc_info.value)


def test_verify_composite_rejects_mutated_channel_binding_without_leak(
    tmp_path: Path,
) -> None:
    channel_binding = bytearray(_channel_binding())
    channel_binding[0] ^= 0x01

    with pytest.raises(AttestationFailedError) as exc_info:
        verify_composite(
            FIXTURE_DIR,
            envelope_tlv=_envelope_tlv(),
            channel_binding=bytes(channel_binding),
            owner_nonce=_owner_nonce(),
            now=NOW,
            nvattest_dir=tmp_path / "unused",
            gpu_appraiser=_safe_gpu_appraiser,
        )

    assert exc_info.value.detail == (
        "the CPU leg rejected the evidence (cpu_verification_failed)"
    )
    _assert_owner_message_safe(exc_info.value)


def test_verify_composite_maps_gpu_nonce_mismatch_without_leak(
    tmp_path: Path,
) -> None:
    def rejecting_gpu_appraiser(
        _envelope: GpuEnvelope,
        owner_nonce: bytes,
        *,
        nvattest_dir: Path,
    ) -> GpuAppraisal:
        assert nvattest_dir
        raise GpuAppraisalError(
            "gpu_nonce_mismatch",
            f"raw nonce {owner_nonce.hex()}",
            stderr=f"nonce_from_ar: {owner_nonce.hex()}",
        )

    with pytest.raises(AttestationFailedError) as exc_info:
        verify_composite(
            FIXTURE_DIR,
            envelope_tlv=_envelope_tlv(),
            channel_binding=_channel_binding(),
            owner_nonce=_owner_nonce(),
            now=NOW,
            nvattest_dir=tmp_path / "unused",
            gpu_appraiser=rejecting_gpu_appraiser,
        )

    assert exc_info.value.detail == (
        "the GPU leg rejected the evidence (gpu_nonce_mismatch)"
    )
    assert isinstance(exc_info.value.__cause__, GpuAppraisalError)
    _assert_owner_message_safe(exc_info.value)


def test_verify_composite_rejects_gpu_unavailable_without_cpu_only_pass(
    tmp_path: Path,
) -> None:
    def unavailable_gpu_appraiser(
        _envelope: GpuEnvelope,
        _owner_nonce: bytes,
        *,
        nvattest_dir: Path,
    ) -> GpuAppraisal:
        assert nvattest_dir
        raise GpuAppraisalError("nvattest_unavailable", "missing nvattest")

    with pytest.raises(AttestationFailedError) as exc_info:
        verify_composite(
            FIXTURE_DIR,
            envelope_tlv=_envelope_tlv(),
            channel_binding=_channel_binding(),
            owner_nonce=_owner_nonce(),
            now=NOW,
            nvattest_dir=tmp_path / "unused",
            gpu_appraiser=unavailable_gpu_appraiser,
        )

    assert exc_info.value.detail == (
        "the GPU leg rejected the evidence (nvattest_unavailable)"
    )
    _assert_owner_message_safe(exc_info.value)
