# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from solstone.think.cogitate_contract import AccessCapabilities
from solstone.think.cogitate_policy import (
    CONTEXT_WARN_FRAC,
    COST_WARN_FRAC,
    DEFAULT_RUN_COST_CAP_USD,
    MAX_TURNS,
    MAX_TURNS_HEADROOM,
)
from solstone.think.providers import openhands
from solstone.think.providers.shared import USAGE_KEYS, JSONEventCallback
from solstone.think.talent import get_talent, get_talent_configs
from tests.openhands_fakes import _REGISTERED_TOOLS, install_fake_openhands


@pytest.fixture
def fake_openhands(monkeypatch):
    return install_fake_openhands(monkeypatch)


@pytest.fixture
def fixed_time(monkeypatch):
    monkeypatch.setattr(openhands, "now_ms", lambda: 123456)


def _translator(
    fake_openhands,
    events: list[dict],
    *,
    llm=None,
    expects_emit_final: bool = False,
    max_turns: int = MAX_TURNS,
) -> openhands._OpenHandsTranslator:
    if llm is None:
        llm = fake_openhands.LLM(model="openai/gpt-5")
    return openhands._OpenHandsTranslator(
        callback=JSONEventCallback(events.append),
        llm=llm,
        provider="openai",
        model="openai/gpt-5",
        cost_cap=DEFAULT_RUN_COST_CAP_USD,
        max_turns=max_turns,
        expects_emit_final=expects_emit_final,
    )


def _run_config(monkeypatch, tmp_path, **overrides):
    monkeypatch.setattr(openhands, "get_journal", lambda: tmp_path)
    monkeypatch.setattr(openhands, "get_project_root", lambda: tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config = {
        "provider": "openai",
        "model": "gpt-5",
        "prompt": "Do the work.",
        "session_id": "11111111-1111-1111-1111-111111111111",
        "day": "20260522",
    }
    config.update(overrides)
    return config


def _real_talent_config(monkeypatch, tmp_path, name: str, **overrides):
    config = get_talent(name)
    config.update(_run_config(monkeypatch, tmp_path, **overrides))
    return config


def _run_and_capture_tool_state(fake_openhands, config: dict, events: list[dict]):
    fake_openhands.Conversation.instances = []
    fake_openhands.Conversation.arun_impl = None
    _REGISTERED_TOOLS.clear()

    result = asyncio.run(openhands.run_cogitate(config, events.append))
    conversation = fake_openhands.Conversation.instances[0]
    agent_tool_names = {tool.name for tool in conversation.agent.tools}
    registered_tool_names = set(_REGISTERED_TOOLS)
    return result, conversation, agent_tool_names, registered_tool_names


def _emit_final_action(
    fake_openhands, content: str, *, llm_response_id: str | None = None
):
    kwargs = {}
    if llm_response_id is not None:
        kwargs["llm_response_id"] = llm_response_id
    return fake_openhands.ActionEvent(
        reasoning_content=None,
        thinking_blocks=[],
        responses_reasoning_item=None,
        tool_name="emit_final",
        tool_call=SimpleNamespace(arguments=f'{{"content":"{content}"}}'),
        tool_call_id="emit-1",
        action=SimpleNamespace(content=content),
        **kwargs,
    )


def _sol_action(
    fake_openhands,
    call_id: str = "c1",
    *,
    llm_response_id: str | None = None,
):
    kwargs = {}
    if llm_response_id is not None:
        kwargs["llm_response_id"] = llm_response_id
    return fake_openhands.ActionEvent(
        reasoning_content=None,
        thinking_blocks=[],
        responses_reasoning_item=None,
        tool_name="sol",
        tool_call=SimpleNamespace(arguments='{"command":"sol call journal search x"}'),
        tool_call_id=call_id,
        action=None,
        **kwargs,
    )


def _parallel_sol_actions(fake_openhands, response_id: str, *call_ids: str):
    return [
        _sol_action(fake_openhands, call_id, llm_response_id=response_id)
        for call_id in call_ids
    ]


def _agent_message(fake_openhands, content: str):
    return fake_openhands.MessageEvent(
        source="agent",
        llm_message=SimpleNamespace(content=[SimpleNamespace(text=content)]),
    )


def _seed_usage(conversation, *, prompt_tokens: int = 12, completion_tokens: int = 4):
    usage = conversation.agent.llm.metrics.accumulated_token_usage
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    conversation.agent.llm.metrics.token_usages = [object()]


def _turn_warning_message(
    used: int,
    limit: int,
    *,
    percent: int,
    finish_tool: str = "finish",
) -> str:
    remaining = limit - used
    if percent == 50:
        instruction = (
            "Start converging on the final result and call "
            f"{finish_tool} as soon as useful work is complete."
        )
    elif percent == 75:
        instruction = (
            "Stop broad gathering; use the remaining turns only for synthesis and "
            f"final checks, then call {finish_tool}."
        )
    else:
        instruction = (
            "Finish now unless one more tool call is essential; call "
            f"{finish_tool} with the best complete result available."
        )
    return (
        f"Turn budget warning: you've used {percent}% of your turn budget so far: "
        f"{used} of {limit} turns, {remaining} turns left. {instruction}"
    )


def _turn_final_message(*, finish_tool: str = "finish") -> str:
    return (
        f"Turn budget reached: this is your last turn. Stop gathering more context "
        f"or using tools, and call {finish_tool} now with the best result available."
    )


def _fake_conversation(fake_openhands):
    return fake_openhands.Conversation(agent=SimpleNamespace(system_prompt="system"))


def _install_emit_final_arun(fake_openhands, content: str) -> None:
    async def emit_final(conversation):
        for callback in conversation.callbacks:
            callback(_emit_final_action(fake_openhands, content))

    fake_openhands.Conversation.arun_impl = emit_final


def test_fake_openhands_replaces_installed_sdk_modules(fake_openhands):
    from openhands.sdk import LLM
    from openhands.sdk.tool import ToolDefinition

    openhands._SOL_TYPES.clear()

    assert LLM is fake_openhands.LLM
    assert issubclass(openhands._ensure_sol_types()["SolTool"], ToolDefinition)


def test_emit_final_tool_description_contract(fake_openhands):
    from solstone.think.providers import emit_final_tool

    emit_final_tool._EMIT_FINAL_TYPES.clear()

    tools = emit_final_tool.build_emit_final_tools()

    assert len(tools) == 1
    assert tools[0].name == "emit_final"
    assert (
        "Terminal tool for ending the run with its final result" in tools[0].description
    )
    assert "Call this tool exactly once" in tools[0].description
    assert "Artifact talents:" in tools[0].description
    assert "Action talents:" in tools[0].description
    assert "concise, signal-carrying record" in tools[0].description
    assert "No-op:" in tools[0].description


def test_translator_maps_thinking_sources(fake_openhands, fixed_time):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)

    translator.on_event(
        fake_openhands.ActionEvent(
            reasoning_content="reasoning summary",
            thinking_blocks=[],
            responses_reasoning_item=None,
            tool_name="",
        )
    )
    translator.on_event(
        fake_openhands.ActionEvent(
            reasoning_content=None,
            thinking_blocks=[
                SimpleNamespace(thinking="signed thinking", signature="sig-1")
            ],
            responses_reasoning_item=None,
            tool_name="",
        )
    )
    translator.on_event(
        fake_openhands.ActionEvent(
            reasoning_content=None,
            thinking_blocks=[],
            responses_reasoning_item=SimpleNamespace(
                summary=[SimpleNamespace(text="responses reasoning")],
                encrypted_content="encrypted",
            ),
            tool_name="",
        )
    )

    assert [{key: event[key] for key in event if key != "raw"} for event in events] == [
        {
            "event": "thinking",
            "summary": "reasoning summary",
            "model": "openai/gpt-5",
            "signature": None,
            "redacted_data": None,
            "ts": 123456,
        },
        {
            "event": "thinking",
            "summary": "signed thinking",
            "model": "openai/gpt-5",
            "signature": "sig-1",
            "redacted_data": None,
            "ts": 123456,
        },
        {
            "event": "thinking",
            "summary": "responses reasoning",
            "model": "openai/gpt-5",
            "signature": None,
            "redacted_data": "encrypted",
            "ts": 123456,
        },
    ]
    assert events[0]["raw"][0]["reasoning_content"] == "reasoning summary"


