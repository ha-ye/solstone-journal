# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from solstone.think.cogitate_contract import (
    COGITATE_DIAGNOSTIC_PREAMBLE,
    capabilities_for_access_tier,
)
from solstone.think.cogitate_policy import (
    DEFAULT_RUN_COST_CAP_USD,
    MAX_TURNS,
)
from solstone.think.providers import emit_final_tool, openhands
from solstone.think.providers.shared import CANNED_GENERATE_NUM_RETRIES
from tests.openhands_fakes import _REGISTERED_TOOLS, install_fake_openhands


def _run_config(monkeypatch, tmp_path, **overrides):
    monkeypatch.setattr(openhands, "get_journal", lambda: tmp_path)
    monkeypatch.setattr(openhands, "get_project_root", lambda: tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = {
        "provider": "openai",
        "model": "gpt-5",
        "prompt": "Run the bounded diagnostic.",
        "session_id": "diagnostic-session",
        "day": "20260720",
        "diagnostic": True,
        "max_turns": 2,
        "max_run_cost_usd": 0.05,
        "timeout_seconds": 60,
    }
    config.update(overrides)
    return config


def _emit_final_action(fake_openhands, content: str):
    return fake_openhands.ActionEvent(
        reasoning_content=None,
        thinking_blocks=[],
        responses_reasoning_item=None,
        tool_name="emit_final",
        tool_call=SimpleNamespace(arguments={"content": content}),
        tool_call_id="emit-1",
        action=SimpleNamespace(content=content),
    )


def _install_emit_final_arun(fake_openhands, content: str) -> None:
    async def emit_final(conversation):
        for callback in conversation.callbacks:
            callback(_emit_final_action(fake_openhands, content))

    fake_openhands.Conversation.arun_impl = emit_final


def test_diagnostic_access_tier_caps_disable_all_journal_tools():
    caps = capabilities_for_access_tier("diagnostic")

    assert (caps.sol, caps.reads, caps.submit) == (False, False, False)


def test_diagnostic_helpers_select_bounds_and_zero_retries():
    config = {
        "diagnostic": True,
        "access_tier": "normal",
        "max_turns": 2,
        "max_run_cost_usd": 0.05,
        "timeout_seconds": 60,
    }

    assert openhands._effective_access_tier(config) == "diagnostic"
    assert openhands._llm_num_retries(config) == CANNED_GENERATE_NUM_RETRIES
    assert openhands._llm_num_retries({}) == openhands.LLM_NUM_RETRIES
    assert openhands._cogitate_budgets(config) == (2, 0.05, 60.0)
    assert openhands._cogitate_budgets({}) == (
        MAX_TURNS,
        DEFAULT_RUN_COST_CAP_USD,
        600.0,
    )


def test_diagnostic_run_registers_only_emit_final_and_writes_no_files(
    monkeypatch,
    tmp_path,
):
    fake_openhands = install_fake_openhands(monkeypatch)
    emit_final_tool._EMIT_FINAL_TYPES.clear()
    _REGISTERED_TOOLS.clear()
    _install_emit_final_arun(fake_openhands, "diagnostic ok")
    config = _run_config(monkeypatch, tmp_path)
    events: list[dict] = []
    created_temp_dirs: list[Path] = []

    real_tempdir = openhands.tempfile.TemporaryDirectory

    def tracking_tempdir(*args, **kwargs):
        kwargs.setdefault("dir", tmp_path.parent)
        tempdir = real_tempdir(*args, **kwargs)
        created_temp_dirs.append(Path(tempdir.name))
        return tempdir

    monkeypatch.setattr(openhands.tempfile, "TemporaryDirectory", tracking_tempdir)

    before_tree = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    result = asyncio.run(openhands.run_cogitate(config, events.append))

    conversation = fake_openhands.Conversation.instances[0]
    agent_tool_names = {tool.name for tool in conversation.agent.tools}
    after_tree = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    assert result == "diagnostic ok"
    assert agent_tool_names == {"emit_final"}
    assert set(_REGISTERED_TOOLS) == {"emit_final"}
    assert conversation.agent.include_default_tools == []
    assert conversation.agent.llm.num_retries == CANNED_GENERATE_NUM_RETRIES
    assert conversation.max_iteration_per_run == 4
    assert conversation.agent.system_prompt.startswith(
        COGITATE_DIAGNOSTIC_PREAMBLE.rstrip("\n")
    )
    assert "emit_final" in conversation.agent.system_prompt
    assert "read_file" not in conversation.agent.system_prompt
    assert "list_directory" not in conversation.agent.system_prompt
    assert "grep_search" not in conversation.agent.system_prompt
    assert "through the `sol` tool" not in conversation.agent.system_prompt
    assert "sol call" not in conversation.agent.system_prompt
    assert before_tree == set()
    assert after_tree == set()
    assert created_temp_dirs
    assert not any(path.exists() for path in created_temp_dirs)
    assert [event["event"] for event in events] == ["finish"]
    assert events[0]["result"] == "diagnostic ok"
