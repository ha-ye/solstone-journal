# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from tests.sandbox_profile import (
    invoke,
    output_json,
    prepare_ok,
    sandbox_journal,
    scout_payload,
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

    assert status.exit_code == 1
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
