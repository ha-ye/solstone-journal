# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from pathlib import Path


def test_root_agents_md_is_hand_maintained():
    project_root = Path(__file__).resolve().parent.parent
    agents_path = project_root / "AGENTS.md"
    content = agents_path.read_text(encoding="utf-8")

    assert content.startswith("# solstone Developer Guide")
    assert "generated from identity/identity.md" not in content
    assert "docs/project-structure.md" in content
    assert "journal/AGENTS.md" in content


def test_root_agent_symlinks_point_to_agents():
    project_root = Path(__file__).resolve().parent.parent
    claude_path = project_root / "CLAUDE.md"
    gemini_path = project_root / "GEMINI.md"

    assert claude_path.is_symlink()
    assert gemini_path.is_symlink()
    assert claude_path.readlink() == Path("AGENTS.md")
    assert gemini_path.readlink() == Path("AGENTS.md")


def test_generation_params_thinking_budget_zero_disables_thinking():
    """An explicit thinking_budget=0 must pass through (disable thinking), not
    coalesce to the default — regression for the `or 8192*2` bug that turned 0
    into 16384, making thinking impossible to disable from talent config."""
    from solstone.think.talents import _generation_params

    # explicit 0 -> 0 (disabled), not the default
    assert _generation_params({"thinking_budget": 0})["thinking_budget"] == 0
    # unset -> default
    assert _generation_params({})["thinking_budget"] == 8192 * 2
    # explicit None -> default
    assert _generation_params({"thinking_budget": None})["thinking_budget"] == 8192 * 2
    # explicit positive value -> passes through unchanged
    assert _generation_params({"thinking_budget": 4096})["thinking_budget"] == 4096


def test_json_extraction_talents_pin_output_cap_and_timeout():
    from solstone.think.talent import get_talent
    from solstone.think.talents import _generation_params

    largest_observed_legitimate_completion = 3560

    sense_config = get_talent("sense")
    sense_params = _generation_params(sense_config)
    assert sense_params["max_output_tokens"] == 6144
    assert sense_config.get("timeout_s") == 480
    assert "temperature" not in sense_config

    participation_config = get_talent("participation")
    participation_params = _generation_params(participation_config)
    participation_tokens = participation_params["max_output_tokens"]
    # Participation did not change and still keeps 2x headroom over the
    # largest legitimate completion observed when this guard was added.
    assert participation_tokens == 12288
    assert participation_tokens >= 2 * largest_observed_legitimate_completion
    assert participation_tokens < 8192 * 6
    assert participation_config.get("timeout_s") == 480
    resolved = participation_config.get("timeout_s") or min(
        480,
        max(
            120,
            (participation_tokens + participation_params["thinking_budget"]) // 100,
        ),
    )
    assert resolved == participation_config["timeout_s"]
    assert resolved >= 480
    assert "temperature" not in participation_config
