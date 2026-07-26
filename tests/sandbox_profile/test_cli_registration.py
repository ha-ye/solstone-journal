# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.think import sol_cli
from solstone.think.generated.access_rejections import JOURNAL_ACCESS_ONLY_COMMANDS
from solstone.think.sandbox_profile import cli
from tests.sandbox_profile import invoke, output_json, sandbox_journal


def test_sandbox_profile_registered_as_service_command() -> None:
    command = sol_cli.COMMANDS["sandbox-profile"]

    assert command.module == "solstone.think.sandbox_profile.cli"
    assert command.surface == "service"
    assert "sandbox-profile" in sol_cli.service_help_group().commands


def test_sandbox_profile_module_exposes_main() -> None:
    assert callable(cli.main)


def test_sandbox_profile_not_a_journal_access_rejection() -> None:
    assert "sandbox-profile" not in JOURNAL_ACCESS_ONLY_COMMANDS


def test_apply_runtime_refuses_with_supported_capability_list(
    tmp_path,
    monkeypatch,
) -> None:
    sandbox_journal(tmp_path, monkeypatch)

    result = invoke(["apply", "runtime", "--json"], input_text="{}")
    body = output_json(result)

    assert result.exit_code == 2
    assert body["error"]["code"] == "unsupported_capability_action"
    assert "scout, spl, spb, spp" in body["next_actions"][0]


def test_apply_unknown_capability_refuses(tmp_path, monkeypatch) -> None:
    sandbox_journal(tmp_path, monkeypatch)

    result = invoke(["apply", "missing", "--json"], input_text="{}")
    body = output_json(result)

    assert result.exit_code == 2
    assert body["error"]["code"] == "unknown_capability"


def test_describe_is_marker_free_preflight(tmp_path, monkeypatch) -> None:
    journal = tmp_path / "unmarked"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    result = invoke(["describe", "--json"])
    body = output_json(result)

    assert result.exit_code == 0
    assert body["run_id"] is None
    assert body["state"] == "ok"
    assert not (journal / ".solstone-sandbox.json").exists()
