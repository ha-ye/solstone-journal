# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from solstone.think.sandbox_profile import manifest
from tests.sandbox_profile import invoke, output_json, read_json, sandbox_journal


def test_prepare_is_idempotent_and_synthetic_identity_converges(
    tmp_path, monkeypatch
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)

    first = invoke(["prepare", "--json"])
    second = invoke(["prepare", "--json"])
    first_body = output_json(first)
    second_body = output_json(second)
    config = read_json(journal / "config" / "journal.json")
    owner = manifest.synthetic_owner_metadata(first_body["run_id"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first_body == second_body
    assert config["setup"]["completed_at"] == owner.setup_completed_at
    assert config["identity"]["name"] == owner.identity_name
    assert config["identity"]["preferred"] == owner.identity_preferred
    assert config["identity"]["timezone"] == owner.identity_timezone
    assert config["journal"]["name"] == owner.journal_name
    assert read_json(journal / "link" / "state.json")["home_label"] == owner.home_label
    assert (journal / "link" / "ca" / "cert.pem").exists()
    assert (journal / "link" / "ca" / "private.pem").exists()


def test_status_read_verb_does_not_materialize_missing_config(
    tmp_path, monkeypatch
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)

    result = invoke(["status", "--json"])
    body = output_json(result)

    assert result.exit_code == 0
    assert body["state"] == "ok"
    assert not (journal / "config" / "journal.json").exists()
    assert not (journal / "health" / "sandbox-profile" / "intent.json").exists()


def test_prepare_refuses_malformed_same_run_intent_before_side_effects(
    tmp_path, monkeypatch
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    intent_path = journal / "health" / "sandbox-profile" / "intent.json"
    intent_path.parent.mkdir(parents=True)
    intent_path.write_text(
        json.dumps(
            {
                "kind": "wrong",
                "contract_version": manifest.CONTRACT_VERSION,
                "run_id": "86d9eb6c-d64e-4ae5-b29e-524ddf57a013",
                "profile": manifest.PROFILE,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    before = intent_path.read_text("utf-8")

    result = invoke(["prepare", "--json"])
    body = output_json(result)

    assert result.exit_code == 2
    assert body["error"]["code"] == "intent_malformed"
    assert body["run_id"] == "86d9eb6c-d64e-4ae5-b29e-524ddf57a013"
    assert intent_path.read_text("utf-8") == before
    assert not (journal / "config" / "journal.json").exists()
    assert not (journal / "link").exists()
