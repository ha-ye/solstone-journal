# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

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
from tests._logging_isolation import preserve_global_logging
from tests.openhands_fakes import _REGISTERED_TOOLS, install_fake_openhands

_MAX_ITERATIONS_4 = f"{openhands._OPENHANDS_MAX_ITERATIONS_PREFIX}(4)."
_MAX_ITERATIONS_62 = f"{openhands._OPENHANDS_MAX_ITERATIONS_PREFIX}(62)."
_OTHER_SDK_ERROR = "Conversation failed to start."
_DIAGNOSTIC_MAX_ITERATIONS_INFO = (
    "Readiness probe reached the iteration limit derived from its deliberate turn "
    "budget; the preceding SDK max-iterations line is expected here and was "
    "recorded at INFO rather than ERROR."
)


class _ListHandler(logging.Handler):
    def __init__(self, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


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


def _agent_message(fake_openhands, content: str):
    return fake_openhands.MessageEvent(
        source="agent",
        llm_message=SimpleNamespace(content=[SimpleNamespace(text=content)]),
    )


def _install_log_and_final_arun(
    fake_openhands,
    message: str,
    content: str,
    *,
    emit_final: bool = True,
) -> None:
    async def log_and_finish(conversation):
        logging.getLogger(openhands._OPENHANDS_CONVERSATION_LOGGER).error(message)
        event = (
            _emit_final_action(fake_openhands, content)
            if emit_final
            else _agent_message(fake_openhands, content)
        )
        for callback in conversation.callbacks:
            callback(event)

    fake_openhands.Conversation.arun_impl = log_and_finish


def _fresh_fake_openhands(monkeypatch):
    fake_openhands = install_fake_openhands(monkeypatch)
    emit_final_tool._EMIT_FINAL_TYPES.clear()
    _REGISTERED_TOOLS.clear()
    return fake_openhands


def _sdk_logger() -> logging.Logger:
    return logging.getLogger(openhands._OPENHANDS_CONVERSATION_LOGGER)


def _run_with_sdk_log_capture(
    config: dict,
    events: list[dict],
) -> tuple[str | None, list[logging.LogRecord]]:
    sdk_logger = _sdk_logger()
    before_filters = tuple(sdk_logger.filters)
    handler = _ListHandler(logging.INFO)
    sdk_logger.addHandler(handler)
    try:
        result = asyncio.run(openhands.run_cogitate(config, events.append))
    finally:
        sdk_logger.removeHandler(handler)
    assert tuple(sdk_logger.filters) == before_filters
    return result, handler.records


def _run_with_provider_log_capture(
    config: dict,
    events: list[dict],
) -> tuple[str | None, list[logging.LogRecord], list[logging.LogRecord]]:
    provider_logger = logging.getLogger("solstone.think.providers.openhands")
    old_provider_level = provider_logger.level
    provider_handler = _ListHandler(logging.INFO)
    provider_logger.addHandler(provider_handler)
    provider_logger.setLevel(logging.INFO)
    try:
        result, sdk_records = _run_with_sdk_log_capture(config, events)
    finally:
        provider_logger.setLevel(old_provider_level)
        provider_logger.removeHandler(provider_handler)
    return result, sdk_records, provider_handler.records


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


@pytest.mark.parametrize("message", [_MAX_ITERATIONS_4, _MAX_ITERATIONS_62])
def test_diagnostic_sdk_max_iterations_error_demotes_to_info(
    monkeypatch,
    tmp_path,
    message: str,
):
    with preserve_global_logging():
        fake_openhands = _fresh_fake_openhands(monkeypatch)
        _install_log_and_final_arun(fake_openhands, message, "diagnostic ok")
        config = _run_config(monkeypatch, tmp_path)
        events: list[dict] = []

        result, records = _run_with_sdk_log_capture(config, events)

    matching = [record for record in records if record.getMessage() == message]
    assert result == "diagnostic ok"
    assert len(matching) == 1
    assert matching[0].levelno == logging.INFO
    assert matching[0].levelname == "INFO"


def test_non_diagnostic_sdk_max_iterations_error_stays_error(monkeypatch, tmp_path):
    with preserve_global_logging():
        fake_openhands = _fresh_fake_openhands(monkeypatch)
        _install_log_and_final_arun(
            fake_openhands,
            _MAX_ITERATIONS_4,
            "non diagnostic ok",
            emit_final=False,
        )
        config = _run_config(monkeypatch, tmp_path)
        del config["diagnostic"]
        events: list[dict] = []

        result, records = _run_with_sdk_log_capture(config, events)

    matching = [
        record for record in records if record.getMessage() == _MAX_ITERATIONS_4
    ]
    assert result == "non diagnostic ok"
    assert len(matching) == 1
    assert matching[0].levelno == logging.ERROR
    assert matching[0].levelname == "ERROR"


def test_diagnostic_keeps_other_sdk_errors_at_error(monkeypatch, tmp_path):
    with preserve_global_logging():
        fake_openhands = _fresh_fake_openhands(monkeypatch)
        _install_log_and_final_arun(fake_openhands, _OTHER_SDK_ERROR, "diagnostic ok")
        config = _run_config(monkeypatch, tmp_path)
        events: list[dict] = []

        result, records = _run_with_sdk_log_capture(config, events)

    matching = [record for record in records if record.getMessage() == _OTHER_SDK_ERROR]
    assert result == "diagnostic ok"
    assert len(matching) == 1
    assert matching[0].levelno == logging.ERROR
    assert matching[0].levelname == "ERROR"


def test_diagnostic_max_iterations_filter_removed_on_normal_and_raised_exit():
    with preserve_global_logging():
        sdk_logger = _sdk_logger()
        before_filters = tuple(sdk_logger.filters)

        with openhands._demote_diagnostic_max_iterations_error() as state:
            assert state.demoted is False
        assert tuple(sdk_logger.filters) == before_filters

        with pytest.raises(RuntimeError, match="boom"):
            with openhands._demote_diagnostic_max_iterations_error():
                raise RuntimeError("boom")
        assert tuple(sdk_logger.filters) == before_filters


def test_diagnostic_companion_info_emits_once_when_demoted(monkeypatch, tmp_path):
    with preserve_global_logging():
        fake_openhands = _fresh_fake_openhands(monkeypatch)
        _install_log_and_final_arun(fake_openhands, _MAX_ITERATIONS_4, "diagnostic ok")
        config = _run_config(monkeypatch, tmp_path)
        events: list[dict] = []

        result, _sdk_records, provider_records = _run_with_provider_log_capture(
            config,
            events,
        )

    companion = [
        record
        for record in provider_records
        if record.getMessage() == _DIAGNOSTIC_MAX_ITERATIONS_INFO
    ]
    assert result == "diagnostic ok"
    assert len(companion) == 1
    assert companion[0].levelno == logging.INFO
    assert companion[0].levelname == "INFO"


def test_diagnostic_companion_info_absent_without_demotion(monkeypatch, tmp_path):
    with preserve_global_logging():
        fake_openhands = _fresh_fake_openhands(monkeypatch)
        _install_emit_final_arun(fake_openhands, "diagnostic ok")
        config = _run_config(monkeypatch, tmp_path)
        events: list[dict] = []

        result, _sdk_records, provider_records = _run_with_provider_log_capture(
            config,
            events,
        )

    companion = [
        record
        for record in provider_records
        if record.getMessage() == _DIAGNOSTIC_MAX_ITERATIONS_INFO
    ]
    assert result == "diagnostic ok"
    assert companion == []