def test_translator_maps_tool_start_and_paired_tool_end(fake_openhands, fixed_time):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)
    command = "sol call journal search x"

    translator.on_event(
        fake_openhands.ActionEvent(
            reasoning_content=None,
            thinking_blocks=[],
            responses_reasoning_item=None,
            tool_name="sol",
            tool_call=SimpleNamespace(arguments=f'{{"command":"{command}"}}'),
            tool_call_id="c1",
            action=None,
        )
    )
    translator.on_event(
        fake_openhands.ObservationEvent(
            tool_name="wrong-if-unpaired",
            tool_call_id="c1",
            observation=fake_openhands.Observation.from_text("tool output"),
        )
    )

    assert events[0]["event"] == "tool_start"
    assert events[0]["tool"] == "sol"
    assert events[0]["args"] == {"command": command}
    assert events[0]["call_id"] == "c1"
    assert events[0]["ts"] == 123456
    assert events[0]["raw"][0]["tool_name"] == "sol"

    assert events[1] == {
        "event": "tool_end",
        "tool": "sol",
        "args": {"command": command},
        "result": "tool output",
        "call_id": "c1",
        "raw": events[1]["raw"],
        "ts": 123456,
    }


def test_translator_records_finish_action_without_tool_start(
    fake_openhands,
    fixed_time,
):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)

    translator.on_event(
        fake_openhands.ActionEvent(
            reasoning_content=None,
            thinking_blocks=[],
            responses_reasoning_item=None,
            tool_name="finish",
            tool_call=SimpleNamespace(arguments='{"message":"done"}'),
            tool_call_id="finish-1",
            action=SimpleNamespace(message="done"),
        )
    )

    assert events == []
    assert translator.finish_message == "done"
    assert translator.result() == "done"


def test_translator_records_emit_final_action_without_tool_start(
    fake_openhands,
    fixed_time,
):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)
    translator.expects_emit_final = True

    translator.on_event(
        fake_openhands.ActionEvent(
            reasoning_content=None,
            thinking_blocks=[],
            responses_reasoning_item=None,
            tool_name="emit_final",
            tool_call=SimpleNamespace(arguments='{"content":"# Done"}'),
            tool_call_id="emit-1",
            action=SimpleNamespace(content="# Done"),
        )
    )

    assert events == []
    assert translator.emit_final_content == "# Done"
    assert translator.result() == "# Done"


def test_translator_result_prefers_emit_final_content(fake_openhands, fixed_time):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)
    translator.expects_emit_final = True

    translator.on_event(
        fake_openhands.MessageEvent(
            source="agent",
            llm_message=SimpleNamespace(
                content=[SimpleNamespace(text="message result")]
            ),
        )
    )
    translator.on_event(
        fake_openhands.ActionEvent(
            reasoning_content=None,
            thinking_blocks=[],
            responses_reasoning_item=None,
            tool_name="finish",
            tool_call=SimpleNamespace(arguments='{"message":"finish result"}'),
            tool_call_id="finish-1",
            action=SimpleNamespace(message="finish result"),
        )
    )
    translator.on_event(
        fake_openhands.ActionEvent(
            reasoning_content=None,
            thinking_blocks=[],
            responses_reasoning_item=None,
            tool_name="emit_final",
            tool_call=SimpleNamespace(arguments='{"content":"emit result"}'),
            tool_call_id="emit-1",
            action=SimpleNamespace(content="emit result"),
        )
    )

    assert translator.result() == "emit result"


