# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import asyncio
import base64
import functools
import importlib
import io
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from solstone.think.models import GPT_5
from solstone.think.providers import bundled
from solstone.think.providers.cli import ThinkingAggregator
from solstone.think.providers.shared import JSONEventCallback


def _openai_provider():
    return importlib.reload(importlib.import_module("solstone.think.providers.openai"))


@contextmanager
def _patched_openai_runner(runner_cls):
    with (
        patch("solstone.think.providers.openai.CLIRunner", runner_cls),
        patch(
            "solstone.think.providers.openai.bundled.resolve_bundled_binary",
            return_value=Path("/usr/bin/codex"),
        ),
    ):
        yield


def _assert_write_mode_sandbox():
    provider = _openai_provider()

    class MockCLIRunner:
        last_instance = None

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.cmd = kwargs["cmd"]
            self.prompt_text = kwargs["prompt_text"]
            self.cli_session_id = "test-session-id"
            self.run = AsyncMock(return_value="test result")
            MockCLIRunner.last_instance = self

    with _patched_openai_runner(MockCLIRunner):
        asyncio.run(
            provider.run_cogitate(
                {"prompt": "hello", "model": GPT_5, "write": True},
                lambda e: None,
            )
        )

    cmd = MockCLIRunner.last_instance.cmd
    assert "-s" in cmd
    s_idx = cmd.index("-s")
    assert cmd[s_idx + 1] == "workspace-write"


def _make_test_harness():
    """Create a callback/aggregator pair for testing _translate_codex."""
    events = []
    cb = JSONEventCallback(lambda e: events.append(e))
    aggregator = ThinkingAggregator(cb, GPT_5)
    return events, cb, aggregator


def _make_openai_response(text="Hello"):
    response = MagicMock()
    response.output_text = text
    response.status = "completed"
    response.incomplete_details = None
    response.usage = None
    response.output = []
    return response


def _png_bytes(size: tuple[int, int] = (4, 3)) -> bytes:
    image = Image.new("RGB", size, color="red")
    buf = io.BytesIO()
    image.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def _decode_openai_image_part(part):
    prefix, b64 = part["image_url"].split(",", 1)
    assert prefix.startswith("data:")
    assert prefix.endswith(";base64")
    return prefix[5:-7], Image.open(io.BytesIO(base64.b64decode(b64)))


