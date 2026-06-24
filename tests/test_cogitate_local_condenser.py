# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from openhands.sdk import LLM
from openhands.sdk.context.condenser import LLMSummarizingCondenser

from solstone.think.providers import openhands
from solstone.think.providers.local_server import LOCAL_MIN_CONTEXT_TOKENS


def _local_llm() -> LLM:
    return LLM(
        model="openai/local-model",
        base_url="http://127.0.0.1:9999/v1",
        api_key="EMPTY",
        native_tool_calling=False,
        max_input_tokens=LOCAL_MIN_CONTEXT_TOKENS,
        max_output_tokens=openhands._LOCAL_OUTPUT_RESERVE_TOKENS,
    )


def test_build_cogitate_agent_adds_bundled_local_condenser():
    agent = openhands._build_cogitate_agent(
        llm=_local_llm(),
        is_bundled_local=True,
        tool_specs=[],
        include_default_tools=[],
        system_prompt="sys",
    )

    assert isinstance(agent.condenser, LLMSummarizingCondenser)
    assert agent.condenser.max_tokens == openhands._LOCAL_CONDENSER_MAX_TOKENS
    assert agent.condenser.keep_first == openhands._LOCAL_CONDENSER_KEEP_FIRST
    assert agent.condenser.llm is agent.llm


def test_build_cogitate_agent_skips_condenser_for_non_bundled_local():
    agent = openhands._build_cogitate_agent(
        llm=_local_llm(),
        is_bundled_local=False,
        tool_specs=[],
        include_default_tools=[],
        system_prompt="sys",
    )

    assert agent.condenser is None


def test_local_condenser_window_invariants():
    assert (
        openhands._LOCAL_CONDENSER_MAX_TOKENS // 2
        + openhands._LOCAL_OUTPUT_RESERVE_TOKENS
        < LOCAL_MIN_CONTEXT_TOKENS
    )
    assert (
        openhands._LOCAL_CONDENSER_MAX_TOKENS + openhands._LOCAL_OUTPUT_RESERVE_TOKENS
        <= LOCAL_MIN_CONTEXT_TOKENS
    )
    assert openhands._LOCAL_CONDENSER_MAX_TOKENS < LOCAL_MIN_CONTEXT_TOKENS
    assert 11000 <= openhands._LOCAL_CONDENSER_MAX_TOKENS <= 11500
    assert openhands._LOCAL_CONDENSER_KEEP_FIRST < 240 // 2 - 1


def test_cogitate_context_ceiling_fractions():
    from solstone.think.cogitate_policy import CONTEXT_FINAL_FRAC, CONTEXT_WARN_FRAC

    assert CONTEXT_WARN_FRAC == 0.70
    assert CONTEXT_FINAL_FRAC == 0.78
    assert CONTEXT_WARN_FRAC < CONTEXT_FINAL_FRAC
