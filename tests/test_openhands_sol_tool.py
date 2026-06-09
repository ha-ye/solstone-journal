# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.think.cogitate_policy import CogitatePolicy
from solstone.think.providers import openhands
from solstone.think.providers.shared import JSONEventCallback
from tests.openhands_fakes import install_fake_openhands


@pytest.fixture
def fake_openhands(monkeypatch):
    return install_fake_openhands(monkeypatch)


@pytest.fixture
def fixed_time(monkeypatch):
    monkeypatch.setattr(openhands, "now_ms", lambda: 123456)


def _sol_tool_and_executor(
    *,
    tmp_path,
    events: list[dict],
    read_call_budget: int = 200,
):
    policy = CogitatePolicy(allowed_roots=[tmp_path], access_tier="normal")
    tools, executor = openhands._build_sol_tools(
        policy=policy,
        callback=JSONEventCallback(events.append),
        read_call_budget=read_call_budget,
    )
    assert len(tools) == 1
    assert tools[0].name == "sol"
    return tools[0], executor


def test_read_only_allowed_sol_call_returns_non_error_observation(
    fake_openhands,
    fixed_time,
    tmp_path,
    monkeypatch,
):
    events: list[dict] = []
    tool, executor = _sol_tool_and_executor(
        tmp_path=tmp_path,
        events=events,
    )

    seen_argv: list[list[str]] = []

    def fake_run(argv: list[str]):
        seen_argv.append(argv)
        return {"text": f"ran: {' '.join(argv)}", "is_error": False}

    monkeypatch.setattr(
        openhands,
        "_run_command",
        fake_run,
    )

    observation = tool(
        tool.action_from_arguments({"command": "sol call journal search x"})
    )

    assert observation.text == "ran: sol call journal search x"
    assert observation.is_error is False
    assert seen_argv == [["sol", "call", "journal", "search", "x"]]
    assert executor.read_call_count == 1
    assert events == []


def test_read_only_policy_deny_is_recoverable_observation(
    fake_openhands,
    fixed_time,
    tmp_path,
    monkeypatch,
):
    events: list[dict] = []
    tool, executor = _sol_tool_and_executor(
        tmp_path=tmp_path,
        events=events,
    )
    monkeypatch.setattr(
        openhands,
        "_run_command",
        lambda _argv: pytest.fail("denied commands must not run"),
    )

    observation = tool(tool.action_from_arguments({"command": "rm -rf journal"}))

    assert observation.is_error is True
    assert observation.text.startswith("policy_deny:")
    assert executor.read_call_count == 0
    assert events == []


def test_read_call_budget_overflow_emits_once_and_denies_recoverably(
    fake_openhands,
    fixed_time,
    tmp_path,
    monkeypatch,
):
    events: list[dict] = []
    tool, executor = _sol_tool_and_executor(
        tmp_path=tmp_path,
        events=events,
        read_call_budget=1,
    )
    monkeypatch.setattr(
        openhands,
        "_run_command",
        lambda argv: {"text": f"ran: {' '.join(argv)}", "is_error": False},
    )
    action = tool.action_from_arguments({"command": "sol call journal search x"})

    first = tool(action)
    second = tool(action)
    third = tool(action)

    assert first.is_error is False
    assert first.text == "ran: sol call journal search x"
    assert second.is_error is True
    assert second.text.startswith("tool_budget_exhausted:")
    assert third.is_error is True
    assert third.text.startswith("tool_budget_exhausted:")
    assert executor.read_call_count == 3
    assert events == [
        {
            "event": "tool_budget_exhausted",
            "tool": "sol",
            "budget": 1,
            "count": 2,
            "ts": 123456,
        }
    ]