def test_translator_returns_none_when_emit_final_branch_skipped(
    fake_openhands,
    fixed_time,
):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)
    translator.expects_emit_final = True

    translator.on_event(
        fake_openhands.MessageEvent(
            source="agent",
            llm_message=SimpleNamespace(
                content=[SimpleNamespace(text="message result")]
            ),
        )
    )

    assert translator.final_message == "message result"
    assert translator.result() is None


def test_translator_maps_text_delta_tokens(fake_openhands, fixed_time):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)

    translator.on_token(
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hi"))])
    )
    translator.on_token(
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))])
    )

    assert events == [
        {
            "event": "text_delta",
            "delta": "hi",
            "model": "openai/gpt-5",
            "ts": 123456,
        }
    ]


def test_translator_result_prefers_finish_message(fake_openhands, fixed_time):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)

    translator.on_event(
        fake_openhands.MessageEvent(
            source="agent",
            llm_message=SimpleNamespace(
                content=[SimpleNamespace(text="message result")]
            ),
        )
    )
    assert translator.final_message == "message result"
    assert translator.result() == "message result"

    translator.on_event(
        fake_openhands.ActionEvent(
            reasoning_content=None,
            thinking_blocks=[],
            responses_reasoning_item=None,
            tool_name="finish",
            tool_call=SimpleNamespace(arguments='{"message":"finish result"}'),
            tool_call_id="finish-1",
            action=SimpleNamespace(message="finish result"),
        )
    )
    assert translator.result() == "finish result"


def test_translator_maps_max_turns_flag(fake_openhands, fixed_time):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)

    translator.on_event(
        fake_openhands.ConversationErrorEvent(
            code="MaxIterationsReached",
            detail="limit",
        )
    )
    translator.on_event(
        fake_openhands.ConversationErrorEvent(
            code="MaxIterationsReached",
            detail="limit",
        )
    )
    translator.on_event(
        fake_openhands.ConversationErrorEvent(code="Other", detail="ignored")
    )

    assert translator.max_turns_exhausted is True
    assert [event for event in events if event["event"] == "max_turns_exhausted"] == []


def test_resource_monitor_wrapup_nudge_on_cost_axis(fake_openhands, fixed_time):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)
    translator.conversation = _fake_conversation(fake_openhands)
    translator.llm.metrics.accumulated_cost = COST_WARN_FRAC * DEFAULT_RUN_COST_CAP_USD

    translator.on_event(_sol_action(fake_openhands, "c1"))
    translator.on_event(_sol_action(fake_openhands, "c2"))

    assert translator.conversation.messages == [
        "Resource budget warning: this run is approaching its per-run resource "
        "budget. Finish useful work now and call finish with the best complete "
        "result you can produce."
    ]
    assert [event["event"] for event in events] == ["tool_start", "tool_start"]


def test_resource_monitor_wrapup_nudge_on_context_axis(fake_openhands, fixed_time):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)
    translator.conversation = _fake_conversation(fake_openhands)
    translator.llm.effective_max_input_tokens = 100
    translator.llm.metrics.accumulated_token_usage.per_turn_token = int(
        CONTEXT_WARN_FRAC * 100
    )

    translator.on_event(_sol_action(fake_openhands))

    assert len(translator.conversation.messages) == 1
    assert "Resource budget warning" in translator.conversation.messages[0]
    assert "call finish" in translator.conversation.messages[0]


def test_resource_monitor_context_axis_noop_without_window(
    fake_openhands,
    fixed_time,
):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)
    translator.conversation = _fake_conversation(fake_openhands)
    translator.llm.metrics.accumulated_token_usage.per_turn_token = 1_000_000

    translator.on_event(_sol_action(fake_openhands))

    assert translator.conversation.messages == []
    assert [event["event"] for event in events] == ["tool_start"]


def test_resource_monitor_cost_fallback_from_fresh_tokens(
    fake_openhands,
    fixed_time,
):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)
    translator.conversation = _fake_conversation(fake_openhands)
    usage = translator.llm.metrics.accumulated_token_usage
    usage.prompt_tokens = 300_000
    usage.cache_read_tokens = 20_000

    translator.on_event(_sol_action(fake_openhands))

    assert len(translator.conversation.messages) == 1
    assert "Resource budget warning" in translator.conversation.messages[0]


def test_resource_monitor_final_turn_then_pause(fake_openhands, fixed_time):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)
    translator.conversation = _fake_conversation(fake_openhands)
    translator.llm.metrics.accumulated_cost = DEFAULT_RUN_COST_CAP_USD

    translator.on_event(_sol_action(fake_openhands, "c1"))

    assert translator._final_turn_armed is True
    assert translator.conversation.paused is False
    assert translator.conversation.messages == [
        "Resource budget reached: this is the final turn. Stop gathering more "
        "context or using tools, and call finish now with the best result "
        "available."
    ]

    translator.on_event(_sol_action(fake_openhands, "c2"))

    assert translator.conversation.paused is True
    assert translator._cost_force_stopped is True
    assert [event["event"] for event in events] == ["tool_start", "tool_start"]


def test_resource_monitor_finish_after_warning_no_pause(fake_openhands, fixed_time):
    events: list[dict] = []
    translator = _translator(fake_openhands, events, expects_emit_final=True)
    translator.conversation = _fake_conversation(fake_openhands)
    translator.llm.metrics.accumulated_cost = DEFAULT_RUN_COST_CAP_USD

    translator.on_event(_sol_action(fake_openhands, "c1"))
    translator.on_event(_emit_final_action(fake_openhands, "# Done"))

    assert translator.conversation.paused is False
    assert translator._cost_force_stopped is False
    assert translator.result() == "# Done"


