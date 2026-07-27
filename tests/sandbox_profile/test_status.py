# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from tests.sandbox_profile import (
    invoke,
    output_json,
    prepare_ok,
    read_json,
    sandbox_journal,
    scout_payload,
    spb_payload,
    spl_payload,
    spp_payload,
)


def test_status_reports_intent_observed_split_without_network(
    tmp_path, monkeypatch
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    result = invoke(
        ["apply", "scout", "--json"], input_text=json.dumps(scout_payload())
    )
    assert result.exit_code == 0
    config_path = journal / "config" / "journal.json"
    config = json.loads(config_path.read_text("utf-8"))
    config["env"].pop("GOOGLE_API_KEY")
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(
        "solstone.think.services.spl.enroll_home",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")),
    )
    monkeypatch.setattr(
        "solstone.think.services.portal_client.build_consent_url",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("portal")),
    )
    monkeypatch.setattr(
        "solstone.think.backup.hosted.fetch_hosted_credentials",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("broker")),
    )

    status = invoke(["status", "--json"])
    body = output_json(status)
    scout = next(cap for cap in body["capabilities"] if cap["name"] == "scout")

    assert status.exit_code == 3
    assert body["state"] == "degraded"
    assert scout["state"] == "degraded"
    assert "scout_block_missing" in scout["residuals"]


def test_status_refuses_intent_run_mismatch_without_mutating(
    tmp_path, monkeypatch
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    intent_path = journal / "health" / "sandbox-profile" / "intent.json"
    payload = json.loads(intent_path.read_text("utf-8"))
    payload["run_id"] = "11111111-1111-4111-8111-111111111111"
    intent_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    result = invoke(["status", "--json"])
    body = output_json(result)

    assert result.exit_code == 2
    assert body["run_id"] == "86d9eb6c-d64e-4ae5-b29e-524ddf57a013"
    assert body["error"]["code"] == "intent_run_mismatch"


def _capability(body: dict[str, object], name: str) -> dict[str, object]:
    capabilities = body["capabilities"]
    assert isinstance(capabilities, list)
    return next(cap for cap in capabilities if cap["name"] == name)


def _assert_degraded_residual(result, name: str, residual: str) -> None:
    body = output_json(result)
    cap = _capability(body, name)
    assert result.exit_code == 3
    assert body["state"] == "degraded"
    assert cap["state"] == "degraded"
    assert residual in cap["residuals"]


def test_status_reconciles_scout_key_fingerprint(tmp_path, monkeypatch) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    result = invoke(
        ["apply", "scout", "--json"], input_text=json.dumps(scout_payload())
    )
    assert result.exit_code == 0
    config_path = journal / "config" / "journal.json"
    config = read_json(config_path)
    config["env"]["GOOGLE_API_KEY"] = "different-google-key"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    status = invoke(["status", "--json"])

    _assert_degraded_residual(status, "scout", "scout_key_fingerprint_mismatch")


def test_status_reconciles_scout_account_id(tmp_path, monkeypatch) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    result = invoke(
        ["apply", "scout", "--json"], input_text=json.dumps(scout_payload())
    )
    assert result.exit_code == 0
    config_path = journal / "config" / "journal.json"
    config = read_json(config_path)
    config["services"]["scout"]["account_id"] = "acct-mismatch"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    status = invoke(["status", "--json"])

    _assert_degraded_residual(status, "scout", "scout_account_id_mismatch")


def test_status_refuses_legacy_scout_intent_without_account_id(
    tmp_path, monkeypatch
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    result = invoke(
        ["apply", "scout", "--json"], input_text=json.dumps(scout_payload())
    )
    assert result.exit_code == 0
    intent_path = journal / "health" / "sandbox-profile" / "intent.json"
    payload = read_json(intent_path)
    payload["observed_at_apply"]["scout"].pop("account_id")
    intent_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    status = invoke(["status", "--json"])

    _assert_degraded_residual(status, "scout", "scout_account_id_mismatch")


def test_status_reconciles_spl_identity(tmp_path, monkeypatch) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    monkeypatch.setattr(
        "solstone.think.services.spl.enroll_home",
        lambda *args, **kwargs: "service-token",
    )
    result = invoke(["apply", "spl", "--json"], input_text=json.dumps(spl_payload()))
    assert result.exit_code == 0
    state_path = journal / "link" / "state.json"
    state = read_json(state_path)
    state["instance_id"] = "did:key:mismatched"
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    status = invoke(["status", "--json"])

    _assert_degraded_residual(status, "spl", "spl_identity_missing")


def test_status_reconciles_spb_binding_instance(tmp_path, monkeypatch) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    result = invoke(
        ["apply", "spb", "--json"], input_text=json.dumps(spb_payload(journal))
    )
    assert result.exit_code == 0
    binding_path = journal / "backup" / "hosted" / "binding.json"
    binding = read_json(binding_path)
    binding["instance_id"] = "did:key:mismatched"
    binding_path.write_text(json.dumps(binding, indent=2) + "\n", encoding="utf-8")

    status = invoke(["status", "--json"])

    _assert_degraded_residual(status, "spb", "spb_instance_mismatch")


def test_status_reconciles_spp_credential_fingerprint(tmp_path, monkeypatch) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    result = invoke(["apply", "spp", "--json"], input_text=json.dumps(spp_payload()))
    assert result.exit_code == 0
    config_path = journal / "config" / "journal.json"
    config = read_json(config_path)
    config["services"]["confidential"]["credential_fingerprint_sha256"] = (
        "mismatched-fingerprint"
    )
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    status = invoke(["status", "--json"])

    _assert_degraded_residual(status, "spp", "spp_credential_fingerprint_mismatch")
