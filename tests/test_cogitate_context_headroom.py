# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import Field, PrivateAttr

from tests._logging_isolation import preserve_global_logging

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

with preserve_global_logging():
    from openhands.sdk import Agent, Conversation, MessageEvent
    from openhands.sdk.context.condenser.utils import get_total_token_count
    from openhands.sdk.llm import LLMResponse, Message, TextContent
    from openhands.sdk.testing import TestLLM
    from openhands.sdk.tool import ToolAnnotations, ToolDefinition, ToolExecutor
    from openhands.sdk.tool.registry import register_tool
    from openhands.sdk.tool.schema import Action, Observation
    from openhands.sdk.tool.spec import Tool

from solstone.think.providers import openhands

_TOOL_NAME = "context_headroom_probe"
_DIVERGENCE_FACTOR = 1.125
_SEED_TEXT = (
    "Earlier segment observation records local context, tool use, finalization, "
    "and verification details for this run.\n" * 40
)
_PAD_UNIT = " measured evidence padding"
# These caps turn tokenizer non-growth into harness failures instead of hangs.
_MAX_SEED_TURNS = 256
_MAX_PAD_STEPS = 4096


@dataclass(frozen=True)
class _RequestRecord:
    kind: str
    estimated_input: int
    max_output_tokens: int


class _ProbeAction(Action):
    value: str = Field(default="", description="Probe value.")


class _ProbeObservation(Observation):
    pass


class _ProbeExecutor(ToolExecutor):
    def __call__(self, action: Any, conversation: Any = None) -> _ProbeObservation:
        del action, conversation
        return _ProbeObservation.from_text("ok")


class _ProbeTool(ToolDefinition[_ProbeAction, _ProbeObservation]):
    name = _TOOL_NAME

    @classmethod
    def create(cls, *args: Any, **kwargs: Any) -> list[Any]:
        del args, kwargs
        return []


