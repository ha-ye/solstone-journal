# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
from unittest.mock import MagicMock

import pytest

from solstone.think.models import LOCAL_MODEL, IncompleteJSONError
from solstone.think.providers.cli import QuotaExhaustedError
from solstone.think.providers.local import LocalCapacityExhausted
from solstone.think.talents import _execute_generate, _execute_with_tools


def _generate_config(
    *,
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-6",
) -> dict:
    return {
        "name": "test_generator",
        "type": "generate",
        "provider": provider,
        "model": model,
        "prompt": "say ok",
        "output": "md",
        "output_path": None,
        "thinking_budget": 0,
        "max_output_tokens": 32,
    }


def _health_rows(tmp_path):
    return json.loads((tmp_path / "health" / "talents.json").read_text())["results"]


@pytest.mark.asyncio
async def test_execute_with_tools_quota_records_and_does_not_switch(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    run_cogitate = MagicMock(
        side_effect=QuotaExhaustedError("quota exhausted", retry_delay_ms=5000)
    )
    monkeypatch.setattr("solstone.think.providers.openhands.run_cogitate", run_cogitate)

    events = []
    config = {
        "name": "test_agent",
        "type": "cogitate",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
    }

    with pytest.raises(QuotaExhaustedError, match="quota exhausted"):
        await _execute_with_tools(config, events.append)

    run_cogitate.assert_called_once()
    assert run_cogitate.call_args.kwargs["config"]["provider"] == "anthropic"
    assert run_cogitate.call_args.kwargs["config"]["model"] == "claude-sonnet-4-6"
    rows = _health_rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == "anthropic"
    assert row["model"] == "claude-sonnet-4-6"
    assert row["interface"] == "cogitate"
    assert row["reason_code"] == "provider_quota_exceeded"
    assert "tier" not in row
    assert row["reset_at_ms"] > 0
    assert [event["event"] for event in events] == ["error"]


@pytest.mark.asyncio
async def test_execute_generate_quota_records_and_does_not_switch(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    quota = QuotaExhaustedError("quota exhausted", retry_delay_ms=7000)
    active = MagicMock(side_effect=quota)
    inactive_google = MagicMock(side_effect=AssertionError("google called"))
    inactive_openai = MagicMock(side_effect=AssertionError("openai called"))
    inactive_local = MagicMock(side_effect=AssertionError("local called"))
    monkeypatch.setattr("solstone.think.providers.anthropic.run_generate", active)
    monkeypatch.setattr("solstone.think.providers.google.run_generate", inactive_google)
    monkeypatch.setattr("solstone.think.providers.openai.run_generate", inactive_openai)
    monkeypatch.setattr("solstone.think.providers.local.run_generate", inactive_local)

    with pytest.raises(QuotaExhaustedError, match="quota exhausted"):
        await _execute_generate(_generate_config(), lambda _event: None)

    active.assert_called_once()
    inactive_google.assert_not_called()
    inactive_openai.assert_not_called()
    inactive_local.assert_not_called()
    row = _health_rows(tmp_path)[0]
    assert row["provider"] == "anthropic"
    assert row["model"] == "claude-sonnet-4-6"
    assert row["interface"] == "generate"
    assert row["reason_code"] == "provider_quota_exceeded"
    assert "tier" not in row
    assert row["reset_at_ms"] > 0


@pytest.mark.asyncio
async def test_execute_generate_local_length_retry_succeeds(monkeypatch):
    calls = []

    def fake_generate_with_result(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise IncompleteJSONError("length", '{"partial":')
        return {"text": '{"ok": true}', "usage": {}}

    monkeypatch.setattr(
        "solstone.think.models.generate_with_result",
        fake_generate_with_result,
    )

    events = []
    await _execute_generate(
        _generate_config(provider="local", model=LOCAL_MODEL),
        events.append,
    )

    assert len(calls) == 2
    assert calls[1]["inference_retry_index"] == 1
    assert "local_exclusive_admission" not in calls[1]
    assert events[-1]["event"] == "finish"


@pytest.mark.asyncio
async def test_execute_generate_local_capacity_retry_succeeds(monkeypatch):
    calls = []

    def fake_generate_with_result(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise LocalCapacityExhausted()
        return {"text": "ok", "usage": {}}

    monkeypatch.setattr(
        "solstone.think.models.generate_with_result",
        fake_generate_with_result,
    )

    events = []
    await _execute_generate(
        _generate_config(provider="local", model=LOCAL_MODEL),
        events.append,
    )

    assert len(calls) == 2
    assert calls[1]["inference_retry_index"] == 1
    assert calls[1]["local_exclusive_admission"] is True
    assert events[-1]["event"] == "finish"


@pytest.mark.asyncio
async def test_execute_generate_local_non_retry_error_propagates(monkeypatch):
    calls = []

    def fake_generate_with_result(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("not retryable")

    monkeypatch.setattr(
        "solstone.think.models.generate_with_result",
        fake_generate_with_result,
    )

    with pytest.raises(RuntimeError, match="not retryable"):
        await _execute_generate(
            _generate_config(provider="local", model=LOCAL_MODEL),
            lambda _event: None,
        )

    assert len(calls) == 1
