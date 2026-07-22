# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
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
from solstone.think.services.spp_attest.snp import (
    AppraisalStep,
    CpuBundle,
    load_cpu_bundle,
)
from solstone.think.services.spp_attest.tlv import GpuEnvelope

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "spp_attest"
NOW = datetime(2026, 7, 11, 18, 0, tzinfo=timezone.utc)
ATTESTED_HWMODEL = "GH100 A01 GSP BROM"
HOSTILE_ARCH = "<script>alert(1)</script>"


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


def _cpu_bundle(bundle_dir: Path = FIXTURE_DIR):
    return load_cpu_bundle(bundle_dir)


def _nonce_only_bundle() -> CpuBundle:
    return CpuBundle(
        hcl_report=b"",
        standalone_report=None,
        cert_pems=(),
        ak_public_key_pem=b"",
        nonce=_owner_nonce(),
        quote_message=b"",
        quote_signature=b"",
        quote_pcrs=b"",
    )


def _fake_nvattest_dir(tmp_path: Path) -> Path:
    root = tmp_path / "nvattest"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "nvattest").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "lib").mkdir()
    (root / "share" / "ca").mkdir(parents=True)
    (root / "share" / "ca" / "ca-bundle.pem").write_text("ca\n", encoding="utf-8")
    return root


def _gpu_appraisal(
    envelope: GpuEnvelope,
    *,
    arch: str | None = None,
    hwmodel: str = ATTESTED_HWMODEL,
) -> GpuAppraisal:
    return GpuAppraisal(
        steps=[AppraisalStep("nvattest", "ok", "test")],
        driver_version="595.71.05",
        vbios_version="96.00.88.00.11",
        hwmodel=hwmodel,
        ueid="test-ueid",
        oemid="5703",
        eat_nonce=_owner_nonce().hex(),
        claims_version="3.0",
        arch=arch or envelope.field(7).decode("utf-8").upper(),
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
        _cpu_bundle(),
        envelope_tlv=_envelope_tlv(),
        channel_binding=_channel_binding(),
        owner_nonce=_owner_nonce(),
        now=NOW,
        nvattest_dir=tmp_path / "unused",
        gpu_appraiser=_safe_gpu_appraiser,
    )

    assert verdict.verified is True
    assert verdict.legs == ("cpu", "gpu")
    assert verdict.substrate == f"AMD SEV-SNP + NVIDIA {ATTESTED_HWMODEL}"
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
        _cpu_bundle(),
        envelope_tlv=_envelope_tlv(),
        channel_binding=_channel_binding(),
        owner_nonce=_owner_nonce(),
        now=NOW,
        nvattest_dir=nvattest_dir,
    )

    assert verdict.substrate == f"AMD SEV-SNP + NVIDIA {ATTESTED_HWMODEL}"
    argv = observed["argv"]
    assert isinstance(argv, list)
    assert argv[argv.index("--nonce") + 1] == _owner_nonce().hex()


def test_verify_composite_substrate_uses_attested_hwmodel_not_envelope_arch(
    tmp_path: Path,
) -> None:
    def hostile_arch_appraiser(
        envelope: GpuEnvelope,
        owner_nonce: bytes,
        *,
        nvattest_dir: Path,
    ) -> GpuAppraisal:
        assert owner_nonce == _owner_nonce()
        assert nvattest_dir
        return _gpu_appraisal(envelope, arch=HOSTILE_ARCH)

    verdict = verify_composite(
        _cpu_bundle(),
        envelope_tlv=_envelope_tlv(),
        channel_binding=_channel_binding(),
        owner_nonce=_owner_nonce(),
        now=NOW,
        nvattest_dir=tmp_path / "unused",
        gpu_appraiser=hostile_arch_appraiser,
    )

    assert verdict.gpu_provenance.arch == HOSTILE_ARCH
    assert verdict.substrate == f"AMD SEV-SNP + NVIDIA {ATTESTED_HWMODEL}"
    assert HOSTILE_ARCH not in verdict.substrate


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
            _cpu_bundle(bundle),
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
    with pytest.raises(AttestationFailedError) as exc_info:
        verify_composite(
            _nonce_only_bundle(),
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
            _cpu_bundle(),
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
    caplog: pytest.LogCaptureFixture,
) -> None:
    def rejecting_gpu_appraiser(
        _envelope: GpuEnvelope,
        _owner_nonce: bytes,
        *,
        nvattest_dir: Path,
    ) -> GpuAppraisal:
        assert nvattest_dir
        raise GpuAppraisalError("gpu_nonce_mismatch")

    with caplog.at_level(
        logging.WARNING,
        logger="solstone.think.services.spp_attest.composite",
    ):
        with pytest.raises(AttestationFailedError) as exc_info:
            verify_composite(
                _cpu_bundle(),
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
    assert exc_info.value.__cause__ is None
    assert "gpu_nonce_mismatch" in caplog.text
    assert _owner_nonce().hex() not in caplog.text
    assert "nonce_from_ar" not in caplog.text
    _assert_owner_message_safe(exc_info.value)


def test_verify_composite_bounds_out_of_band_gpu_reason_without_leak(
    tmp_path: Path,
) -> None:
    unsafe_reason = f"unsafe-reason-{_owner_nonce().hex()}"

    def rejecting_gpu_appraiser(
        _envelope: GpuEnvelope,
        _owner_nonce: bytes,
        *,
        nvattest_dir: Path,
    ) -> GpuAppraisal:
        assert nvattest_dir
        raise GpuAppraisalError(unsafe_reason)

    with pytest.raises(AttestationFailedError) as exc_info:
        verify_composite(
            _cpu_bundle(),
            envelope_tlv=_envelope_tlv(),
            channel_binding=_channel_binding(),
            owner_nonce=_owner_nonce(),
            now=NOW,
            nvattest_dir=tmp_path / "unused",
            gpu_appraiser=rejecting_gpu_appraiser,
        )

    assert exc_info.value.detail == (
        "the GPU leg rejected the evidence (gpu_appraisal_failed)"
    )
    assert unsafe_reason not in str(exc_info.value)
    _assert_owner_message_safe(exc_info.value)


@pytest.mark.parametrize(
    "reason_code",
    [
        "nvattest_unavailable",
        "nvattest_integrity_failed",
    ],
)
def test_verify_composite_rejects_gpu_appraiser_prerequisite_without_cpu_only_pass(
    tmp_path: Path,
    reason_code: str,
) -> None:
    def unavailable_gpu_appraiser(
        _envelope: GpuEnvelope,
        _owner_nonce: bytes,
        *,
        nvattest_dir: Path,
    ) -> GpuAppraisal:
        assert nvattest_dir
        raise GpuAppraisalError(reason_code)

    with pytest.raises(AttestationFailedError) as exc_info:
        verify_composite(
            _cpu_bundle(),
            envelope_tlv=_envelope_tlv(),
            channel_binding=_channel_binding(),
            owner_nonce=_owner_nonce(),
            now=NOW,
            nvattest_dir=tmp_path / "unused",
            gpu_appraiser=unavailable_gpu_appraiser,
        )

    assert exc_info.value.detail == f"the GPU leg rejected the evidence ({reason_code})"
    _assert_owner_message_safe(exc_info.value)
