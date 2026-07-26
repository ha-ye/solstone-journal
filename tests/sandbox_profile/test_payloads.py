# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

import pytest

from tests.sandbox_profile import (
    invoke,
    output_json,
    prepare_ok,
    sandbox_journal,
    scout_payload,
    spb_payload,
    spl_payload,
    spp_payload,
)


@pytest.mark.parametrize(
    ("capability", "payload"),
    [
        ("scout", {**scout_payload(), "extra": "nope"}),
        ("spl", {**spl_payload(), "service": "wrong"}),
        ("spp", spp_payload(endpoint_url="not-a-url")),
    ],
)
def test_payload_matrix_rejects_invalid_payloads_before_apply(
    tmp_path,
    monkeypatch,
    capability: str,
    payload: dict[str, object],
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)

    result = invoke(["apply", capability, "--json"], input_text=json.dumps(payload))
    body = output_json(result)

    assert result.exit_code == 2
    assert body["error"]["code"] == "payload_invalid"
    intent_payload = json.loads(
        (journal / "health" / "sandbox-profile" / "intent.json").read_text("utf-8")
    )
    cap = next(
        item for item in intent_payload["capabilities"] if item["name"] == capability
    )
    assert cap["intent_state"] == "prepared"


def test_spb_rejects_instance_id_mismatch_before_local_mutation(
    tmp_path,
    monkeypatch,
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    payload = spb_payload(journal, instance_id="not-the-runtime-instance")

    result = invoke(["apply", "spb", "--json"], input_text=json.dumps(payload))
    body = output_json(result)

    assert result.exit_code == 2
    assert body["error"]["code"] == "spb_instance_mismatch"
    assert "instance_id" in body["error"]["message"]
    assert "not-the-runtime-instance" not in result.output
    assert not (journal / "backup" / "hosted" / "binding.json").exists()


@pytest.mark.parametrize("payload_text", ["[]", '{"a": 1} trailing', '{"a":1,"a":2}'])
def test_apply_rejects_non_object_trailing_and_duplicate_key_payloads(
    tmp_path,
    monkeypatch,
    payload_text: str,
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)

    result = invoke(["apply", "scout", "--json"], input_text=payload_text)
    body = output_json(result)

    assert result.exit_code == 2
    assert body["error"]["code"] == "payload_invalid"


def test_apply_rejects_oversized_payload(tmp_path, monkeypatch) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)

    result = invoke(["apply", "scout", "--json"], input_text="x" * (64 * 1024 + 1))
    body = output_json(result)

    assert result.exit_code == 2
    assert body["error"]["code"] == "payload_invalid"


def test_apply_runtime_and_unknown_capability_name_supported_apply_list(
    tmp_path,
    monkeypatch,
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)

    runtime = invoke(["apply", "runtime", "--json"], input_text="{}")
    unknown = invoke(["apply", "missing-cap", "--json"], input_text="{}")

    assert runtime.exit_code == 2
    assert output_json(runtime)["error"]["code"] == "unsupported_capability_action"
    assert "scout, spl, spb, spp" in output_json(runtime)["next_actions"][0]
    assert unknown.exit_code == 2
    assert output_json(unknown)["error"]["code"] == "unknown_capability"


def test_spl_apply_uses_consuming_module_enroll_home(monkeypatch, tmp_path) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    calls = []

    def fake_enroll_home(*args, **kwargs):
        calls.append((args, kwargs))
        return "service-token"

    monkeypatch.setattr("solstone.think.services.spl.enroll_home", fake_enroll_home)

    result = invoke(["apply", "spl", "--json"], input_text=json.dumps(spl_payload()))
    body = output_json(result)

    assert result.exit_code == 0
    assert calls
    assert (
        next(cap for cap in body["capabilities"] if cap["name"] == "spl")["state"]
        == "ready"
    )
