# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from solstone.think.sandbox_profile import manifest, probe_contract, probe_records

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_sandbox_probe_contract.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_sandbox_probe_contract", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_v1_vocabulary_is_pinned() -> None:
    assert probe_contract.PROOF_CHECKS == {
        "scout": (
            "scout.response_schema",
            "scout.nonce_match",
            "scout.finish",
            "scout.usage",
        ),
        "spl": (
            "spl.enrollment",
            "spl.relay_dial",
            "spl.inner_tls",
            "spl.observer_registered",
            "spl.segment_transferred",
            "spl.segment_landed",
            "spl.authorization_removed",
        ),
        "spb": (
            "spb.repository_initialized",
            "spb.snapshot_created",
            "spb.snapshot_confirmed",
            "spb.restore_match",
            "spb.local_cleanup",
        ),
        "spp": (
            "spp.attestation_session",
            "spp.text_nonce",
            "spp.text_usage",
            "spp.transcript_expected",
        ),
        "runtime": (
            "runtime.supervisor",
            "runtime.callosum",
            "runtime.listener",
            "runtime.sense",
            "runtime.task_queue",
            "runtime.cortex",
            "runtime.talent_output",
            "runtime.talent_usage",
            "runtime.cadence_contract",
            "runtime.cadence_dry_run",
        ),
    }
    assert probe_contract.PROOF_SPECIFIC_REASONS == {
        "scout": (
            "capability_not_ready",
            "content_mismatch",
            "deadline_exceeded",
            "remote_rejected",
            "response_invalid",
            "usage_invalid",
        ),
        "spl": (
            "capability_not_ready",
            "content_mismatch",
            "deadline_exceeded",
            "remote_rejected",
            "response_invalid",
        ),
        "spb": (
            "capability_not_ready",
            "content_mismatch",
            "deadline_exceeded",
            "remote_rejected",
            "response_invalid",
        ),
        "spp": (
            "attestation_unverified",
            "capability_not_ready",
            "content_mismatch",
            "deadline_exceeded",
            "remote_rejected",
            "response_invalid",
            "usage_invalid",
        ),
        "runtime": (
            "cadence_contract_mismatch",
            "capability_not_ready",
            "content_mismatch",
            "deadline_exceeded",
            "response_invalid",
            "runtime_unavailable",
            "usage_invalid",
        ),
    }


def test_probe_contract_agrees_with_manifest_today() -> None:
    assert probe_contract.CONTRACT_VERSION == manifest.CONTRACT_VERSION
    assert probe_contract.CAPABILITY_SCOUT == manifest.CAPABILITY_SCOUT
    assert probe_contract.CAPABILITY_SPL == manifest.CAPABILITY_SPL
    assert probe_contract.CAPABILITY_SPB == manifest.CAPABILITY_SPB
    assert probe_contract.CAPABILITY_SPP == manifest.CAPABILITY_SPP
    assert probe_contract.CAPABILITY_RUNTIME == manifest.CAPABILITY_RUNTIME
    assert probe_contract.CAPABILITY_ORDER == manifest.CAPABILITY_ORDER


def test_probe_contract_ignores_manifest_monkeypatch(monkeypatch) -> None:
    before = probe_contract.contract_payload()

    monkeypatch.setattr(manifest, "CONTRACT_VERSION", 999)
    monkeypatch.setattr(manifest, "CAPABILITY_SCOUT", "changed-scout")
    monkeypatch.setattr(
        manifest,
        "CAPABILITY_ORDER",
        ("changed-scout", "spl", "spb", "spp", "runtime"),
    )

    assert probe_contract.contract_payload() == before


def test_contract_payload_top_level_keys_are_pinned() -> None:
    assert set(probe_contract.contract_payload()) == {
        "attempt_terminal_reasons",
        "attempt_terminal_states",
        "cancellation",
        "capability_order",
        "cleanup_resolution",
        "cleanup_states",
        "common_reasons",
        "contract_version",
        "limits",
        "not_run_reasons",
        "predicate_keys",
        "proof_reason_pool",
        "proof_terminal_rules",
        "proof_terminal_states",
        "proofs",
        "record_cardinality",
        "record_fields",
        "record_types",
        "retry_eligible_terminals",
        "stable_errors",
        "terminal_derivation",
    }


def test_probe_predicate_registry_matches_contract() -> None:
    assert tuple(probe_records.PREDICATE_REGISTRY) == probe_contract.PREDICATE_KEYS


def test_contract_artifact_matches_constants() -> None:
    artifact = (
        REPO_ROOT / "solstone" / "think" / "sandbox_profile" / "probe_contract_v1.json"
    )
    actual = json.loads(artifact.read_text(encoding="utf-8"))
    assert actual == probe_contract.contract_payload()


def test_builder_check_is_bidirectional(monkeypatch, tmp_path, capsys) -> None:
    builder = _load_builder()
    artifact = (
        tmp_path / "solstone" / "think" / "sandbox_profile" / "probe_contract_v1.json"
    )
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "ARTIFACT_PATH", artifact)

    builder.write_outputs()
    assert builder.check_outputs() == 0

    artifact.write_text(
        artifact.read_text(encoding="utf-8").replace("{", "[", 1),
        encoding="utf-8",
    )
    assert builder.check_outputs() == 1
    captured = capsys.readouterr()
    assert (
        "Sandbox probe contract is stale: "
        "solstone/think/sandbox_profile/probe_contract_v1.json. "
        "Run: make sandbox-probe-contract"
    ) in captured.err
