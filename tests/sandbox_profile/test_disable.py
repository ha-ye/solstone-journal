# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json

from solstone.think.services.scout import DisableOutcome
from tests.sandbox_profile import (
    invoke,
    output_json,
    prepare_ok,
    read_json,
    sandbox_journal,
    scout_payload,
    spb_payload,
)


def test_disable_is_idempotent_and_leaves_runtime_state(tmp_path, monkeypatch) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)

    first = invoke(["disable", "--json"])
    second = invoke(["disable", "--json"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert (journal / "link" / "state.json").exists()
    assert (journal / "link" / "ca" / "cert.pem").exists()
    assert "production-side reconciler" in output_json(first)["next_actions"][0]


def test_disable_missing_applied_spb_binding_reports_cleanup_failed(
    tmp_path,
    monkeypatch,
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    apply = invoke(
        ["apply", "spb", "--json"], input_text=json.dumps(spb_payload(journal))
    )
    assert apply.exit_code == 0
    (journal / "backup" / "hosted" / "binding.json").unlink()

    result = invoke(["disable", "--json"])
    body = output_json(result)
    spb = next(cap for cap in body["capabilities"] if cap["name"] == "spb")

    assert result.exit_code == 3
    assert body["state"] == "cleanup_failed"
    assert spb["state"] == "cleanup_failed"
    assert "spb_binding_missing" in spb["residuals"]
    assert "production-side reconciler" in body["next_actions"][0]


def test_clean_disable_after_spb_restore_converges_twice(tmp_path, monkeypatch) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    apply = invoke(
        ["apply", "spb", "--json"], input_text=json.dumps(spb_payload(journal))
    )
    assert apply.exit_code == 0
    binding = read_json(journal / "backup" / "hosted" / "binding.json")
    (journal / "backup" / "hosted" / "binding.json").unlink()
    failed = invoke(["disable", "--json"])
    assert failed.exit_code == 3
    (journal / "backup" / "hosted" / "binding.json").write_text(
        json.dumps(binding, indent=2) + "\n",
        encoding="utf-8",
    )

    clean = invoke(["disable", "--json"])
    again = invoke(["disable", "--json"])

    assert clean.exit_code == 0
    assert again.exit_code == 0
    assert not (journal / "backup" / "hosted" / "binding.json").exists()


def test_disable_still_applied_capability_is_cleanup_failed(
    tmp_path,
    monkeypatch,
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    apply = invoke(["apply", "scout", "--json"], input_text=json.dumps(scout_payload()))
    assert apply.exit_code == 0
    monkeypatch.setattr(
        "solstone.think.services.scout.disable_scout",
        lambda: DisableOutcome(was_enabled=False, env_key_preserved=False),
    )

    result = invoke(["disable", "scout", "--json"])
    body = output_json(result)
    scout = next(cap for cap in body["capabilities"] if cap["name"] == "scout")

    assert result.exit_code == 3
    assert body["state"] == "cleanup_failed"
    assert scout["state"] == "cleanup_failed"
    assert "cleanup_still_applied" in scout["residuals"]


def test_disable_logs_and_classifies_owner_io_failures(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    apply = invoke(
        ["apply", "spb", "--json"], input_text=json.dumps(spb_payload(journal))
    )
    assert apply.exit_code == 0

    def fail_clear_backup_config() -> None:
        raise OSError("forced local artifact failure")

    monkeypatch.setattr(
        "solstone.think.backup.state.clear_backup_config",
        fail_clear_backup_config,
    )

    result = invoke(["disable", "spb", "--json"])
    body = output_json(result)
    spb = next(cap for cap in body["capabilities"] if cap["name"] == "spb")

    assert result.exit_code == 3
    assert "local_artifact_io_failed" in spb["residuals"]
    assert "capability=spb" in caplog.text
    assert "exception_type=OSError" in caplog.text
    assert "broker-token" not in caplog.text
