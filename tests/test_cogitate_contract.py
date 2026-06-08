# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from solstone.think.cogitate_contract import (
    COGITATE_ACCESS_TIERS,
    COGITATE_RUNTIME_PREAMBLE,
    FUTURE_ACCESS_TIERS,
    TALENT_FINALIZATION_MODES,
)
from solstone.think.providers.cli import assemble_prompt


def test_cogitate_preamble_injected_with_and_without_system_instruction():
    _, system = assemble_prompt({"system_instruction": "X"}, sol_tool_name="sol")
    assert system is not None
    assert system.startswith(COGITATE_RUNTIME_PREAMBLE)

    _, system = assemble_prompt({}, sol_tool_name="sol")
    assert system is not None
    assert system.startswith(COGITATE_RUNTIME_PREAMBLE)


def test_cogitate_preamble_ordering_with_scope_hint():
    _, system = assemble_prompt(
        {"system_instruction": "X", "read_scope": ["c"]},
        sol_tool_name="sol",
    )

    assert system is not None
    assert system.startswith(COGITATE_RUNTIME_PREAMBLE)
    assert (
        system.index(COGITATE_RUNTIME_PREAMBLE.rstrip("\n"))
        < system.index("X")
        < system.index("through the `sol` tool")
        < system.index("Limit filesystem reads to today's segment dir")
    )


def test_non_cogitate_prompt_omits_preamble():
    _, system = assemble_prompt({"system_instruction": "X"}, sol_tool_name=None)

    assert system == "X"
    assert COGITATE_RUNTIME_PREAMBLE not in system


def test_prompt_body_unchanged_under_cogitate_injection():
    body, _ = assemble_prompt(
        {
            "transcript": "t",
            "extra_context": "e",
            "user_instruction": "u",
            "prompt": "p",
            "system_instruction": "X",
        },
        sol_tool_name="sol",
    )

    assert body == "t\n\ne\n\nu\n\np"


def test_cogitate_vocabulary_lock():
    assert COGITATE_ACCESS_TIERS == ("normal", "system-read", "outbound")
    assert FUTURE_ACCESS_TIERS == ("code-agent",)
    assert TALENT_FINALIZATION_MODES == ("emit_final", "FinishTool", "quiet")
    assert "repair" not in COGITATE_ACCESS_TIERS
    assert "repair" not in FUTURE_ACCESS_TIERS


def test_cogitate_runtime_preamble_content_guard():
    assert "sol call ..." in COGITATE_RUNTIME_PREAMBLE
    assert "journal root" in COGITATE_RUNTIME_PREAMBLE
    assert "node_modules" in COGITATE_RUNTIME_PREAMBLE
    assert "emit_final" in COGITATE_RUNTIME_PREAMBLE
    assert "finish tool" in COGITATE_RUNTIME_PREAMBLE
    assert "through a `sol` domain command" in COGITATE_RUNTIME_PREAMBLE
    assert "no MCP tools" in COGITATE_RUNTIME_PREAMBLE
    assert "no bare `journal ...` commands" in COGITATE_RUNTIME_PREAMBLE
