# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest

from solstone.think.sandbox_profile import envelope, manifest
from tests.sandbox_profile import invoke, output_json, prepare_ok, sandbox_journal


def test_envelope_field_order_types_and_default_json_output(
    tmp_path, monkeypatch
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)

    result = invoke(["describe", "--profile", "full", "--contract-version", "1"])
    raw = json.loads(result.output)

    assert result.exit_code == 0
    assert list(raw) == [
        "contract_version",
        "action",
        "profile",
        "run_id",
        "state",
        "capabilities",
        "next_actions",
        "error",
    ]
    assert raw["contract_version"] == 1
    assert raw["action"] == "describe"
    assert raw["profile"] == "full"
    assert raw["run_id"] is None
    assert [cap["name"] for cap in raw["capabilities"]] == [
        "scout",
        "spl",
        "spb",
        "spp",
        "runtime",
    ]
    assert raw["error"] is None
    assert not (journal / "config" / "journal.json").exists()


def test_exit_mapping_distinguishes_internal_failure_from_known_outcomes() -> None:
    caps = envelope.empty_capabilities()
    assert envelope.Envelope("status", "full", None, "ok", caps).exit_code == 0
    assert envelope.Envelope("status", "full", None, "degraded", caps).exit_code == 3
    assert (
        envelope.Envelope("status", "full", None, "cleanup_failed", caps).exit_code == 3
    )
    assert (
        envelope.error_envelope(
            action="status",
            code="payload_invalid",
            message="invalid",
            run_id=None,
        ).exit_code
        == 2
    )
    assert (
        envelope.error_envelope(
            action="status",
            code="internal_error",
            message="failed",
            run_id=None,
        ).exit_code
        == 1
    )


def test_capability_serializer_enforces_closed_vocabulary() -> None:
    with pytest.raises(ValueError, match="unsupported capability name"):
        envelope.CapabilityEnvelope("unknown", envelope.CAP_READY).to_json()
    with pytest.raises(ValueError, match="unsupported residual code"):
        envelope.CapabilityEnvelope(
            manifest.CAPABILITY_SCOUT,
            envelope.CAP_DEGRADED,
            ("typo_residual",),
        ).to_json()


def test_cli_exit_codes_cover_ok_degraded_error_and_cleanup_failed(
    tmp_path, monkeypatch
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    ok = invoke(["status", "--json"])
    prepare_ok(journal)
    error = invoke(["apply", "runtime", "--json"], input_text="{}")
    intent_path = journal / "health" / "sandbox-profile" / "intent.json"
    payload = json.loads(intent_path.read_text("utf-8"))
    for cap in payload["capabilities"]:
        if cap["name"] == "spb":
            cap["intent_state"] = "applied"
    intent_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    degraded = invoke(["status", "--json"])
    cleanup = invoke(["disable", "spb", "--json"])

    assert ok.exit_code == 0
    assert degraded.exit_code == 3
    assert error.exit_code == 2
    assert cleanup.exit_code == 3
    assert output_json(cleanup)["state"] == "cleanup_failed"
