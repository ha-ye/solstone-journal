# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from solstone.think.services.spp_attest.cadence import (
    GPU_REATTEST_INTERVAL,
    SESSION_CAP,
    TPM_HEARTBEAT_INTERVAL,
    AttestationSession,
)
from solstone.think.services.spp_attest.composite import CompositeVerdict
from solstone.think.services.spp_attest.nvgpu.claims import GpuAppraisal
from solstone.think.services.spp_attest.snp import AppraisalStep, CpuAppraisal

NOW = datetime(2026, 7, 11, 18, 0, tzinfo=timezone.utc)


def _cpu_appraisal() -> CpuAppraisal:
    return CpuAppraisal(
        steps=[AppraisalStep("cpu", "ok", "test")],
        hcla_version=2,
        report_version=5,
        cpuid={"family": 25, "model": 17, "step": 1},
        tcb={},
        pcr_sha256="pcr-machine-id",
        host_data="host-data",
        measurement="measurement",
        chip_id="chip-machine-id",
    )


def _gpu_appraisal() -> GpuAppraisal:
    return GpuAppraisal(
        steps=[AppraisalStep("gpu", "ok", "test")],
        driver_version="595.71.05",
        vbios_version="96.00.88.00.11",
        hwmodel="GH100 A01 GSP BROM",
        ueid="ueid-machine-id",
        oemid="5703",
        eat_nonce="eat-nonce",
        claims_version="3.0",
        arch="HOPPER",
        envelope_gpu_uuid="GPU-machine-id",
    )


def _verdict() -> CompositeVerdict:
    return CompositeVerdict(
        verified=True,
        legs=("cpu", "gpu"),
        substrate="AMD SEV-SNP + NVIDIA HOPPER",
        checked_at=NOW,
        cpu_provenance=_cpu_appraisal(),
        gpu_provenance=_gpu_appraisal(),
    )


def _session(
    *,
    started_at: datetime = NOW,
    tpm_heartbeat_at: datetime = NOW,
    gpu_reattest_at: datetime = NOW,
) -> AttestationSession:
    return AttestationSession(
        verdict=_verdict(),
        started_at=started_at,
        tpm_heartbeat_at=tpm_heartbeat_at,
        gpu_reattest_at=gpu_reattest_at,
    )


def test_attestation_session_due_properties() -> None:
    session = _session()

    assert session.tpm_heartbeat_due_at == NOW + TPM_HEARTBEAT_INTERVAL
    assert session.gpu_reattest_due_at == NOW + GPU_REATTEST_INTERVAL
    assert session.session_cap_at == NOW + SESSION_CAP


def test_attestation_session_verified_before_all_windows() -> None:
    session = _session()

    assert session.status(NOW + TPM_HEARTBEAT_INTERVAL - timedelta(microseconds=1)) == (
        "verified"
    )


def test_attestation_session_verified_one_microsecond_before_each_boundary() -> None:
    assert (
        _session(tpm_heartbeat_at=NOW - TPM_HEARTBEAT_INTERVAL).status(
            NOW - timedelta(microseconds=1)
        )
        == "verified"
    )
    assert (
        _session(gpu_reattest_at=NOW - GPU_REATTEST_INTERVAL).status(
            NOW - timedelta(microseconds=1)
        )
        == "verified"
    )
    assert (
        _session(started_at=NOW - SESSION_CAP).status(NOW - timedelta(microseconds=1))
        == "verified"
    )


def test_attestation_session_stale_when_tpm_heartbeat_lapses() -> None:
    session = _session(tpm_heartbeat_at=NOW - TPM_HEARTBEAT_INTERVAL)

    assert session.status(NOW) == "stale"


def test_attestation_session_stale_when_gpu_reattest_lapses() -> None:
    session = _session(gpu_reattest_at=NOW - GPU_REATTEST_INTERVAL)

    assert session.status(NOW) == "stale"


def test_attestation_session_stale_when_session_cap_lapses() -> None:
    session = _session(started_at=NOW - SESSION_CAP)

    assert session.status(NOW) == "stale"
