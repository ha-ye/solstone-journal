# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest

from tests.sandbox_profile import (
    invoke,
    output_json,
    prepare_ok,
    read_json,
    sandbox_journal,
    scout_payload,
    spb_payload,
    spp_payload,
)


def test_local_apply_capabilities_compose_existing_owner_functions(
    tmp_path,
    monkeypatch,
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)

    scout = invoke(["apply", "scout", "--json"], input_text=json.dumps(scout_payload()))
    spb = invoke(
        ["apply", "spb", "--json"], input_text=json.dumps(spb_payload(journal))
    )
    spp = invoke(["apply", "spp", "--json"], input_text=json.dumps(spp_payload()))

    assert scout.exit_code == 0
    assert spb.exit_code == 0
    assert spp.exit_code == 0
    config = read_json(journal / "config" / "journal.json")
    assert config["env"]["GOOGLE_API_KEY"] == "fake-google-key"
    assert config["services"]["scout"]["key_fingerprint_sha256"]
    assert config["backup"]["mode"] == "operated"
    assert config["backup"]["enabled"] is True
    assert (
        journal / "backup" / "hosted" / "binding.json"
    ).stat().st_mode & 0o777 == 0o600
    assert config["services"]["confidential"]["credential_fingerprint_sha256"]


@pytest.mark.parametrize(
    ("capability", "payload_factory", "residual"),
    [
        ("scout", lambda journal: scout_payload(), "scout_block_missing"),
        ("spb", spb_payload, "spb_binding_missing"),
        ("spp", lambda journal: spp_payload(), "spp_block_missing"),
    ],
)
@pytest.mark.parametrize("fail_on_replace", [1, 2])
def test_apply_atomic_faults_are_intent_first_and_status_names_partial_state(
    tmp_path,
    monkeypatch,
    capability: str,
    payload_factory,
    residual: str,
    fail_on_replace: int,
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    real_replace = __import__("os").replace
    calls = {"count": 0}

    def flaky_replace(src, dst):
        calls["count"] += 1
        if calls["count"] == fail_on_replace:
            raise OSError("forced commit failure")
        real_replace(src, dst)

    monkeypatch.setattr("solstone.think.journal_io.atomic.os.replace", flaky_replace)

    result = invoke(
        ["apply", capability, "--json"],
        input_text=json.dumps(payload_factory(journal)),
    )
    status = invoke(["status", "--json"])
    status_body = output_json(status)
    cap_status = next(
        cap for cap in status_body["capabilities"] if cap["name"] == capability
    )

    assert result.exit_code == 2
    if fail_on_replace == 1:
        assert cap_status["state"] == "not_applied"
    else:
        assert status.exit_code == 1
        assert cap_status["state"] == "degraded"
        assert residual in cap_status["residuals"]
