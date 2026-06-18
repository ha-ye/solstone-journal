# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from pathlib import Path

import pytest

from solstone.think import cogitate_contract
from solstone.think.cogitate_contract import (
    COGITATE_ACCESS_TIERS,
    COGITATE_READ_TOOL_NAMES,
    COGITATE_RUNTIME_PREAMBLE,
    FUTURE_ACCESS_TIERS,
    TALENT_FINALIZATION_MODES,
    expects_emit_final,
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
    assert COGITATE_ACCESS_TIERS == ("normal", "system-read", "outbound", "synthesis")
    assert COGITATE_READ_TOOL_NAMES == (
        "read_file",
        "list_directory",
        "glob",
        "grep_search",
    )
    assert FUTURE_ACCESS_TIERS == ("code-agent",)
    assert TALENT_FINALIZATION_MODES == ("emit_final", "FinishTool", "quiet")
    assert "repair" not in COGITATE_ACCESS_TIERS
    assert "repair" not in FUTURE_ACCESS_TIERS


def test_access_tier_capability_mapping_matches_vocabulary():
    assert set(cogitate_contract._ACCESS_TIER_CAPABILITIES) == set(
        COGITATE_ACCESS_TIERS
    )


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"output_path": "/tmp/out.md"}, True),
        ({"schedule": "daily"}, True),
        ({"schedule": "weekly"}, True),
        ({"schedule": "activity"}, True),
        ({"schedule": "segment"}, False),
        ({"schedule": "none"}, False),
        ({}, False),
    ],
)
def test_expects_emit_final(config, expected):
    assert expects_emit_final(config) is expected


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("normal", (True, True, False)),
        ("system-read", (True, True, False)),
        ("outbound", (True, False, True)),
        ("synthesis", (True, False, False)),
    ],
)
def test_capabilities_for_access_tier_real_tiers(tier, expected):
    caps = cogitate_contract.capabilities_for_access_tier(tier)

    assert (caps.sol, caps.reads, caps.submit) == expected


def test_outbound_tier_has_no_read_tools():
    assert cogitate_contract.capabilities_for_access_tier("outbound").reads is False


@pytest.mark.parametrize("tier", ["repair", "code-agent", "bogus"])
def test_capabilities_for_access_tier_unknown_names_tier(tier):
    with pytest.raises(ValueError, match=tier):
        cogitate_contract.capabilities_for_access_tier(tier)


def test_cogitate_runtime_preamble_content_guard():
    assert "sol call ..." in COGITATE_RUNTIME_PREAMBLE
    assert "single parsed command-line invocation" in COGITATE_RUNTIME_PREAMBLE
    assert "journal root" in COGITATE_RUNTIME_PREAMBLE
    assert "node_modules" in COGITATE_RUNTIME_PREAMBLE
    assert "emit_final" in COGITATE_RUNTIME_PREAMBLE
    assert "finish tool" in COGITATE_RUNTIME_PREAMBLE
    assert "through a `sol` domain command" in COGITATE_RUNTIME_PREAMBLE
    assert "no MCP tools" in COGITATE_RUNTIME_PREAMBLE
    assert "no bare `journal ...` commands" in COGITATE_RUNTIME_PREAMBLE
    assert "no shell composition" in COGITATE_RUNTIME_PREAMBLE
    assert "read_file" in COGITATE_RUNTIME_PREAMBLE
    assert "list_directory" in COGITATE_RUNTIME_PREAMBLE
    assert "glob" in COGITATE_RUNTIME_PREAMBLE
    assert "grep_search" in COGITATE_RUNTIME_PREAMBLE
    assert "when this run provides a read tool" not in COGITATE_RUNTIME_PREAMBLE


def test_cogitate_doc_preamble_block_matches_source_constant():
    docs_path = Path(__file__).resolve().parents[1] / "docs" / "COGITATE.md"
    text = docs_path.read_text(encoding="utf-8")
    heading = "## The in-context preamble (named source constant)"
    _, heading_found, tail = text.partition(heading)
    assert heading_found
    _, verbatim_found, tail = tail.partition("verbatim text:")
    assert verbatim_found
    _, fence_found, tail = tail.partition("```\n")
    assert fence_found
    block, closing_fence, _tail = tail.partition("\n```")
    assert closing_fence

    assert block == COGITATE_RUNTIME_PREAMBLE.rstrip("\n")