class _RecordingLLM(TestLLM):
    _records: list[_RequestRecord] = PrivateAttr(default_factory=list)

    @property
    def records(self) -> list[_RequestRecord]:
        return self._records

    def completion(
        self,
        messages: list[Message],
        tools: Sequence[ToolDefinition] | None = None,
        _return_metrics: bool = False,
        add_security_risk_prediction: bool = False,
        on_token: Any | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        tool_list = list(tools or [])
        self._records.append(
            _RequestRecord(
                kind="agent" if tool_list else "condenser",
                estimated_input=self.get_token_count(
                    messages,
                    tools=tool_list,
                    add_security_risk_prediction=True,
                ),
                max_output_tokens=self.effective_max_output_tokens,
            )
        )
        return super().completion(
            messages=messages,
            tools=tools,
            _return_metrics=_return_metrics,
            add_security_risk_prediction=add_security_risk_prediction,
            on_token=on_token,
            **kwargs,
        )


def _assistant_message(text: str) -> Message:
    return Message(role="assistant", content=[TextContent(text=text)])


def _probe_tool() -> _ProbeTool:
    return _ProbeTool(
        description="Probe tool that keeps the request on the tool-calling path.",
        action_type=_ProbeAction,
        observation_type=_ProbeObservation,
        executor=_ProbeExecutor(),
        annotations=ToolAnnotations(
            title=_TOOL_NAME,
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )


def _recording_llm(*, window: int, reserve: int) -> _RecordingLLM:
    return _RecordingLLM.from_messages(
        [_assistant_message("summary") for _ in range(300)],
        model="openai/gpt-4o-mini",
        native_tool_calling=False,
        max_input_tokens=window,
        max_output_tokens=reserve,
        input_cost_per_token=0,
        output_cost_per_token=0,
    )


def _conversation(
    *,
    llm: _RecordingLLM,
    boundary: int,
    tmp_path,
) -> Conversation:
    register_tool(_TOOL_NAME, _probe_tool())
    agent = Agent(
        llm=llm,
        tools=[Tool(name=_TOOL_NAME)],
        include_default_tools=[],
        system_prompt="System prompt for local condenser headroom tests.",
        condenser=openhands._build_local_condenser(llm, max_tokens=boundary),
    )
    return Conversation(
        agent=agent,
        workspace=str(tmp_path),
        persistence_dir=str(tmp_path / "history"),
        visualizer=None,
        stuck_detection=False,
    )


def _user_event(text: str) -> MessageEvent:
    return MessageEvent(
        source="user",
        llm_message=Message(role="user", content=[TextContent(text=text)]),
    )


def _prepared_count(
    *,
    conversation: Conversation,
    llm: _RecordingLLM,
    candidate: str | None = None,
) -> int:
    events = list(conversation.state.events)
    if candidate is not None:
        events.append(_user_event(candidate))
    return get_total_token_count(events, llm)


def _run_turn(conversation: Conversation, message: str) -> None:
    conversation.send_message(message)
    asyncio.run(conversation.arun())


def _seed_history(
    *,
    conversation: Conversation,
    llm: _RecordingLLM,
    target_low: int,
) -> None:
    last_count: int | None = None
    for seed_turns in range(_MAX_SEED_TURNS):
        next_count = _prepared_count(
            conversation=conversation,
            llm=llm,
            candidate=_SEED_TEXT,
        )
        last_count = next_count
        if seed_turns >= openhands._LOCAL_CONDENSER_KEEP_FIRST and (
            next_count >= target_low - 256
        ):
            return
        assert next_count < target_low, (
            "harness precondition failed: seed history would cross the target "
            f"before final padding; next_count={next_count}, target_low={target_low}"
        )
        _run_turn(conversation, _SEED_TEXT)

    assert False, (
        "harness precondition failed: seed history did not reach the target "
        f"band after {_MAX_SEED_TURNS} turns; observed_count={last_count}, "
        f"target_band=[{target_low - 256}, {target_low})"
    )


def _pad_to_band(
    *,
    conversation: Conversation,
    llm: _RecordingLLM,
    lower_exclusive: int,
    upper_inclusive: int,
    label: str,
) -> tuple[str, int]:
    text = "Final measured turn."
    count = _prepared_count(conversation=conversation, llm=llm, candidate=text)
    pad_steps = 0
    while count <= lower_exclusive and pad_steps < _MAX_PAD_STEPS:
        text += _PAD_UNIT
        count = _prepared_count(conversation=conversation, llm=llm, candidate=text)
        pad_steps += 1

    assert count > lower_exclusive, (
        f"harness precondition failed: {label} padding did not reach the "
        f"target band after {pad_steps} steps; observed_count={count}, "
        f"target_band=({lower_exclusive}, {upper_inclusive}]"
    )
    assert count <= upper_inclusive, (
        f"harness precondition failed: {label} prepared count {count} not in "
        f"({lower_exclusive}, {upper_inclusive}]"
    )
    return text, count


@pytest.mark.parametrize(("window", "reserve"), [(16384, 4096), (32768, 8192)])
@pytest.mark.parametrize("case", ["above-boundary", "below-boundary"])
def test_local_context_headroom_constrains_agent_turns(window, reserve, case, tmp_path):
    expected_boundary = math.floor((window - reserve) / _DIVERGENCE_FACTOR)
    source_boundary = openhands._local_condenser_max_tokens(window)
    # Historical shipped condenser boundary, used only as the regression band ceiling.
    old_boundary = window * 11 // 16
    assert math.ceil(expected_boundary * _DIVERGENCE_FACTOR) + reserve == window

    llm = _recording_llm(window=window, reserve=reserve)
    conversation = _conversation(
        llm=llm,
        boundary=source_boundary,
        tmp_path=tmp_path / f"{window}-{case}",
    )
    try:
        if case == "above-boundary":
            lower = expected_boundary
            upper = old_boundary
        else:
            lower = expected_boundary - 512
            upper = expected_boundary - 1

        _seed_history(conversation=conversation, llm=llm, target_low=lower)
        final_message, prepared_count = _pad_to_band(
            conversation=conversation,
            llm=llm,
            lower_exclusive=lower,
            upper_inclusive=upper,
            label=case,
        )

        start_index = len(llm.records)
        conversation.send_message(final_message)
        assert _prepared_count(conversation=conversation, llm=llm) == prepared_count
        asyncio.run(conversation.arun())

        observed = llm.records[start_index:]
        assert observed, "expected the final turn to reach the LLM spy"
        if case == "above-boundary":
            assert observed[0].kind == "condenser", (
                "expected condensation before any agent-turn request; observed "
                f"{observed}"
            )
            assert any(record.kind == "agent" for record in observed), (
                "expected an agent turn after condensation"
            )
        else:
            assert observed[0].kind == "agent", (
                "expected below-boundary prompt to reach the agent without "
                f"condensation; observed {observed}"
            )
            assert all(record.kind == "agent" for record in observed), (
                f"unexpected condenser call below boundary; observed {observed}"
            )

        for record in llm.records:
            if record.kind != "agent":
                continue
            assert (
                math.ceil(record.estimated_input * _DIVERGENCE_FACTOR)
                + record.max_output_tokens
                <= window
            ), (
                "agent turn exceeded local tokenizer-divergence headroom: "
                f"{record}, window={window}"
            )
    finally:
        conversation.close()
