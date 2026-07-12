# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from solstone.convey import create_app
from solstone.think.services import spp
from solstone.think.services.spp_attest.cadence import AttestationSession
from solstone.think.services.spp_attest.composite import CompositeVerdict
from solstone.think.services.spp_attest.nvgpu.claims import GpuAppraisal
from solstone.think.services.spp_attest.snp import AppraisalStep, CpuAppraisal

ATTESTED_SUBSTRATE = "AMD SEV-SNP + NVIDIA GH100 A01 GSP BROM"
HOSTILE_ARCH = "<script>alert(1)</script>"


def _client(settings_env, *, confidential: bool = False):
    journal_path, config = settings_env()
    config["setup"] = {"completed_at": "2026-05-23T00:00:00Z"}
    if confidential:
        config.setdefault("services", {})["confidential"] = {
            "enabled_at": "2026-05-24T00:00:00Z",
            "account_id": "acct-secret",
            "endpoint_url": "https://spp.example.test/v1",
            "served_model_id": "confidential-model",
            "credential_created_at": "2026-05-24T00:00:00Z",
            "credential_fingerprint_sha256": "fingerprint-secret",
            "prior_generate_provider": "google",
            "prior_cogitate_provider": "openai",
            "prior_local_endpoint": None,
        }
        config.setdefault("providers", {})["local"] = {
            "endpoint_url": "https://spp.example.test",
            "served_model_id": "confidential-model",
            "credential": "credential-secret",
        }
    (journal_path / "config" / "journal.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    app = create_app(str(journal_path))
    app.config["TESTING"] = True
    return app.test_client()


def _session(
    *,
    now: datetime,
    started_at: datetime,
    tpm_heartbeat_at: datetime,
    gpu_reattest_at: datetime,
    substrate: str = ATTESTED_SUBSTRATE,
    arch: str = "HOPPER",
) -> AttestationSession:
    cpu = CpuAppraisal(
        steps=[AppraisalStep("cpu", "ok", "test")],
        hcla_version=2,
        report_version=5,
        cpuid={"family": 25},
        tcb={},
        pcr_sha256="pcr-machine-secret",
        host_data="host-data-secret",
        measurement="measurement-secret",
        chip_id="chip-machine-secret",
    )
    gpu = GpuAppraisal(
        steps=[AppraisalStep("gpu", "ok", "test")],
        driver_version="595.71.05",
        vbios_version="96.00.88.00.11",
        hwmodel="GH100 A01 GSP BROM",
        ueid="ueid-machine-secret",
        oemid="5703",
        eat_nonce="eat-nonce-secret",
        claims_version="3.0",
        arch=arch,
        envelope_gpu_uuid="GPU-machine-secret",
    )
    verdict = CompositeVerdict(
        verified=True,
        legs=("cpu", "gpu"),
        substrate=substrate,
        checked_at=now,
        cpu_provenance=cpu,
        gpu_provenance=gpu,
    )
    return AttestationSession(
        verdict=verdict,
        started_at=started_at,
        tpm_heartbeat_at=tpm_heartbeat_at,
        gpu_reattest_at=gpu_reattest_at,
    )


def _providers(client) -> dict:
    response = client.get("/app/thinking/api/providers")
    assert response.status_code == 200
    payload = response.get_json()
    assert isinstance(payload, dict)
    return payload


def test_active_lane_confidential_attestation_defaults_to_off(settings_env):
    client = _client(settings_env)

    payload = _providers(client)

    assert payload["active_lane"]["confidential_attestation"] == {
        "state": "off",
        "provenance": None,
        "last_verified": None,
        "reason": "confidential_not_configured",
    }


def test_active_lane_confidential_attestation_configured_without_session_verifies(
    settings_env,
):
    client = _client(settings_env, confidential=True)

    payload = _providers(client)

    assert payload["active_lane"]["confidential_attestation"] == {
        "state": "verifying",
        "provenance": None,
        "last_verified": None,
        "reason": "attestation_not_yet_verified",
    }


def test_active_lane_confidential_attestation_verified_session_serializes_safe_provenance(
    settings_env,
):
    client = _client(settings_env, confidential=True)
    now = datetime.now(timezone.utc)
    session = _session(
        now=now,
        started_at=now - timedelta(minutes=1),
        tpm_heartbeat_at=now - timedelta(minutes=1),
        gpu_reattest_at=now - timedelta(minutes=1),
    )
    spp.record_attestation_verified(session)

    response = client.get("/app/thinking/api/providers")
    assert response.status_code == 200
    payload = response.get_json()
    attestation = payload["active_lane"]["confidential_attestation"]

    assert attestation == {
        "state": "verified",
        "provenance": {
            "legs": ["cpu", "gpu"],
            "substrate": ATTESTED_SUBSTRATE,
            "checked_at": now.isoformat(),
        },
        "last_verified": {
            "legs": ["cpu", "gpu"],
            "substrate": ATTESTED_SUBSTRATE,
            "checked_at": now.isoformat(),
        },
        "reason": None,
    }
    serialized = response.get_data(as_text=True)
    for forbidden in {
        "chip-machine-secret",
        "ueid-machine-secret",
        "GPU-machine-secret",
        "host-data-secret",
        "measurement-secret",
        "pcr-machine-secret",
        "eat-nonce-secret",
    }:
        assert forbidden not in serialized


def test_active_lane_confidential_attestation_does_not_serialize_unverified_arch(
    settings_env,
):
    client = _client(settings_env, confidential=True)
    now = datetime.now(timezone.utc)
    session = _session(
        now=now,
        started_at=now - timedelta(minutes=1),
        tpm_heartbeat_at=now - timedelta(minutes=1),
        gpu_reattest_at=now - timedelta(minutes=1),
        arch=HOSTILE_ARCH,
    )
    spp.record_attestation_verified(session)

    response = client.get("/app/thinking/api/providers")
    assert response.status_code == 200
    payload = response.get_json()
    attestation = payload["active_lane"]["confidential_attestation"]

    assert attestation["provenance"]["substrate"] == ATTESTED_SUBSTRATE
    assert HOSTILE_ARCH not in response.get_data(as_text=True)


def test_active_lane_confidential_attestation_stale_session(settings_env):
    client = _client(settings_env, confidential=True)
    now = datetime.now(timezone.utc)
    session = _session(
        now=now,
        started_at=now - timedelta(hours=2),
        tpm_heartbeat_at=now,
        gpu_reattest_at=now,
    )
    spp.record_attestation_verified(session)

    payload = _providers(client)

    assert payload["active_lane"]["confidential_attestation"] == {
        "state": "stale",
        "provenance": None,
        "last_verified": {
            "legs": ["cpu", "gpu"],
            "substrate": ATTESTED_SUBSTRATE,
            "checked_at": now.isoformat(),
        },
        "reason": "attestation_stale",
    }


def test_active_lane_confidential_attestation_failed_state(settings_env):
    client = _client(settings_env, confidential=True)
    spp.record_attestation_failed("failed", "gpu_nonce_mismatch")

    payload = _providers(client)

    assert payload["active_lane"]["confidential_attestation"] == {
        "state": "failed",
        "provenance": None,
        "last_verified": None,
        "reason": "attestation_failed",
    }


def test_active_lane_confidential_attestation_unreachable_state_preserves_last_verified(
    settings_env,
):
    client = _client(settings_env, confidential=True)
    now = datetime.now(timezone.utc)
    session = _session(
        now=now,
        started_at=now - timedelta(minutes=1),
        tpm_heartbeat_at=now - timedelta(minutes=1),
        gpu_reattest_at=now - timedelta(minutes=1),
    )
    spp.record_attestation_verified(session)
    spp.record_attestation_failed("unreachable", "gateway_unreachable")

    payload = _providers(client)

    assert payload["active_lane"]["confidential_attestation"] == {
        "state": "unreachable",
        "provenance": None,
        "last_verified": {
            "legs": ["cpu", "gpu"],
            "substrate": ATTESTED_SUBSTRATE,
            "checked_at": now.isoformat(),
        },
        "reason": "attestation_unreachable",
    }


def test_active_lane_confidential_attestation_missing_provenance_degrades_to_failed(
    settings_env,
):
    client = _client(settings_env, confidential=True)
    now = datetime.now(timezone.utc)
    session = _session(
        now=now,
        started_at=now - timedelta(minutes=1),
        tpm_heartbeat_at=now - timedelta(minutes=1),
        gpu_reattest_at=now - timedelta(minutes=1),
        substrate="",
    )
    spp.record_attestation_verified(session)

    payload = _providers(client)

    assert payload["active_lane"]["confidential_attestation"] == {
        "state": "failed",
        "provenance": None,
        "last_verified": None,
        "reason": "attestation_failed",
    }