def test_run_cogitate_uses_emit_final_branch_for_output_path(
    fake_openhands,
    monkeypatch,
    tmp_path,
):
    config = _run_config(monkeypatch, tmp_path, output_path=str(tmp_path / "out.md"))
    events: list[dict] = []

    result = asyncio.run(openhands.run_cogitate(config, events.append))

    conversation = fake_openhands.Conversation.instances[0]
    assert result is None
    assert [tool.name for tool in conversation.agent.tools] == [
        "sol",
        "read_file",
        "list_directory",
        "glob",
        "grep_search",
        "emit_final",
    ]
    assert conversation.agent.include_default_tools == []
    error_events = [event for event in events if event["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["reason_code"] == "no_output"
    assert error_events[0].get("terminal") is True
    assert [event for event in events if event["event"] == "finish"] == []
    assert [
        event["event"] for event in events if event["event"] in ("finish", "error")
    ] == ["error"]


def test_run_cogitate_emits_no_output_for_whitespace_emit_final(
    fake_openhands,
    monkeypatch,
    tmp_path,
):
    _install_emit_final_arun(fake_openhands, "   ")
    config = _run_config(monkeypatch, tmp_path, output_path=str(tmp_path / "out.md"))
    events: list[dict] = []

    result = asyncio.run(openhands.run_cogitate(config, events.append))

    assert result is None
    error_events = [event for event in events if event["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["reason_code"] == "no_output"
    assert error_events[0].get("terminal") is True
    assert [event for event in events if event["event"] == "finish"] == []
    assert [
        event["event"] for event in events if event["event"] in ("finish", "error")
    ] == ["error"]


def test_run_cogitate_emits_finish_when_emit_final_has_content(
    fake_openhands,
    monkeypatch,
    tmp_path,
):
    _install_emit_final_arun(fake_openhands, "No changes needed.")
    config = _run_config(monkeypatch, tmp_path, output_path=str(tmp_path / "out.md"))
    events: list[dict] = []

    result = asyncio.run(openhands.run_cogitate(config, events.append))

    assert result == "No changes needed."
    finish_events = [event for event in events if event["event"] == "finish"]
    assert len(finish_events) == 1
    assert finish_events[0]["result"] == "No changes needed."
    assert [event for event in events if event["event"] == "error"] == []
    assert [
        event["event"] for event in events if event["event"] in ("finish", "error")
    ] == ["finish"]


def test_run_cogitate_daily_no_output_finishes_when_emit_final_has_content(
    fake_openhands,
    monkeypatch,
    tmp_path,
):
    _install_emit_final_arun(fake_openhands, "no changes")
    config = _run_config(monkeypatch, tmp_path, schedule="daily")
    events: list[dict] = []

    result = asyncio.run(openhands.run_cogitate(config, events.append))

    assert result == "no changes"
    finish_events = [event for event in events if event["event"] == "finish"]
    assert len(finish_events) == 1
    assert finish_events[0]["result"] == "no changes"
    assert [event for event in events if event["event"] == "error"] == []


def test_run_cogitate_keeps_finish_branch_without_output_path(
    fake_openhands,
    monkeypatch,
    tmp_path,
):
    config = _run_config(monkeypatch, tmp_path, schedule="segment")
    events: list[dict] = []

    result = asyncio.run(openhands.run_cogitate(config, events.append))

    conversation = fake_openhands.Conversation.instances[0]
    assert result is None
    assert [tool.name for tool in conversation.agent.tools] == [
        "sol",
        "read_file",
        "list_directory",
        "glob",
        "grep_search",
    ]
    assert conversation.agent.include_default_tools == ["FinishTool"]
    finish_events = [event for event in events if event["event"] == "finish"]
    assert len(finish_events) == 1
    assert finish_events[0]["result"] is None
    assert [event for event in events if event["event"] == "error"] == []


def test_run_cogitate_uses_emit_final_branch_for_daily_no_output(
    fake_openhands,
    monkeypatch,
    tmp_path,
):
    config = _run_config(monkeypatch, tmp_path, schedule="daily")
    events: list[dict] = []

    asyncio.run(openhands.run_cogitate(config, events.append))

    conversation = fake_openhands.Conversation.instances[0]
    assert not config.get("output_path")
    assert [tool.name for tool in conversation.agent.tools] == [
        "sol",
        "read_file",
        "list_directory",
        "glob",
        "grep_search",
        "emit_final",
    ]
    assert conversation.agent.include_default_tools == []
    error_events = [event for event in events if event["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["reason_code"] == "no_output"
    assert error_events[0].get("terminal") is True
    assert [event for event in events if event["event"] == "finish"] == []
    assert [
        event["event"] for event in events if event["event"] in ("finish", "error")
    ] == ["error"]


def test_run_cogitate_force_stop_emits_token_budget_exceeded(
    fake_openhands,
    fixed_time,
    monkeypatch,
    tmp_path,
):
    async def hit_cost_cap(conversation):
        _seed_usage(conversation)
        conversation.agent.llm.metrics.accumulated_cost = DEFAULT_RUN_COST_CAP_USD
        for callback in conversation.callbacks:
            callback(_sol_action(fake_openhands, "c1"))
        for callback in conversation.callbacks:
            callback(_sol_action(fake_openhands, "c2"))

    fake_openhands.Conversation.arun_impl = hit_cost_cap
    config = _run_config(monkeypatch, tmp_path, output_path=str(tmp_path / "out.md"))
    events: list[dict] = []

    result = asyncio.run(openhands.run_cogitate(config, events.append))

    assert result is None
    error_events = [event for event in events if event["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["reason_code"] == "token_budget_exceeded"
    assert error_events[0]["terminal"] is True
    assert error_events[0]["result"] is None
    assert error_events[0]["usage"]["total_tokens"] > 0
    assert fake_openhands.Conversation.instances[0].closed is True
    assert [event for event in events if event.get("reason_code") == "no_output"] == []
    assert [event for event in events if event["event"] == "finish"] == []


def test_run_cogitate_cost_force_stop_with_partial_logs_once(
    fake_openhands,
    fixed_time,
    monkeypatch,
    tmp_path,
):
    partial = "partial result"

    async def hit_cost_cap_with_partial(conversation):
        _seed_usage(conversation)
        conversation.agent.llm.metrics.accumulated_cost = DEFAULT_RUN_COST_CAP_USD
        for callback in conversation.callbacks:
            callback(_agent_message(fake_openhands, partial))
        for callback in conversation.callbacks:
            callback(_sol_action(fake_openhands, "c1"))
        for callback in conversation.callbacks:
            callback(_sol_action(fake_openhands, "c2"))

    fake_openhands.Conversation.arun_impl = hit_cost_cap_with_partial
    config = _run_config(monkeypatch, tmp_path)
    events: list[dict] = []

    result = asyncio.run(openhands.run_cogitate(config, events.append))

    assert result == partial
    error_events = [event for event in events if event["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["reason_code"] == "token_budget_exceeded"
    assert error_events[0]["terminal"] is True
    assert error_events[0]["result"] == partial
    assert error_events[0]["usage"]["total_tokens"] > 0
    assert [event for event in events if event["event"] == "finish"] == []
    assert fake_openhands.Conversation.instances[0].closed is True


def test_run_cogitate_wall_clock_exceeded_emits_terminal_error(
    fake_openhands,
    fixed_time,
    monkeypatch,
    tmp_path,
):
    async def never_finishes(conversation):
        _seed_usage(conversation)
        await asyncio.sleep(30)

    fake_openhands.Conversation.arun_impl = never_finishes
    monkeypatch.setattr(openhands, "WALL_CLOCK_GRACE_S", 0.0)
    config = _run_config(monkeypatch, tmp_path)
    config["timeout_seconds"] = 0.1
    events: list[dict] = []

    result = asyncio.run(openhands.run_cogitate(config, events.append))

    assert result is None
    error_events = [event for event in events if event["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["reason_code"] == "wall_clock_exceeded"
    assert error_events[0]["terminal"] is True
    assert error_events[0]["result"] is None
    assert error_events[0]["usage"]["total_tokens"] > 0
    assert [event for event in events if event["event"] == "finish"] == []
    assert fake_openhands.Conversation.instances[0].closed is True


def test_run_cogitate_wall_clock_exceeded_preserves_partial(
    fake_openhands,
    fixed_time,
    monkeypatch,
    tmp_path,
):
    partial = "partial result"

    async def emit_then_hang(conversation):
        _seed_usage(conversation)
        for callback in conversation.callbacks:
            callback(_agent_message(fake_openhands, partial))
        await asyncio.sleep(30)

    fake_openhands.Conversation.arun_impl = emit_then_hang
    monkeypatch.setattr(openhands, "WALL_CLOCK_GRACE_S", 0.0)
    config = _run_config(monkeypatch, tmp_path)
    config["timeout_seconds"] = 0.1
    events: list[dict] = []

    result = asyncio.run(openhands.run_cogitate(config, events.append))

    assert result == partial
    error_events = [event for event in events if event["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["reason_code"] == "wall_clock_exceeded"
    assert error_events[0]["terminal"] is True
    assert error_events[0]["result"] == partial
    assert error_events[0]["usage"]["total_tokens"] > 0
    assert [event for event in events if event["event"] == "finish"] == []
    assert fake_openhands.Conversation.instances[0].closed is True


def test_wall_clock_deadline_derivation():
    grace = openhands.WALL_CLOCK_GRACE_S
    assert openhands._wall_clock_deadline_s(600) == 600 - grace
    assert openhands._wall_clock_deadline_s(600) < 600
    # timeout at/below the grace falls back to half the budget, still positive
    # and strictly less than timeout_seconds.
    assert 0 < openhands._wall_clock_deadline_s(grace) < grace


def test_build_llm_bounds_timeout_and_retries(fake_openhands, monkeypatch):
    monkeypatch.setenv(openhands._API_KEY_ENV["anthropic"], "test-key")
    llm = openhands._build_llm("anthropic", "claude-test")
    assert llm.timeout == openhands.LLM_TIMEOUT_S
    assert llm.num_retries == openhands.LLM_NUM_RETRIES


def test_run_cogitate_stuck_emits_agent_stuck_error(
    fake_openhands,
    fixed_time,
    monkeypatch,
    tmp_path,
):
    async def stuck(conversation):
        _seed_usage(conversation)
        conversation.state.execution_status = "stuck"

    fake_openhands.Conversation.arun_impl = stuck
    config = _run_config(monkeypatch, tmp_path)
    events: list[dict] = []

    result = asyncio.run(openhands.run_cogitate(config, events.append))

    assert result is None
    error_events = [event for event in events if event["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["reason_code"] == "agent_stuck"
    assert error_events[0]["terminal"] is True
    assert error_events[0]["result"] is None
    assert error_events[0]["usage"]["total_tokens"] > 0
    assert [event for event in events if event["event"] == "finish"] == []
    assert fake_openhands.Conversation.instances[0].closed is True


def test_run_cogitate_stuck_with_partial_preserves_result(
    fake_openhands,
    fixed_time,
    monkeypatch,
    tmp_path,
):
    partial = "partial result"

    async def stuck_with_partial(conversation):
        _seed_usage(conversation)
        for callback in conversation.callbacks:
            callback(_agent_message(fake_openhands, partial))
        conversation.state.execution_status = "stuck"

    fake_openhands.Conversation.arun_impl = stuck_with_partial
    config = _run_config(monkeypatch, tmp_path)
    events: list[dict] = []

    result = asyncio.run(openhands.run_cogitate(config, events.append))

    assert result == partial
    error_events = [event for event in events if event["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["reason_code"] == "agent_stuck"
    assert error_events[0]["terminal"] is True
    assert error_events[0]["result"] == partial
    assert error_events[0]["usage"]["total_tokens"] > 0
    assert [event for event in events if event["event"] == "finish"] == []
    assert fake_openhands.Conversation.instances[0].closed is True


def test_run_cogitate_stuck_zero_usage_still_emits_terminal_error(
    fake_openhands,
    fixed_time,
    monkeypatch,
    tmp_path,
):
    async def stuck_without_usage(conversation):
        conversation.state.execution_status = "stuck"

    fake_openhands.Conversation.arun_impl = stuck_without_usage
    config = _run_config(monkeypatch, tmp_path)
    events: list[dict] = []

    result = asyncio.run(openhands.run_cogitate(config, events.append))

    assert result is None
    error_events = [event for event in events if event["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["reason_code"] == "agent_stuck"
    assert error_events[0]["terminal"] is True
    assert error_events[0]["usage"]["total_tokens"] == 0
    assert [event for event in events if event["event"] == "finish"] == []
    assert fake_openhands.Conversation.instances[0].closed is True


def test_run_cogitate_threads_max_run_cost_usd_override(
    fake_openhands,
    monkeypatch,
    tmp_path,
):
    real_translator = openhands._OpenHandsTranslator
    translators: list[openhands._OpenHandsTranslator] = []

    class CapturingTranslator(real_translator):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            translators.append(self)

    monkeypatch.setattr(openhands, "_OpenHandsTranslator", CapturingTranslator)
    config = _run_config(monkeypatch, tmp_path, max_run_cost_usd=2.5)
    events: list[dict] = []

    asyncio.run(openhands.run_cogitate(config, events.append))

    assert len(translators) == 1
    assert translators[0].cost_cap == 2.5


def test_resource_monitor_injects_via_send_message_not_system_prompt(
    fake_openhands,
    monkeypatch,
    tmp_path,
):
    """AC#7: cogitate has no AgentContext, so send_message appends user suffix only."""

    async def hit_warn_cap(conversation):
        original_system_prompt = conversation.agent.system_prompt
        conversation.agent.llm.metrics.accumulated_cost = (
            COST_WARN_FRAC * DEFAULT_RUN_COST_CAP_USD
        )
        for callback in conversation.callbacks:
            callback(_sol_action(fake_openhands))
        assert conversation.agent.system_prompt == original_system_prompt

    fake_openhands.Conversation.arun_impl = hit_warn_cap
    config = _run_config(monkeypatch, tmp_path)
    events: list[dict] = []

    asyncio.run(openhands.run_cogitate(config, events.append))

    conversation = fake_openhands.Conversation.instances[0]
    warnings = [
        message
        for message in conversation.messages
        if message.startswith("Resource budget warning")
    ]
    assert len(warnings) == 1
    assert warnings[0] not in conversation.agent.system_prompt


def test_turn_budget_warning_injects_via_send_message_not_system_prompt(
    fake_openhands,
    monkeypatch,
    tmp_path,
):
    async def hit_turn_warn(conversation):
        original_system_prompt = conversation.agent.system_prompt
        for turn in range(1, 3):
            for callback in conversation.callbacks:
                callback(
                    _sol_action(
                        fake_openhands,
                        f"c{turn}",
                        llm_response_id=f"r{turn}",
                    )
                )
        assert conversation.agent.system_prompt == original_system_prompt

    fake_openhands.Conversation.arun_impl = hit_turn_warn
    config = _run_config(monkeypatch, tmp_path, max_turns=4)
    events: list[dict] = []

    asyncio.run(openhands.run_cogitate(config, events.append))

    conversation = fake_openhands.Conversation.instances[0]
    warnings = [
        message
        for message in conversation.messages
        if message.startswith("Turn budget warning")
    ]
    assert warnings == [_turn_warning_message(2, 4, percent=50)]
    assert warnings[0] not in conversation.agent.system_prompt


def test_turn_budget_warnings_fire_at_thresholds_once(fake_openhands, fixed_time):
    events: list[dict] = []
    translator = _translator(fake_openhands, events, max_turns=20)
    translator.conversation = _fake_conversation(fake_openhands)

    for turn in range(1, 21):
        translator.on_event(
            _sol_action(fake_openhands, f"c{turn}", llm_response_id=f"r{turn}")
        )

    warnings = [
        message
        for message in translator.conversation.messages
        if message.startswith("Turn budget warning")
    ]
    assert warnings == [
        _turn_warning_message(10, 20, percent=50),
        _turn_warning_message(15, 20, percent=75),
        _turn_warning_message(18, 20, percent=90),
    ]


def test_turn_budget_parallel_actions_share_one_observed_turn(
    fake_openhands,
    fixed_time,
):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)
    translator.conversation = _fake_conversation(fake_openhands)

    for action in _parallel_sol_actions(fake_openhands, "r1", "c1", "c2", "c3"):
        translator.on_event(action)

    assert translator._observed_turns == 1


def test_turn_budget_seen_response_id_does_not_advance_or_refire(
    fake_openhands,
    fixed_time,
):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)
    translator.conversation = _fake_conversation(fake_openhands)

    translator.on_event(_sol_action(fake_openhands, "c1", llm_response_id="r1"))
    messages_after_first = list(translator.conversation.messages)
    translator.on_event(_sol_action(fake_openhands, "c2", llm_response_id="r1"))

    assert translator._observed_turns == 1
    assert translator.conversation.messages == messages_after_first


def test_turn_budget_missing_response_id_counts_each_action(
    fake_openhands,
    fixed_time,
):
    events: list[dict] = []
    translator = _translator(fake_openhands, events)
    translator.conversation = _fake_conversation(fake_openhands)

    for turn in range(1, 4):
        translator.on_event(_sol_action(fake_openhands, f"c{turn}"))

    assert translator._observed_turns == 3


def test_turn_budget_thresholds_use_ceiling(fake_openhands, fixed_time):
    events: list[dict] = []
    translator = _translator(fake_openhands, events, max_turns=10)
    translator.conversation = _fake_conversation(fake_openhands)

    for turn in range(1, 8):
        translator.on_event(
            _sol_action(fake_openhands, f"c{turn}", llm_response_id=f"r{turn}")
        )

    warnings = [
        message
        for message in translator.conversation.messages
        if message.startswith("Turn budget warning")
    ]
    assert warnings == [_turn_warning_message(5, 10, percent=50)]

    translator.on_event(_sol_action(fake_openhands, "c8", llm_response_id="r8"))

    warnings = [
        message
        for message in translator.conversation.messages
        if message.startswith("Turn budget warning")
    ]
    assert warnings == [
        _turn_warning_message(5, 10, percent=50),
        _turn_warning_message(8, 10, percent=75),
    ]


def test_turn_budget_final_ultimatum_arms_at_one_remaining(
    fake_openhands,
    fixed_time,
):
    events: list[dict] = []
    translator = _translator(fake_openhands, events, max_turns=3)
    translator.conversation = _fake_conversation(fake_openhands)

    translator.on_event(_sol_action(fake_openhands, "c1", llm_response_id="r1"))
    translator.on_event(_sol_action(fake_openhands, "c2", llm_response_id="r2"))

    assert translator.conversation.messages == [_turn_final_message()]
    assert translator._turn_final_armed is True
    assert translator.conversation.paused is False


def test_turn_budget_final_ultimatum_handles_single_turn_limit(
    fake_openhands,
    fixed_time,
):
    events: list[dict] = []
    translator = _translator(fake_openhands, events, max_turns=1)
    translator.conversation = _fake_conversation(fake_openhands)

    translator.on_event(_sol_action(fake_openhands, "c1", llm_response_id="r1"))
    translator.on_event(_sol_action(fake_openhands, "c2", llm_response_id="r2"))

    assert translator.conversation.messages == [_turn_final_message()]
    assert translator.conversation.paused is True
    assert translator.max_turns_exhausted is True
    assert [event for event in events if event["event"] == "max_turns_exhausted"] == []


def test_run_cogitate_turn_force_stop_uses_solstone_max_turns_path(
    fake_openhands,
    fixed_time,
    monkeypatch,
    tmp_path,
):
    fed_conversation_error_codes: list[str] = []

    def feed(conversation, event):
        if isinstance(event, fake_openhands.ConversationErrorEvent):
            fed_conversation_error_codes.append(str(getattr(event, "code", "")))
        for callback in conversation.callbacks:
            callback(event)

    async def exhaust_turn_budget(conversation):
        _seed_usage(conversation)
        feed(conversation, _agent_message(fake_openhands, "partial result"))
        for turn in range(1, 4):
            feed(
                conversation,
                _sol_action(
                    fake_openhands,
                    f"c{turn}",
                    llm_response_id=f"r{turn}",
                ),
            )

    fake_openhands.Conversation.arun_impl = exhaust_turn_budget
    config = _run_config(monkeypatch, tmp_path, max_turns=3)
    events: list[dict] = []

    result = asyncio.run(openhands.run_cogitate(config, events.append))

    conversation = fake_openhands.Conversation.instances[0]
    assert result == "partial result"
    error_events = [event for event in events if event["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["reason_code"] == "max_turns_exhausted"
    assert error_events[0]["terminal"] is True
    assert error_events[0]["result"] == "partial result"
    assert error_events[0]["usage"]["total_tokens"] > 0
    assert conversation.closed is True
    assert [event for event in events if event["event"] == "finish"] == []
    assert [event for event in events if event["event"] == "max_turns_exhausted"] == []
    assert fed_conversation_error_codes == []


def test_turn_budget_emit_final_without_response_id_preempts_force_stop(
    fake_openhands,
    fixed_time,
):
    events: list[dict] = []
    translator = _translator(
        fake_openhands,
        events,
        expects_emit_final=True,
        max_turns=4,
    )
    translator.conversation = _fake_conversation(fake_openhands)

    translator.on_event(_sol_action(fake_openhands, "c1", llm_response_id="r1"))
    translator.on_event(_sol_action(fake_openhands, "c2", llm_response_id="r2"))
    translator.on_event(_emit_final_action(fake_openhands, "# Done"))

    assert translator.conversation.messages == [
        _turn_warning_message(2, 4, percent=50, finish_tool="emit_final")
    ]
    assert translator.conversation.paused is False
    assert translator._turn_force_stopped is False
    assert translator.result() == "# Done"


def test_run_cogitate_skips_read_tool_registration_when_tier_caps_disable_reads(
    fake_openhands,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        openhands,
        "capabilities_for_access_tier",
        lambda _tier: AccessCapabilities(sol=True, reads=False, submit=False),
    )
    config = _run_config(monkeypatch, tmp_path, access_tier="normal")
    events: list[dict] = []

    _result, conversation, agent_tool_names, registered_tool_names = (
        _run_and_capture_tool_state(fake_openhands, config, events)
    )

    read_tool_names = {"read_file", "list_directory", "glob", "grep_search"}
    assert "sol" in agent_tool_names
    assert "sol" in registered_tool_names
    assert agent_tool_names.isdisjoint(read_tool_names)
    assert registered_tool_names.isdisjoint(read_tool_names)
    assert conversation.agent.include_default_tools == ["FinishTool"]


def test_run_cogitate_passes_outbound_approval_to_policy(
    fake_openhands,
    monkeypatch,
    tmp_path,
):
    real_policy = openhands.CogitatePolicy
    approvals: list[str | None] = []

    class CapturingPolicy(real_policy):
        def __init__(self, *args, **kwargs):
            approvals.append(kwargs.get("outbound_approval"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(openhands, "CogitatePolicy", CapturingPolicy)
    config = _run_config(
        monkeypatch,
        tmp_path,
        access_tier="outbound",
        outbound_approval="approval-token",
    )
    events: list[dict] = []

    _run_and_capture_tool_state(fake_openhands, config, events)

    assert approvals == ["approval-token"]


@pytest.mark.parametrize(
    ("name", "expected_access_tier", "expected_agent_tools", "expected_default_tools"),
    [
        (
            "support:support",
            "outbound",
            {"sol"},
            ["FinishTool"],
        ),
        (
            "exec",
            "normal",
            {"sol", "read_file", "list_directory", "glob", "grep_search"},
            ["FinishTool"],
        ),
        (
            "read",
            "normal",
            {"sol", "read_file", "list_directory", "glob", "grep_search"},
            ["FinishTool"],
        ),
    ],
)
def test_run_cogitate_real_talent_access_tiers_register_expected_tools(
    fake_openhands,
    monkeypatch,
    tmp_path,
    name,
    expected_access_tier,
    expected_agent_tools,
    expected_default_tools,
):
    config = _real_talent_config(monkeypatch, tmp_path, name)
    events: list[dict] = []

    _result, conversation, agent_tool_names, registered_tool_names = (
        _run_and_capture_tool_state(fake_openhands, config, events)
    )

    assert config["access_tier"] == expected_access_tier
    assert agent_tool_names == expected_agent_tools
    assert registered_tool_names == expected_agent_tools
    assert conversation.agent.include_default_tools == expected_default_tools


def test_run_cogitate_exec_tool_surface_matches_normal_talent(
    fake_openhands,
    monkeypatch,
    tmp_path,
):
    exec_config = _real_talent_config(monkeypatch, tmp_path, "exec")
    normal_config = _real_talent_config(monkeypatch, tmp_path, "read")

    exec_events: list[dict] = []
    _result, _conversation, exec_tool_names, _registered = _run_and_capture_tool_state(
        fake_openhands, exec_config, exec_events
    )
    normal_events: list[dict] = []
    _result, _conversation, normal_tool_names, _registered = (
        _run_and_capture_tool_state(fake_openhands, normal_config, normal_events)
    )

    assert exec_config["access_tier"] == "normal"
    assert normal_config["access_tier"] == "normal"
    assert exec_tool_names == normal_tool_names


def test_schedule_gated_cogitate_prompts_use_emit_final():
    old_tool_name = "emit" + "_output"
    configs = get_talent_configs(type="cogitate")
    converted = {
        name: config
        for name, config in configs.items()
        if config.get("schedule") in {"daily", "weekly", "activity"}
        and "output" not in config
    }
    # steward and facet_newsletter are generate talents now, not cogitate prompts.
    assert len(converted) == 3

    for name, config in converted.items():
        body = Path(config["path"]).read_text(encoding="utf-8")
        assert "emit_final" in body, name
        assert old_tool_name not in body, name
        assert "FinishTool" not in body, name
        assert body.count("emit_final") >= 2, name


def test_weekly_reflection_declares_run_cost_override():
    config = get_talent("weekly_reflection")

    assert config["max_run_cost_usd"] == 5.00


def test_partner_declares_same_max_turns_as_weekly_reflection():
    partner = get_talent("partner")
    weekly_reflection = get_talent("weekly_reflection")

    assert partner["max_turns"] == weekly_reflection["max_turns"]


def test_run_cogitate_uses_default_max_turn_headroom(
    fake_openhands,
    monkeypatch,
    tmp_path,
):
    config = _run_config(monkeypatch, tmp_path)
    events: list[dict] = []

    asyncio.run(openhands.run_cogitate(config, events.append))

    conversation = fake_openhands.Conversation.instances[0]
    assert conversation.max_iteration_per_run == MAX_TURNS + MAX_TURNS_HEADROOM


def test_run_cogitate_threads_configured_max_turns(
    fake_openhands,
    fixed_time,
    monkeypatch,
    tmp_path,
):
    async def exhaust(conversation):
        _seed_usage(conversation)
        for callback in conversation.callbacks:
            callback(
                fake_openhands.ConversationErrorEvent(
                    code="MaxIterationsReached",
                    detail="limit",
                )
            )

    fake_openhands.Conversation.arun_impl = exhaust
    config = _run_config(monkeypatch, tmp_path, max_turns=100)
    events: list[dict] = []

    result = asyncio.run(openhands.run_cogitate(config, events.append))

    conversation = fake_openhands.Conversation.instances[0]
    assert result is None
    assert conversation.max_iteration_per_run == 102
    error_events = [event for event in events if event["event"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["reason_code"] == "max_turns_exhausted"
    assert error_events[0]["terminal"] is True
    assert error_events[0]["usage"]["total_tokens"] > 0
    assert conversation.closed is True
    assert [event for event in events if event["event"] == "max_turns_exhausted"] == []


def test_usage_delta_is_normalized_delta():
    class Usage:
        prompt_tokens = 10
        completion_tokens = 20
        cache_read_tokens = 3
        cache_write_tokens = 4
        reasoning_tokens = 5

    class Metrics:
        accumulated_token_usage = Usage()
        token_usages = [object()]

    llm = SimpleNamespace(metrics=Metrics())
    start = openhands._usage_snapshot(llm)

    Usage.prompt_tokens = 15
    Usage.completion_tokens = 29
    Usage.cache_read_tokens = 8
    Usage.cache_write_tokens = 10
    Usage.reasoning_tokens = 12
    Metrics.token_usages = [object(), object(), object()]

    usage = openhands._usage_delta(start, llm)

    assert set(usage) == USAGE_KEYS
    assert usage == {
        "input_tokens": 5,
        "output_tokens": 9,
        "cached_tokens": 5,
        "cache_creation_tokens": 6,
        "reasoning_tokens": 7,
        "requests": 2,
        "total_tokens": 14,
    }