class TestParseModelEffort:
    def test_no_suffix(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("gpt-5.2") == ("gpt-5.2", None)

    def test_high_suffix(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("gpt-5.2-high") == ("gpt-5.2", "high")

    def test_low_suffix(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("gpt-5.2-low") == ("gpt-5.2", "low")

    def test_medium_suffix(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("gpt-5.2-medium") == ("gpt-5.2", "medium")

    def test_none_suffix(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("gpt-5.2-none") == ("gpt-5.2", "none")

    def test_xhigh_suffix(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("gpt-5.2-xhigh") == ("gpt-5.2", "xhigh")

    def test_unknown_suffix_not_stripped(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("gpt-5.2-turbo") == (
            "gpt-5.2-turbo",
            None,
        )

    def test_non_gpt_model_passthrough(self):
        provider = _openai_provider()
        assert provider._parse_model_effort("claude-sonnet-4-5") == (
            "claude-sonnet-4-5",
            None,
        )


class TestTranslateCodex:
    def test_thread_started_returns_id(self):
        provider = _openai_provider()
        events, cb, aggregator = _make_test_harness()
        event = {"type": "thread.started", "thread_id": "abc-123"}

        result = provider._translate_codex(event, aggregator, cb)

        assert result == "abc-123"
        assert events == []

    def test_turn_started_ignored(self):
        provider = _openai_provider()
        events, cb, aggregator = _make_test_harness()
        event = {"type": "turn.started"}

        result = provider._translate_codex(event, aggregator, cb)

        assert result is None
        assert events == []

    def test_reasoning_emits_thinking(self):
        provider = _openai_provider()
        events, cb, aggregator = _make_test_harness()
        event = {
            "type": "item.completed",
            "item": {"id": "r1", "type": "reasoning", "text": "Let me think..."},
        }

        result = provider._translate_codex(event, aggregator, cb)

        assert result is None
        assert len(events) == 1
        assert events[0]["event"] == "thinking"
        assert events[0]["summary"] == "Let me think..."
        assert events[0]["raw"] == [event]

    def test_agent_message_accumulates(self):
        provider = _openai_provider()
        events, cb, aggregator = _make_test_harness()
        event1 = {
            "type": "item.completed",
            "item": {"id": "m1", "type": "agent_message", "text": "Hello "},
        }
        event2 = {
            "type": "item.completed",
            "item": {"id": "m2", "type": "agent_message", "text": "world"},
        }

        provider._translate_codex(event1, aggregator, cb)
        provider._translate_codex(event2, aggregator, cb)

        assert events == []
        assert aggregator.has_content

    def test_command_started_emits_tool_start(self):
        provider = _openai_provider()
        events, cb, aggregator = _make_test_harness()
        event = {
            "type": "item.started",
            "item": {
                "id": "cmd1",
                "type": "command_execution",
                "command": "echo hi",
            },
        }

        result = provider._translate_codex(event, aggregator, cb)

        assert result is None
        assert len(events) == 1
        assert events[0]["event"] == "tool_start"
        assert events[0]["tool"] == "bash"
        assert events[0]["args"] == {"command": "echo hi"}
        assert events[0]["call_id"] == "cmd1"
        assert events[0]["raw"] == [event]

    def test_command_started_flushes_thinking(self):
        provider = _openai_provider()
        events, cb, aggregator = _make_test_harness()
        aggregator.accumulate("I should run a command now")
        event = {
            "type": "item.started",
            "item": {
                "id": "cmd1",
                "type": "command_execution",
                "command": "echo hi",
            },
        }

        provider._translate_codex(event, aggregator, cb)

        assert len(events) == 2
        assert events[0]["event"] == "thinking"
        assert events[1]["event"] == "tool_start"
        assert events[0]["raw"] == [event]
        assert events[1]["raw"] == [event]

    def test_command_completed_emits_tool_end(self):
        provider = _openai_provider()
        events, cb, aggregator = _make_test_harness()
        event = {
            "type": "item.completed",
            "item": {
                "id": "cmd1",
                "type": "command_execution",
                "command": "echo hi",
                "aggregated_output": "hi\n",
            },
        }

        result = provider._translate_codex(event, aggregator, cb)

        assert result is None
        assert len(events) == 1
        assert events[0]["event"] == "tool_end"
        assert events[0]["tool"] == "bash"
        assert events[0]["args"] == {"command": "echo hi"}
        assert events[0]["result"] == "hi\n"
        assert events[0]["call_id"] == "cmd1"
        assert events[0]["raw"] == [event]

    def test_turn_completed_captures_usage(self):
        provider = _openai_provider()
        events, cb, aggregator = _make_test_harness()
        usage_holder = [{}]
        translate = functools.partial(
            provider._translate_codex, usage_holder=usage_holder
        )
        event = {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 5,
                "output_tokens": 2,
            },
        }

        result = translate(event, aggregator, cb)

        assert result is None
        assert usage_holder[0] == {
            "input_tokens": 10,
            "cached_input_tokens": 5,
            "output_tokens": 2,
        }
        assert events == []

    def test_turn_completed_without_holder(self):
        provider = _openai_provider()
        events, cb, aggregator = _make_test_harness()
        event = {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 5,
                "output_tokens": 2,
            },
        }

        result = provider._translate_codex(event, aggregator, cb)

        assert result is None
        assert events == []

    def test_unknown_event_ignored(self):
        provider = _openai_provider()
        events, cb, aggregator = _make_test_harness()
        event = {"type": "something.unknown", "data": "whatever"}

        result = provider._translate_codex(event, aggregator, cb)

        assert result is None
        assert events == []


class TestRunCogitate:
    def test_basic_command_construction(self):
        provider = _openai_provider()
        events = []

        class MockCLIRunner:
            last_instance = None

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.cmd = kwargs["cmd"]
                self.prompt_text = kwargs["prompt_text"]
                self.cli_session_id = "test-session-id"
                self.run = AsyncMock(return_value="test result")
                MockCLIRunner.last_instance = self

        with _patched_openai_runner(MockCLIRunner):
            result = asyncio.run(
                provider.run_cogitate(
                    {"prompt": "hello", "model": GPT_5}, events.append
                )
            )

        assert result == "test result"
        assert MockCLIRunner.last_instance is not None
        assert MockCLIRunner.last_instance.cmd[:6] == [
            "/usr/bin/codex",
            "exec",
            "--json",
            "-s",
            "read-only",
            "-m",
        ]
        # Model should have effort suffix stripped for codex CLI
        expected_model, expected_effort = provider._parse_model_effort(GPT_5)
        assert MockCLIRunner.last_instance.cmd[6] == expected_model
        # Effort should be forwarded via -c flag
        if expected_effort:
            assert "-c" in MockCLIRunner.last_instance.cmd
            c_idx = MockCLIRunner.last_instance.cmd.index("-c")
            assert (
                MockCLIRunner.last_instance.cmd[c_idx + 1]
                == f'model_reasoning_effort="{expected_effort}"'
            )
        assert MockCLIRunner.last_instance.cmd[-1] == "-"

    def test_write_mode_sandbox(self):
        _assert_write_mode_sandbox()

    def test_resume_command(self):
        provider = _openai_provider()
        events = []

        class MockCLIRunner:
            last_instance = None

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.cmd = kwargs["cmd"]
                self.prompt_text = kwargs["prompt_text"]
                self.cli_session_id = "test-session-id"
                self.run = AsyncMock(return_value="test result")
                MockCLIRunner.last_instance = self

        with _patched_openai_runner(MockCLIRunner):
            asyncio.run(
                provider.run_cogitate(
                    {
                        "prompt": "hello",
                        "model": GPT_5,
                        "session_id": "thread-abc",
                    },
                    events.append,
                )
            )

        assert MockCLIRunner.last_instance is not None
        assert "resume" in MockCLIRunner.last_instance.cmd
        assert "thread-abc" in MockCLIRunner.last_instance.cmd

    def test_run_cogitate_passes_cwd_to_cli_runner(self):
        provider = _openai_provider()
        events = []

        class MockCLIRunner:
            last_instance = None

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.cmd = kwargs["cmd"]
                self.prompt_text = kwargs["prompt_text"]
                self.cli_session_id = "test-session-id"
                self.run = AsyncMock(return_value="test result")
                MockCLIRunner.last_instance = self

        with _patched_openai_runner(MockCLIRunner):
            asyncio.run(
                provider.run_cogitate(
                    {
                        "prompt": "hello",
                        "model": GPT_5,
                        "cwd": "/fake/journal",
                    },
                    events.append,
                )
            )

        assert MockCLIRunner.last_instance is not None
        assert MockCLIRunner.last_instance.kwargs["cwd"] == Path("/fake/journal")

    def test_system_instruction_prepended(self):
        provider = _openai_provider()
        events = []

        class MockCLIRunner:
            last_instance = None

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.cmd = kwargs["cmd"]
                self.prompt_text = kwargs["prompt_text"]
                self.cli_session_id = "test-session-id"
                self.run = AsyncMock(return_value="test result")
                MockCLIRunner.last_instance = self

        with _patched_openai_runner(MockCLIRunner):
            asyncio.run(
                provider.run_cogitate(
                    {
                        "prompt": "hello",
                        "model": GPT_5,
                        "system_instruction": "You are a system",
                    },
                    events.append,
                )
            )

        assert MockCLIRunner.last_instance is not None
        assert MockCLIRunner.last_instance.prompt_text.startswith(
            "You are a system\n\n"
        )

    def test_finish_event_emitted(self):
        provider = _openai_provider()
        events = []

        class MockCLIRunner:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.cmd = kwargs["cmd"]
                self.prompt_text = kwargs["prompt_text"]
                self.cli_session_id = "test-session-id"
                self.run = AsyncMock(return_value="result text")

        with _patched_openai_runner(MockCLIRunner):
            result = asyncio.run(
                provider.run_cogitate(
                    {"prompt": "hello", "model": GPT_5}, events.append
                )
            )

        assert result == "result text"
        assert events[-1]["event"] == "finish"
        assert events[-1]["result"] == "result text"
        assert events[-1]["cli_session_id"] == "test-session-id"

    def test_finish_event_without_session_id(self):
        provider = _openai_provider()
        events = []

        class MockCLIRunner:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.cmd = kwargs["cmd"]
                self.prompt_text = kwargs["prompt_text"]
                self.cli_session_id = None  # no session ID
                self.run = AsyncMock(return_value="result text")

        with _patched_openai_runner(MockCLIRunner):
            asyncio.run(
                provider.run_cogitate(
                    {"prompt": "hello", "model": GPT_5}, events.append
                )
            )

        finish = events[-1]
        assert finish["event"] == "finish"
        assert finish["result"] == "result text"
        assert "cli_session_id" not in finish

    def test_finish_event_includes_usage(self):
        provider = _openai_provider()
        events = []
        expected_usage = {"input_tokens": 100, "output_tokens": 50}

        class MockCLIRunner:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.cmd = kwargs["cmd"]
                self.prompt_text = kwargs["prompt_text"]
                self.cli_session_id = "sid"
                # Simulate translate populating usage_holder via the translate callback
                translate_fn = kwargs["translate"]
                agg = kwargs["aggregator"]
                cb = kwargs["callback"]
                turn_event = {"type": "turn.completed", "usage": expected_usage}
                translate_fn(turn_event, agg, cb)
                self.run = AsyncMock(return_value="done")

        with _patched_openai_runner(MockCLIRunner):
            asyncio.run(
                provider.run_cogitate(
                    {"prompt": "hello", "model": GPT_5}, events.append
                )
            )

        finish = events[-1]
        assert finish["event"] == "finish"
        assert finish["usage"] == expected_usage

    def test_error_emits_event_and_reraises(self):
        provider = _openai_provider()
        events = []

        class MockCLIRunner:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.cmd = kwargs["cmd"]
                self.prompt_text = kwargs["prompt_text"]
                self.cli_session_id = "test-session-id"
                self.run = AsyncMock(side_effect=RuntimeError("boom"))

        exc = None
        with _patched_openai_runner(MockCLIRunner):
            try:
                asyncio.run(
                    provider.run_cogitate(
                        {"prompt": "hello", "model": GPT_5}, events.append
                    )
                )
            except RuntimeError as caught:
                exc = caught

        assert exc is not None
        assert str(exc) == "boom"
        assert getattr(exc, "_evented", False) is True
        assert events[-1]["event"] == "error"
        assert events[-1]["error"] == "boom"

    def test_resolve_error_emits_event_and_reraises(self):
        provider = _openai_provider()
        events = []
        exc = None
        hint = "sol call settings providers install openai"

        with patch(
            "solstone.think.providers.openai.bundled.resolve_bundled_binary",
            side_effect=bundled.CogitateProviderNotInstalled(hint),
        ):
            try:
                asyncio.run(
                    provider.run_cogitate(
                        {"prompt": "hello", "model": GPT_5}, events.append
                    )
                )
            except bundled.CogitateProviderNotInstalled as caught:
                exc = caught

        assert exc is not None
        assert getattr(exc, "_evented", False) is True
        assert events[-1]["event"] == "error"
        assert events[-1]["provider"] == "openai"
        assert hint in events[-1]["error"]


class TestBuildInput:
    def test_string_input(self):
        provider = _openai_provider()
        assert provider._build_input("hello") == ("hello", None)

    def test_string_with_system(self):
        provider = _openai_provider()
        assert provider._build_input("hello", "sys") == ("hello", "sys")

    def test_list_of_parts(self):
        provider = _openai_provider()
        assert provider._build_input(["part1", "part2"]) == ("part1\npart2", None)

    def test_message_list(self):
        provider = _openai_provider()
        message = [{"role": "user", "content": "hi"}]
        assert provider._build_input(message) == (message, None)

    def test_non_string(self):
        provider = _openai_provider()
        assert provider._build_input(42) == ("42", None)


class TestExtractUsage:
    def test_extract_usage_with_details(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_response.usage.total_tokens = 150
        mock_response.usage.input_tokens_details.cached_tokens = 20
        mock_response.usage.output_tokens_details.reasoning_tokens = 10

        assert provider._extract_usage(mock_response) == {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "cached_tokens": 20,
            "reasoning_tokens": 10,
        }

    def test_extract_usage_missing(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.usage = None
        assert provider._extract_usage(mock_response) is None

    def test_extract_usage_without_details(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50
        mock_response.usage.total_tokens = 150
        mock_response.usage.input_tokens_details = None
        mock_response.usage.output_tokens_details = None

        assert provider._extract_usage(mock_response) == {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        }


class TestNormalizeFinishReason:
    def test_completed(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.status = "completed"
        assert provider._normalize_finish_reason(mock_response) == "stop"

    def test_incomplete_max_tokens(self):
        provider = _openai_provider()
        incomplete_details = MagicMock()
        incomplete_details.reason = "max_output_tokens"
        mock_response = MagicMock()
        mock_response.status = "incomplete"
        mock_response.incomplete_details = incomplete_details
        assert provider._normalize_finish_reason(mock_response) == "max_tokens"

    def test_incomplete_content_filter(self):
        provider = _openai_provider()
        incomplete_details = MagicMock()
        incomplete_details.reason = "content_filter"
        mock_response = MagicMock()
        mock_response.status = "incomplete"
        mock_response.incomplete_details = incomplete_details
        assert provider._normalize_finish_reason(mock_response) == "content_filter"

    def test_incomplete_without_details(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.status = "incomplete"
        mock_response.incomplete_details = None
        assert provider._normalize_finish_reason(mock_response) == "max_tokens"

    def test_failed(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.status = "failed"
        assert provider._normalize_finish_reason(mock_response) == "error"


class TestExtractThinking:
    def test_reasoning_summary_extracted(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        reasoning_item = MagicMock()
        reasoning_item.type = "reasoning"
        summary = MagicMock()
        summary.text = "Let me think..."
        reasoning_item.summary = [summary]
        mock_response.output = [reasoning_item]

        assert provider._extract_thinking(mock_response) == [
            {"summary": "Let me think..."},
        ]

    def test_no_reasoning_items(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.output = [MagicMock(type="message")]
        assert provider._extract_thinking(mock_response) is None

    def test_empty_output(self):
        provider = _openai_provider()
        mock_response = MagicMock()
        mock_response.output = []
        assert provider._extract_thinking(mock_response) is None


class TestRunGenerate:
    def test_basic_generate(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello world"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_response.usage.input_tokens_details = None
        mock_response.usage.output_tokens_details = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            result = provider.run_generate("hello", model="gpt-5.2")

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["model"] == "gpt-5.2"
        assert called_kwargs["input"] == "hello"
        assert called_kwargs["max_output_tokens"] == 16384
        assert "instructions" not in called_kwargs
        assert result["text"] == "Hello world"
        assert result["finish_reason"] == "stop"
        assert result["thinking"] is None
        assert result["usage"] == {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        }

    def test_run_generate_records_resolved_model_version(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = _make_openai_response("Hello world")
        mock_response.model = "gpt-5-2025-08-07"
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_response.usage.input_tokens_details = None
        mock_response.usage.output_tokens_details = None
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            result = provider.run_generate("hello", model="gpt-5")

        assert result["model"] == "gpt-5-2025-08-07"
        assert result["usage"]["model_version"] == "gpt-5-2025-08-07"

    def test_run_generate_model_version_falls_back_to_requested(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = _make_openai_response("Hello world")
        mock_response.model = MagicMock()
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_response.usage.input_tokens_details = None
        mock_response.usage.output_tokens_details = None
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            result = provider.run_generate("hello", model="gpt-5")

        assert result["model"] == "gpt-5"
        assert "model_version" not in result["usage"]

    def test_structured_messages_passthrough(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello world"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ]

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate(messages, system_instruction="Be helpful")

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["input"] == messages
        assert called_kwargs["instructions"] == "Be helpful"

    def test_image_parts_build_structured_input(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock(return_value=_make_openai_response())
        image = Image.new("RGB", (5, 4), color="blue")

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate(["before", image, "after"], model="gpt-5.2")

        called_kwargs = mock_client.responses.create.call_args.kwargs
        message = called_kwargs["input"][0]
        assert message["role"] == "user"
        parts = message["content"]
        assert [part["type"] for part in parts] == [
            "input_text",
            "input_image",
            "input_text",
        ]
        assert parts[0]["text"] == "before"
        assert parts[2]["text"] == "after"
        assert parts[1]["detail"] == "auto"
        media_type, decoded = _decode_openai_image_part(parts[1])
        assert media_type == "image/png"
        assert decoded.size == image.size
        assert decoded.format == "PNG"

    def test_png_bytes_part_builds_data_url(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock(return_value=_make_openai_response())
        data = _png_bytes((6, 3))

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate(["prompt", data], model="gpt-5.2")

        parts = mock_client.responses.create.call_args.kwargs["input"][0]["content"]
        media_type, decoded = _decode_openai_image_part(parts[1])
        assert media_type == "image/png"
        assert decoded.size == (6, 3)
        assert decoded.format == "PNG"

    def test_bad_bytes_raise_before_create(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()

        with (
            patch(
                "solstone.think.providers.openai._get_openai_client",
                return_value=mock_client,
            ),
            pytest.raises(ValueError) as exc_info,
        ):
            provider.run_generate(["prompt", b"not-an-image"], model="gpt-5.2")

        assert "bytes" in str(exc_info.value)
        assert "not-an-image" in str(exc_info.value)
        assert mock_client.responses.create.call_count == 0

    def test_cmyk_image_raises_before_create(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        image = Image.new("CMYK", (2, 2))

        with (
            patch(
                "solstone.think.providers.openai._get_openai_client",
                return_value=mock_client,
            ),
            pytest.raises(ValueError) as exc_info,
        ):
            provider.run_generate(["prompt", image], model="gpt-5.2")

        assert "Image" in str(exc_info.value)
        assert "CMYK" in str(exc_info.value)
        assert mock_client.responses.create.call_count == 0

    def test_with_effort_suffix(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate("hello", model="gpt-5.2-high")

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["model"] == "gpt-5.2"
        assert called_kwargs["reasoning"] == {"effort": "high"}

    def test_with_json_output(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate("hello", model="gpt-5.2", json_output=True)

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"] == {"format": {"type": "json_object"}}

    def test_no_schema_format_unchanged(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate(
                "hello",
                model="gpt-5.2",
                json_output=True,
                json_schema=None,
            )

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"] == {"format": {"type": "json_object"}}

    def test_with_schema_format_shape(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "{}"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response
        schema = {"type": "object"}

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate("hello", model="gpt-5.2", json_schema=schema)

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"] == {
            "format": {
                "type": "json_schema",
                "name": "response",
                "schema": schema,
                "strict": True,
            }
        }

    def test_schema_title_becomes_name(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "{}"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate(
                "hello",
                model="gpt-5.2",
                json_schema={"title": "MyThing", "type": "object"},
            )

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"]["format"]["name"] == "MyThing"

    def test_schema_bad_title_falls_back(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "{}"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate(
                "hello",
                model="gpt-5.2",
                json_schema={"title": "bad name with spaces", "type": "object"},
            )

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"]["format"]["name"] == "response"

    def test_with_system_instruction(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate("hello", system_instruction="Be helpful")

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["instructions"] == "Be helpful"

    def test_with_timeout(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = MagicMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_openai_client",
            return_value=mock_client,
        ):
            provider.run_generate("hello", timeout_s=30.0)

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["timeout"] == 30.0


class TestRunAgenerate:
    def test_basic_agenerate(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello world"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = MagicMock()
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_response.usage.input_tokens_details = None
        mock_response.usage.output_tokens_details = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_async_openai_client",
            return_value=mock_client,
        ):
            result = asyncio.run(provider.run_agenerate("hello", model="gpt-5.2"))

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["model"] == "gpt-5.2"
        assert called_kwargs["input"] == "hello"
        assert called_kwargs["max_output_tokens"] == 16384
        assert result["text"] == "Hello world"
        assert result["finish_reason"] == "stop"
        assert result["thinking"] is None

    def test_with_thinking(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock()
        reasoning_item = MagicMock()
        reasoning_item.type = "reasoning"
        summary = MagicMock()
        summary.text = "Let me think..."
        reasoning_item.summary = [summary]
        mock_response = MagicMock()
        mock_response.output_text = "Hello"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = [reasoning_item]
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_async_openai_client",
            return_value=mock_client,
        ):
            result = asyncio.run(provider.run_agenerate("hello", model="gpt-5.2"))

        assert result["thinking"] == [{"summary": "Let me think..."}]

    def test_async_multi_image_parts_preserve_order(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock(return_value=_make_openai_response())
        first = Image.new("RGB", (3, 2), color="red")
        second = Image.new("RGB", (4, 5), color="green")

        with patch(
            "solstone.think.providers.openai._get_async_openai_client",
            return_value=mock_client,
        ):
            asyncio.run(
                provider.run_agenerate(
                    ["prompt", first, second],
                    model="gpt-5.2",
                )
            )

        parts = mock_client.responses.create.call_args.kwargs["input"][0]["content"]
        assert [part["type"] for part in parts] == [
            "input_text",
            "input_image",
            "input_image",
        ]
        assert parts[0]["text"] == "prompt"
        first_media_type, first_decoded = _decode_openai_image_part(parts[1])
        second_media_type, second_decoded = _decode_openai_image_part(parts[2])
        assert first_media_type == "image/png"
        assert second_media_type == "image/png"
        assert first_decoded.size == first.size
        assert second_decoded.size == second.size
        assert first_decoded.format == "PNG"
        assert second_decoded.format == "PNG"

    def test_no_schema_format_unchanged(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock()
        mock_response = MagicMock()
        mock_response.output_text = "Hello"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_async_openai_client",
            return_value=mock_client,
        ):
            asyncio.run(
                provider.run_agenerate(
                    "hello",
                    model="gpt-5.2",
                    json_output=True,
                    json_schema=None,
                )
            )

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"] == {"format": {"type": "json_object"}}

    def test_with_schema_format_shape(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock()
        mock_response = MagicMock()
        mock_response.output_text = "{}"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response
        schema = {"type": "object"}

        with patch(
            "solstone.think.providers.openai._get_async_openai_client",
            return_value=mock_client,
        ):
            asyncio.run(
                provider.run_agenerate("hello", model="gpt-5.2", json_schema=schema)
            )

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"] == {
            "format": {
                "type": "json_schema",
                "name": "response",
                "schema": schema,
                "strict": True,
            }
        }

    def test_schema_title_becomes_name(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock()
        mock_response = MagicMock()
        mock_response.output_text = "{}"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_async_openai_client",
            return_value=mock_client,
        ):
            asyncio.run(
                provider.run_agenerate(
                    "hello",
                    model="gpt-5.2",
                    json_schema={"title": "MyThing", "type": "object"},
                )
            )

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"]["format"]["name"] == "MyThing"

    def test_schema_bad_title_falls_back(self):
        provider = _openai_provider()
        mock_client = MagicMock()
        mock_client.responses.create = AsyncMock()
        mock_response = MagicMock()
        mock_response.output_text = "{}"
        mock_response.status = "completed"
        mock_response.incomplete_details = None
        mock_response.usage = None
        mock_response.output = []
        mock_client.responses.create.return_value = mock_response

        with patch(
            "solstone.think.providers.openai._get_async_openai_client",
            return_value=mock_client,
        ):
            asyncio.run(
                provider.run_agenerate(
                    "hello",
                    model="gpt-5.2",
                    json_schema={"title": "bad name with spaces", "type": "object"},
                )
            )

        called_kwargs = mock_client.responses.create.call_args.kwargs
        assert called_kwargs["text"]["format"]["name"] == "response"
