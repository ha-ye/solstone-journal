# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import time

import pytest

from solstone.think.cogitate_policy import CogitatePolicy
from solstone.think.providers import local_admission, openhands
from solstone.think.providers.local_admission import LocalSlotLease
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
    slot_lease=None,
):
    policy = CogitatePolicy(allowed_roots=[tmp_path], access_tier="normal")
    tools, executor = openhands._build_sol_tools(
        policy=policy,
        callback=JSONEventCallback(events.append),
        read_call_budget=read_call_budget,
        slot_lease=slot_lease,
    )
    assert len(tools) == 1
    assert tools[0].name == "sol"
    return tools[0], executor


def _isolated_lease(monkeypatch, tmp_path, *, timeout_s: float = 1.0):
    monkeypatch.setattr(
        local_admission,
        "_admission_dir",
        lambda: tmp_path / "local-inference-admission",
    )
    permit = local_admission.acquire_local_slot(1, 0.1)
    return LocalSlotLease(
        capacity=1,
        deadline=time.monotonic() + timeout_s,
        permit=permit,
    )


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


def test_unexpected_command_exception_reacquires_before_escaping(
    fake_openhands,
    fixed_time,
    tmp_path,
    monkeypatch,
):
    events: list[dict] = []
    lease = _isolated_lease(monkeypatch, tmp_path)
    tool, _executor = _sol_tool_and_executor(
        tmp_path=tmp_path,
        events=events,
        slot_lease=lease,
    )

    def fail_command(_argv: list[str]):
        raise RuntimeError("boom")

    monkeypatch.setattr(openhands, "_run_command", fail_command)

    with pytest.raises(RuntimeError, match="boom"):
        tool(tool.action_from_arguments({"command": "sol call journal search x"}))

    with pytest.raises(local_admission.LocalAdmissionTimeout):
        local_admission.acquire_local_slot(1, 0.03)
    lease.close()
    with local_admission.acquire_local_slot(1, 0.1) as permit:
        assert permit.slot_index == 0


def test_reacquire_timeout_sets_terminal_marker_and_interrupts_conversation(
    fake_openhands,
    fixed_time,
    tmp_path,
    monkeypatch,
):
    events: list[dict] = []
    lease = _isolated_lease(monkeypatch, tmp_path, timeout_s=0.03)
    tool, executor = _sol_tool_and_executor(
        tmp_path=tmp_path,
        events=events,
        slot_lease=lease,
    )
    holder = None

    def run_and_hold(_argv: list[str]):
        nonlocal holder
        holder = local_admission.acquire_local_slot(1, 0.1)
        return {"text": "ran", "is_error": False}

    conversation = fake_openhands.Conversation()
    monkeypatch.setattr(openhands, "_run_command", run_and_hold)

    observation = tool(
        tool.action_from_arguments({"command": "sol call journal search x"}),
        conversation,
    )

    try:
        terminal = executor.take_terminal_error()
        assert observation.is_error is True
        assert isinstance(terminal, local_admission.LocalAdmissionTimeout)
        assert terminal.reason_code == "local_queue_timeout"
        assert conversation.interrupted is True
    finally:
        if holder is not None:
            holder.release()
        lease.close()

    with local_admission.acquire_local_slot(1, 0.1) as permit:
        assert permit.slot_index == 0


def test_unexpected_reacquire_exception_sets_terminal_marker_and_interrupts(
    fake_openhands,
    fixed_time,
    tmp_path,
    monkeypatch,
):
    events: list[dict] = []
    lease = _isolated_lease(monkeypatch, tmp_path)
    tool, executor = _sol_tool_and_executor(
        tmp_path=tmp_path,
        events=events,
        slot_lease=lease,
    )
    terminal = RuntimeError("flock exploded")

    monkeypatch.setattr(
        openhands,
        "_run_command",
        lambda _argv: {"text": "ran", "is_error": False},
    )

    def fail_reacquire() -> None:
        raise terminal

    monkeypatch.setattr(lease, "reacquire", fail_reacquire)
    conversation = fake_openhands.Conversation()

    observation = tool(
        tool.action_from_arguments({"command": "sol call journal search x"}),
        conversation,
    )

    assert observation.is_error is True
    assert observation.text == "flock exploded"
    assert executor.take_terminal_error() is terminal
    assert conversation.interrupted is True
    lease.close()
    with local_admission.acquire_local_slot(1, 0.1) as permit:
        assert permit.slot_index == 0


def test_reacquire_cancel_is_recoverable_observation_not_terminal(
    fake_openhands,
    fixed_time,
    tmp_path,
    monkeypatch,
):
    events: list[dict] = []
    lease = _isolated_lease(monkeypatch, tmp_path)
    tool, executor = _sol_tool_and_executor(
        tmp_path=tmp_path,
        events=events,
        slot_lease=lease,
    )

    def run_and_cancel(_argv: list[str]):
        lease.cancel_pending_reacquire()
        return {"text": "ran before cancel", "is_error": False}

    monkeypatch.setattr(openhands, "_run_command", run_and_cancel)

    observation = tool(
        tool.action_from_arguments({"command": "sol call journal search x"})
    )

    assert observation.text == "ran before cancel"
    assert observation.is_error is False
    assert executor.take_terminal_error() is None
    lease.close()
    with local_admission.acquire_local_slot(1, 0.1) as permit:
        assert permit.slot_index == 0


@pytest.mark.parametrize(
    "command_result",
    [
        {"text": "exit_code: 2", "is_error": True},
        {"text": "timeout: command exceeded 30s", "is_error": True},
    ],
)
def test_command_error_results_reacquire_before_returning(
    fake_openhands,
    fixed_time,
    tmp_path,
    monkeypatch,
    command_result,
):
    events: list[dict] = []
    lease = _isolated_lease(monkeypatch, tmp_path)
    tool, executor = _sol_tool_and_executor(
        tmp_path=tmp_path,
        events=events,
        slot_lease=lease,
    )
    monkeypatch.setattr(openhands, "_run_command", lambda _argv: command_result)

    observation = tool(
        tool.action_from_arguments({"command": "sol call journal search x"})
    )

    assert observation.text == command_result["text"]
    assert observation.is_error is True
    assert executor.take_terminal_error() is None
    with pytest.raises(local_admission.LocalAdmissionTimeout):
        local_admission.acquire_local_slot(1, 0.03)
    lease.close()
