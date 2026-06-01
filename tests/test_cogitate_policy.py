# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

from solstone.think import cogitate_policy


def test_resolve_read_scope_defaults_to_current_day_chronicle():
    assert cogitate_policy.resolve_read_scope({}, "20260427") == ["chronicle/20260427"]


def test_resolve_read_scope_expands_override_placeholders():
    assert cogitate_policy.resolve_read_scope(
        {"read_scope": ["chronicle/<day>", "chronicle/<day-2>", "facets"]},
        "20260427",
    ) == ["chronicle/20260427", "chronicle/20260425", "facets"]


def test_resolve_read_scope_span_is_inclusive():
    assert cogitate_policy.resolve_read_scope(
        {"read_scope_span": 2},
        "20260427",
    ) == ["chronicle/20260425", "chronicle/20260426", "chronicle/20260427"]


def test_policy_denies_write_tools(tmp_path):
    policy = cogitate_policy.CogitatePolicy(allowed_roots=[tmp_path])

    allowed, reason = policy.check("write_file", {"file_path": "x"})

    assert allowed is False
    assert reason.startswith("policy_deny:")


@pytest.mark.parametrize(
    "command",
    [
        "journal identity pulse",
        "journal identity awareness --write --value update",
        "journal routines list",
        "journal routines output morning",
        "journal health logs --since 1h",
        "journal talent logs --daily -c 10",
        "journal identity pulse --write --value 'a; quoted value'",
    ],
)
def test_policy_allows_approved_journal_invocations(tmp_path, command):
    policy = cogitate_policy.CogitatePolicy(allowed_roots=[tmp_path])

    allowed, reason = policy.check("run_shell_command", {"command": command})

    assert allowed is True
    assert reason == "ok"


@pytest.mark.parametrize(
    "command",
    [
        "journal think --segment",
        "journal navigate --path /app/support",
        "journal supervisor status",
        "journal indexer --rescan-full",
        "journal identity ; rm -rf journal",
        "journal identity pulse --value $(rm -rf journal)",
    ],
)
def test_policy_denies_unapproved_journal_invocations(tmp_path, command):
    policy = cogitate_policy.CogitatePolicy(allowed_roots=[tmp_path])

    allowed, reason = policy.check("run_shell_command", {"command": command})

    assert allowed is False
    assert reason.startswith("policy_deny:")


def test_cogitate_toml_removed_and_build_policy_import_fails():
    # AC 19: TOML policy generation is removed.
    policy_path = (
        Path(__file__).parents[1] / "solstone" / "think" / "policies" / "cogitate.toml"
    )
    assert not policy_path.exists()
    missing_symbol = "build" + "_per_task_policy"
    with pytest.raises(ImportError):
        exec(f"from solstone.think.cogitate_policy import {missing_symbol}", {})
