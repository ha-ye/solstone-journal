# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from solstone.think.services import spp
from solstone.think.services.spp_attest.cadence import AttestationSession
from solstone.think.services.spp_attest.composite import CompositeVerdict
from solstone.think.services.spp_attest.nvgpu.claims import GpuAppraisal
from solstone.think.services.spp_attest.snp import AppraisalStep, CpuAppraisal

NOW = datetime(2026, 7, 11, 18, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clear_attestation_state():
    spp.delete_attestation_state()
    yield
    spp.delete_attestation_state()


def _session() -> AttestationSession:
    cpu = CpuAppraisal(
        steps=[AppraisalStep("cpu", "ok", "test")],
        hcla_version=2,
        report_version=5,
        cpuid={},
        tcb={},
        pcr_sha256="pcr",
        host_data="host",
        measurement="measurement",
        chip_id="chip",
    )
    gpu = GpuAppraisal(
        steps=[AppraisalStep("gpu", "ok", "test")],
        driver_version="595.71.05",
        vbios_version="96.00.88.00.11",
        hwmodel="GH100 A01 GSP BROM",
        ueid="ueid",
        oemid="5703",
        eat_nonce="nonce",
        claims_version="3.0",
        arch="HOPPER",
        envelope_gpu_uuid="GPU-test",
    )
    verdict = CompositeVerdict(
        verified=True,
        legs=("cpu", "gpu"),
        substrate="AMD SEV-SNP + NVIDIA HOPPER",
        checked_at=NOW,
        cpu_provenance=cpu,
        gpu_provenance=gpu,
    )
    return AttestationSession(
        verdict=verdict,
        started_at=NOW,
        tpm_heartbeat_at=NOW,
        gpu_reattest_at=NOW,
    )


def test_attestation_state_defaults_empty() -> None:
    state = spp.get_attestation_state()

    assert state.session is None
    assert state.failure is None
    assert state.last_verified is None


def test_record_attestation_verified_sets_session_and_clears_failure() -> None:
    spp.record_attestation_failed("failed", "prior")
    session = _session()

    spp.record_attestation_verified(session)

    state = spp.get_attestation_state()
    assert state.session is session
    assert state.failure is None
    assert state.last_verified is session


def test_record_attestation_failed_sets_failure_and_preserves_last_verified() -> None:
    session = _session()
    spp.record_attestation_verified(session)

    spp.record_attestation_failed("failed", "gpu_nonce_mismatch")

    state = spp.get_attestation_state()
    assert state.session is None
    assert state.failure is not None
    assert state.failure.kind == "failed"
    assert state.failure.reason_code == "gpu_nonce_mismatch"
    assert state.last_verified is session


def test_clear_attestation_state_preserves_last_verified() -> None:
    session = _session()
    spp.record_attestation_verified(session)
    spp.record_attestation_failed("unreachable", "gateway_unreachable")

    spp.clear_attestation_state()

    state = spp.get_attestation_state()
    assert state.session is None
    assert state.failure is None
    assert state.last_verified is session


def test_delete_attestation_state_resets_holder() -> None:
    spp.record_attestation_verified(_session())

    spp.delete_attestation_state()

    state = spp.get_attestation_state()
    assert state.session is None
    assert state.failure is None
    assert state.last_verified is None
