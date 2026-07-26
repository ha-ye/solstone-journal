# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from solstone.think.sandbox_profile import intent, manifest
from tests._repo_inventory import assert_inventory_unchanged, repository_inventory
from tests.sandbox_profile import (
    OTHER_RUN_ID,
    RUN_ID,
    invoke,
    output_json,
    sandbox_journal,
    write_marker,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _assert_refusal_without_repo_writes(monkeypatch, journal: Path, code: str) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal.resolve()))
    before = repository_inventory(REPO_ROOT)
    result = invoke(["prepare", "--json"])
    after = repository_inventory(REPO_ROOT)

    assert_inventory_unchanged(before, after)
    assert result.exit_code == 2
    payload = output_json(result)
    assert payload["state"] == "error"
    assert payload["run_id"] is None
    assert payload["error"]["code"] == code


def test_missing_marker_refuses_before_mkdir_or_repo_write(
    tmp_path, monkeypatch
) -> None:
    journal = tmp_path / "missing" / "journal"
    _assert_refusal_without_repo_writes(monkeypatch, journal, "sandbox_marker_missing")
    assert not journal.exists()


def test_marker_refusal_matrix_zero_side_effects(tmp_path, monkeypatch) -> None:
    cases = [
        ("symlink", "sandbox_marker_symlink"),
        ("not_regular", "sandbox_marker_not_regular"),
        ("unparseable", "sandbox_marker_unparseable"),
        ("non_object", "sandbox_marker_non_object"),
        ("wrong_kind", "sandbox_marker_wrong_kind"),
        ("wrong_contract", "sandbox_marker_wrong_contract_version"),
        ("wrong_profile", "sandbox_marker_wrong_profile"),
        ("bad_run_id", "sandbox_marker_bad_run_id"),
        ("path_mismatch", "sandbox_marker_path_mismatch"),
    ]
    for name, code in cases:
        journal = tmp_path / name
        journal.mkdir()
        marker_path = journal / ".solstone-sandbox.json"
        payload = {
            "kind": manifest.MARKER_KIND,
            "contract_version": manifest.CONTRACT_VERSION,
            "profile": manifest.PROFILE,
            "run_id": RUN_ID,
            "journal_path": str(journal.resolve()),
        }
        if name == "symlink":
            target = journal / "target.json"
            target.write_text("{}", encoding="utf-8")
            marker_path.symlink_to(target)
        elif name == "not_regular":
            marker_path.mkdir()
        elif name == "unparseable":
            marker_path.write_text('{"kind": "x"} trailing', encoding="utf-8")
        elif name == "non_object":
            marker_path.write_text("[]\n", encoding="utf-8")
        else:
            if name == "wrong_kind":
                payload["kind"] = "wrong"
            elif name == "wrong_contract":
                payload["contract_version"] = 99
            elif name == "wrong_profile":
                payload["profile"] = "other"
            elif name == "bad_run_id":
                payload["run_id"] = RUN_ID.upper()
            elif name == "path_mismatch":
                payload["journal_path"] = str((tmp_path / "other").resolve())
            marker_path.write_text(json.dumps(payload), encoding="utf-8")

        _assert_refusal_without_repo_writes(monkeypatch, journal, code)


def test_duplicate_key_marker_is_unparseable(tmp_path, monkeypatch) -> None:
    journal = tmp_path / "journal"
    journal.mkdir()
    (journal / ".solstone-sandbox.json").write_text(
        (
            '{"kind":"solstone-disposable-journal","kind":"x",'
            '"contract_version":1,"profile":"full",'
            f'"run_id":"{RUN_ID}","journal_path":"{journal.resolve()}"}}'
        ),
        encoding="utf-8",
    )
    _assert_refusal_without_repo_writes(
        monkeypatch, journal, "sandbox_marker_unparseable"
    )


def test_valid_marker_with_different_intent_run_is_intent_conflict(
    tmp_path,
    monkeypatch,
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    intent.ensure_prepared(journal, OTHER_RUN_ID)

    result = invoke(["prepare", "--json"])
    payload = output_json(result)

    assert result.exit_code == 2
    assert payload["run_id"] == RUN_ID
    assert payload["error"]["code"] == "intent_run_mismatch"
    assert "owning run" in payload["next_actions"][0]


def test_describe_rejects_unsupported_options_before_marker_validation(
    tmp_path,
    monkeypatch,
) -> None:
    journal = tmp_path / "uncreated"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    result = invoke(["describe", "--profile", "other", "--json"])
    payload = output_json(result)

    assert result.exit_code == 2
    assert payload["error"]["code"] == "sandbox_marker_wrong_profile"
    assert not journal.exists()


@pytest.mark.parametrize("command", ["apply", "disable"])
def test_mutating_commands_refuse_missing_marker_with_null_run_id(
    tmp_path,
    monkeypatch,
    command: str,
) -> None:
    journal = tmp_path / command / "journal"
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    args = [command, "scout", "--json"] if command == "apply" else [command, "--json"]
    result = invoke(args, input_text="{}")
    payload = output_json(result)

    assert result.exit_code == 2
    assert payload["run_id"] is None
    assert payload["error"]["code"] == "sandbox_marker_missing"
    assert not journal.exists()


def test_marker_validation_does_not_inject_config_env(tmp_path, monkeypatch) -> None:
    journal = tmp_path / "journal"
    write_marker(journal)
    config_path = journal / "config" / "journal.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps({"env": {"GOOGLE_API_KEY": "should-not-enter-env"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal.resolve()))
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    result = invoke(["status", "--json"])

    assert result.exit_code == 0
    assert os.environ.get("GOOGLE_API_KEY") is None
