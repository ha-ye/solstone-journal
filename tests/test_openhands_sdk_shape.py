# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import inspect
import os
import threading
from pathlib import Path

from pydantic import Field

from solstone.think.providers.openhands import (
    _OPENHANDS_CONVERSATION_LOGGER,
    _OPENHANDS_MAX_ITERATIONS_PREFIX,
)
from tests._logging_isolation import preserve_global_logging

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from openhands.sdk.tool import (  # noqa: E402
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
)
from openhands.sdk.tool.schema import Action, Observation  # noqa: E402


class _ShapeAction(Action):
    value: str = Field(default="")


class _ShapeObservation(Observation):
    pass


class _ShapeTool(ToolDefinition[_ShapeAction, _ShapeObservation]):
    name = "sdk_shape"

    @classmethod
    def create(cls, *args, **kwargs):
        del args, kwargs
        return []


class _InterruptingExecutor(ToolExecutor):
    def __init__(self) -> None:
        self.call_count = 0
        self.worker_thread_id: int | None = None

    def __call__(self, action, conversation=None):
        del action
        self.call_count += 1
        self.worker_thread_id = threading.get_ident()
        assert conversation is not None
        conversation.interrupt()
        return _ShapeObservation.from_text("interrupted", is_error=True)


class _RaisingExecutor(ToolExecutor):
    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self, action, conversation=None):
        del action, conversation
        self.call_count += 1
        raise RuntimeError("executor boom")


def test_local_conversation_methods_match_provider_await_sites(monkeypatch):
    monkeypatch.setenv("OPENHANDS_SUPPRESS_BANNER", "1")
    with preserve_global_logging():
        from openhands.sdk.conversation.impl.local_conversation import (
            LocalConversation,
        )

        assert inspect.iscoroutinefunction(LocalConversation.arun) is True
        assert inspect.iscoroutinefunction(LocalConversation.send_message) is False


def test_local_conversation_logger_and_max_iterations_prefix_shape(monkeypatch):
    monkeypatch.setenv("OPENHANDS_SUPPRESS_BANNER", "1")
    with preserve_global_logging():
        from openhands.sdk.conversation.impl import local_conversation

        assert local_conversation.logger.name == _OPENHANDS_CONVERSATION_LOGGER
        source_path = Path(local_conversation.__file__)
        source = source_path.read_text(encoding="utf-8")

    assert _OPENHANDS_MAX_ITERATIONS_PREFIX in source


def _shape_tool(executor):
    return _ShapeTool(
        description="SDK shape test tool",
        action_type=_ShapeAction,
        observation_type=_ShapeObservation,
        executor=executor,
        annotations=ToolAnnotations(title="sdk_shape"),
    )


def _tool_call_message(call_id: str = "call-1"):
    from openhands.sdk.llm import Message, MessageToolCall

    return Message(
        role="assistant",
        content=[],
        tool_calls=[
            MessageToolCall(
                id=call_id,
                name="sdk_shape",
                arguments='{"value":"ok"}',
                origin="completion",
            )
        ],
    )


def _assistant_message(text: str):
    from openhands.sdk.llm import Message, TextContent

    return Message(role="assistant", content=[TextContent(text=text)])


def _conversation(llm, tool, tmp_path, callbacks=None):
    from openhands.sdk import Agent, Conversation
    from openhands.sdk.tool.registry import register_tool
    from openhands.sdk.tool.spec import Tool

    register_tool("sdk_shape", tool)
    agent = Agent(
        llm=llm,
        tools=[Tool(name="sdk_shape")],
        include_default_tools=[],
        system_prompt="system",
    )
    return Conversation(
        agent=agent,
        workspace=str(tmp_path),
        persistence_dir=str(tmp_path / "history"),
        callbacks=callbacks or [],
        visualizer=None,
        stuck_detection=False,
    )


def test_interrupt_from_tool_worker_stops_before_next_llm_completion(tmp_path):
    from openhands.sdk.testing import TestLLM

    executor = _InterruptingExecutor()
    llm = TestLLM.from_messages(
        [_tool_call_message(), _assistant_message("should not be consumed")]
    )
    conversation = _conversation(llm, _shape_tool(executor), tmp_path)
    conversation.send_message("go")

    asyncio.run(conversation.arun())

    assert executor.call_count == 1
    assert executor.worker_thread_id != threading.get_ident()
    assert llm.call_count == 1
    conversation.close()


def test_executor_exception_becomes_agent_error_and_loop_continues(tmp_path):
    from openhands.sdk.event.llm_convertible import AgentErrorEvent
    from openhands.sdk.testing import TestLLM

    executor = _RaisingExecutor()
    events = []
    llm = TestLLM.from_messages([_tool_call_message(), _assistant_message("done")])
    conversation = _conversation(
        llm,
        _shape_tool(executor),
        tmp_path,
        callbacks=[events.append],
    )
    conversation.send_message("go")

    asyncio.run(conversation.arun())

    assert executor.call_count == 1
    assert llm.call_count == 2
    assert any(isinstance(event, AgentErrorEvent) for event in events)
    conversation.close()


def test_agent_default_concurrency_and_undeclared_tool_mutex():
    from openhands.sdk import Agent
    from openhands.sdk.agent.parallel_executor import ParallelToolExecutor
    from openhands.sdk.event.llm_convertible import ActionEvent
    from openhands.sdk.llm import MessageToolCall
    from openhands.sdk.testing import TestLLM

    tool = _shape_tool(_InterruptingExecutor())
    agent = Agent(
        llm=TestLLM.from_messages([]),
        tools=[],
        include_default_tools=[],
        system_prompt="system",
    )
    action_event = ActionEvent(
        thought=[],
        tool_name="sdk_shape",
        tool_call_id="call-1",
        tool_call=MessageToolCall(
            id="call-1",
            name="sdk_shape",
            arguments='{"value":"ok"}',
            origin="completion",
        ),
        llm_response_id="response-1",
        action=_ShapeAction(value="ok"),
    )

    assert agent.tool_concurrency_limit == 1
    # Private helper pin: undeclared resources resolve to the same tool-wide
    # mutex key that serializes same-tool calls when concurrency is raised.
    resources = ParallelToolExecutor._extract_declared_resources(action_event, tool)
    assert resources is not None
    assert resources.declared is False
    assert ParallelToolExecutor._resolve_lock_keys(resources, tool) == [
        "tool:sdk_shape"
    ]
