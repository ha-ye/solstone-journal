# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import asyncio
import json
from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from solstone.think.models import (
    LOCAL_MODEL,
    NO_BRAIN_PROVIDER,
    TYPE_DEFAULTS,
    IncompleteJSONError,
    NoBrainConfiguredError,
    get_backup_provider,
    is_provider_healthy,
    is_provider_model_interface_healthy,
    should_recheck_health,
)
from solstone.think.providers.cli import QuotaExhaustedError
from solstone.think.talents import (
    TalentHookError,
    _is_retryable_error,
    _should_fallback,
)
from solstone.think.utils import now_ms


def test_is_provider_healthy_all_failed():
    health_data = {
        "results": [
            {"provider": "google", "ok": False},
            {"provider": "google", "ok": False},
        ]
    }
    assert is_provider_healthy("google", health_data) is False


def test_is_provider_healthy_some_passed():
    health_data = {
        "results": [
            {"provider": "google", "ok": False},
            {"provider": "google", "ok": True},
        ]
    }
    assert is_provider_healthy("google", health_data) is True


def test_is_provider_healthy_no_data():
    assert is_provider_healthy("google", None) is True


def test_is_provider_healthy_no_results_for_provider():
    health_data = {"results": [{"provider": "anthropic", "ok": False}]}
    assert is_provider_healthy("google", health_data) is True


def test_is_provider_model_interface_healthy_match_failed():
    health_data = {
        "results": [
            {
                "provider": "google",
                "model": "gemini-3-flash-preview",
                "interface": "cogitate",
                "ok": False,
            }
        ]
    }
    assert (
        is_provider_model_interface_healthy(
            "google", "gemini-3-flash-preview", "cogitate", health_data
        )
        is False
    )


def test_is_provider_model_interface_healthy_mismatch_is_healthy():
    health_data = {
        "results": [
            {
                "provider": "google",
                "model": "gemini-3-flash-preview",
                "interface": "generate",
                "ok": False,
            }
        ]
    }
    assert (
        is_provider_model_interface_healthy(
            "google", "gemini-3-flash-preview", "cogitate", health_data
        )
        is True
    )


def test_is_provider_model_interface_healthy_missing_fields_are_healthy():
    health_data = {"results": [{"provider": "google", "ok": False}]}
    assert (
        is_provider_model_interface_healthy(
            "google", "gemini-3-flash-preview", "cogitate", health_data
        )
        is True
    )


def test_is_provider_model_interface_healthy_none_data():
    assert (
        is_provider_model_interface_healthy(
            "google", "gemini-3-flash-preview", "cogitate", None
        )
        is True
    )


def test_should_recheck_health_stale():
    checked_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    health_data = {"checked_at": checked_at}
    assert should_recheck_health(health_data) is True


def test_should_recheck_health_fresh():
    checked_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    health_data = {"checked_at": checked_at}
    assert should_recheck_health(health_data) is False


def test_should_recheck_health_honors_reset_at_ms():
    checked_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    pre_reset = {
        "checked_at": checked_at,
        "results": [{"ok": False, "reset_at_ms": now_ms() + 60_000}],
    }
    post_reset = {
        "checked_at": checked_at,
        "results": [{"ok": False, "reset_at_ms": now_ms() - 1_000}],
    }
    no_reset = {
        "checked_at": checked_at,
        "results": [{"ok": False}],
    }

    assert should_recheck_health(pre_reset) is False
    assert should_recheck_health(post_reset) is True
    assert should_recheck_health(no_reset) is True


def test_get_backup_provider_from_config(monkeypatch):
    monkeypatch.setattr(
        "solstone.think.models.get_config",
        lambda: {"providers": {"generate": {"provider": "google", "backup": "openai"}}},
    )
    assert get_backup_provider("generate") == "openai"


def test_get_backup_provider_no_brain_disables_backup(monkeypatch):
    monkeypatch.setattr("solstone.think.models.get_config", lambda: {})
    monkeypatch.setattr(
        "solstone.think.providers.state.local_runtime_ready", lambda: False
    )
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert get_backup_provider("generate") is None
    assert get_backup_provider("cogitate") is None


def test_get_backup_provider_keyed_default_uses_fallback_constant(monkeypatch):
    monkeypatch.setattr("solstone.think.models.get_config", lambda: {})
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")

    assert get_backup_provider("generate") == TYPE_DEFAULTS["generate"]["backup"]
    assert get_backup_provider("cogitate") == TYPE_DEFAULTS["cogitate"]["backup"]


def test_get_backup_provider_none_when_same_as_primary(monkeypatch):
    monkeypatch.setattr(
        "solstone.think.models.get_config",
        lambda: {
            "providers": {
                "generate": {"provider": "openai", "backup": "openai"},
            }
        },
    )
    assert get_backup_provider("generate") is None


@pytest.mark.parametrize("agent_type", ["generate", "cogitate"])
def test_get_backup_provider_local_disables_backup(monkeypatch, agent_type):
    monkeypatch.setattr(
        "solstone.think.models.get_config",
        lambda: {
            "providers": {
                agent_type: {"provider": "local", "backup": "anthropic"},
            }
        },
    )
    assert get_backup_provider(agent_type) is None


@pytest.mark.parametrize("agent_type", ["generate", "cogitate"])
def test_get_backup_provider_no_brain_primary_disables_backup(monkeypatch, agent_type):
    monkeypatch.setattr(
        "solstone.think.models.get_config",
        lambda: {
            "providers": {
                agent_type: {"provider": NO_BRAIN_PROVIDER, "backup": "anthropic"},
            }
        },
    )
    assert get_backup_provider(agent_type) is None


def test_no_brain_error_is_non_retryable():
    assert _is_retryable_error(NoBrainConfiguredError()) is False
    assert _should_fallback(NoBrainConfiguredError()) is False


def test_execute_with_tools_local_failure_does_not_consult_backup(monkeypatch):
    from solstone.think import talents

    class LocalModule:
        @staticmethod
        async def run_cogitate(config, on_event=None):
            raise RuntimeError("binary_missing")

    monkeypatch.setattr(
        "solstone.think.providers.get_provider_module",
        lambda provider: LocalModule,
    )

    def fail_backup(_agent_type):
        raise AssertionError("local failure must not consult cloud backup")

    monkeypatch.setattr("solstone.think.models.get_backup_provider", fail_backup)

    with pytest.raises(RuntimeError, match="binary_missing"):
        asyncio.run(
            talents._execute_with_tools(
                {
                    "provider": "local",
                    "model": LOCAL_MODEL,
                    "output_path": None,
                },
                lambda _event: None,
            )
        )


def _mock_base_agent_config() -> dict:
    return {
        "type": "cogitate",
        "path": None,
        "sources": {},
        "system_instruction": "",
        "user_instruction": "",
        "prompt": "",
        "disabled": False,
    }


def _patch_prepare_config_dependencies(monkeypatch):
    monkeypatch.setattr(
        "solstone.think.talent.get_talent",
        lambda *args, **kwargs: _mock_base_agent_config(),
    )
    monkeypatch.setattr(
        "solstone.think.talent.key_to_context", lambda _name: "talent.system.default"
    )
    monkeypatch.setattr(
        "solstone.think.models.resolve_provider",
        lambda _context, _type: ("google", "gemini-3-flash-preview"),
    )
    monkeypatch.setattr("solstone.think.models.get_context_registry", lambda: {})


def test_prepare_config_rejects_frontmatter_outbound_approval(tmp_path, monkeypatch):
    import solstone.think.talent as talent_module
    from solstone.think.talents import prepare_config

    monkeypatch.setattr(talent_module, "TALENT_DIR", tmp_path)
    (tmp_path / "approval_static.md").write_text(
        "{\n"
        '  "type": "cogitate",\n'
        '  "outbound_approval": "static-template-value"\n'
        "}\n\n"
        "Prompt body\n"
    )

    with pytest.raises(
        ValueError,
        match=(
            "declares 'outbound_approval' in frontmatter; "
            "this field is launch-config-only"
        ),
    ):
        prepare_config({"name": "approval_static", "prompt": "hello"})


def test_preflight_swap_unhealthy_primary(monkeypatch):
    from solstone.think.talents import prepare_config

    _patch_prepare_config_dependencies(monkeypatch)
    monkeypatch.setattr(
        "solstone.think.models.load_health_status",
        lambda: {
            "results": [
                {
                    "provider": "google",
                    "model": "gemini-3-flash-preview",
                    "interface": "cogitate",
                    "ok": False,
                }
            ]
        },
    )
    monkeypatch.setattr("solstone.think.models.should_recheck_health", lambda _h: False)
    monkeypatch.setattr(
        "solstone.think.models.get_backup_provider", lambda _type: "anthropic"
    )
    monkeypatch.setattr(
        "solstone.think.models.resolve_model_for_provider",
        lambda _context, _provider, _type="generate": "claude-sonnet-4-5",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    config = prepare_config({"name": "chat", "prompt": "hello"})

    assert config["provider"] == "anthropic"
    assert config["model"] == "claude-sonnet-4-5"
    assert config["fallback_from"] == "google"


def test_preflight_swap_reads_unified_backend(monkeypatch):
    from solstone.think.models import CLAUDE_SONNET_4
    from solstone.think.providers import state
    from solstone.think.talents import prepare_config

    _patch_prepare_config_dependencies(monkeypatch)
    monkeypatch.setattr(
        state,
        "read_health_status",
        lambda: {
            "results": [
                {
                    "provider": "google",
                    "model": "gemini-3-flash-preview",
                    "interface": "cogitate",
                    "ok": False,
                    "reason_code": "provider_unavailable",
                }
            ]
        },
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    config = prepare_config({"name": "chat", "prompt": "hello"})

    assert config["provider"] == "anthropic"
    assert config["model"] == CLAUDE_SONNET_4
    assert config["fallback_from"] == "google"


def test_preflight_no_swap_healthy_primary(monkeypatch):
    from solstone.think.talents import prepare_config

    _patch_prepare_config_dependencies(monkeypatch)
    monkeypatch.setattr(
        "solstone.think.models.load_health_status",
        lambda: {
            "results": [
                {
                    "provider": "google",
                    "model": "gemini-3-flash-preview",
                    "interface": "cogitate",
                    "ok": True,
                }
            ]
        },
    )
    monkeypatch.setattr("solstone.think.models.should_recheck_health", lambda _h: False)

    config = prepare_config({"name": "chat", "prompt": "hello"})

    assert config["provider"] == "google"
    assert "fallback_from" not in config


def test_preflight_no_swap_no_backup_key(monkeypatch):
    from solstone.think.talents import prepare_config

    _patch_prepare_config_dependencies(monkeypatch)
    monkeypatch.setattr(
        "solstone.think.models.load_health_status",
        lambda: {
            "results": [
                {
                    "provider": "google",
                    "model": "gemini-3-flash-preview",
                    "interface": "cogitate",
                    "ok": False,
                }
            ]
        },
    )
    monkeypatch.setattr("solstone.think.models.should_recheck_health", lambda _h: False)
    monkeypatch.setattr(
        "solstone.think.models.get_backup_provider", lambda _type: "anthropic"
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    config = prepare_config({"name": "chat", "prompt": "hello"})

    assert config["provider"] == "google"
    assert "fallback_from" not in config


def test_on_failure_retry_cogitate(monkeypatch):
    from solstone.think.talents import _execute_with_tools

    events = []
    attempts = {"primary": 0, "backup": 0}

    async def fail_cogitate(*_args, **_kwargs):
        attempts["primary"] += 1
        raise RuntimeError("primary down")

    async def pass_cogitate(*_args, **kwargs):
        attempts["backup"] += 1
        on_event = kwargs.get("on_event")
        if on_event:
            on_event({"event": "finish", "result": "backup result"})
        return "backup result"

    monkeypatch.setattr(
        "solstone.think.providers.PROVIDER_REGISTRY", {"google": "x", "anthropic": "y"}
    )
    monkeypatch.setattr(
        "solstone.think.providers.get_provider_module",
        lambda provider: SimpleNamespace(
            run_cogitate=fail_cogitate if provider == "google" else pass_cogitate
        ),
    )
    monkeypatch.setattr(
        "solstone.think.models.get_backup_provider", lambda _type: "anthropic"
    )
    monkeypatch.setattr(
        "solstone.think.models.resolve_model_for_provider",
        lambda _context, _provider, _type="cogitate": "claude-sonnet-4-5",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    config = {
        "provider": "google",
        "model": "gemini-3-flash-preview",
        "health_stale": False,
        "context": "talent.system.default",
    }

    asyncio.run(_execute_with_tools(config, events.append))

    assert attempts["primary"] == 1
    assert attempts["backup"] == 1
    assert config["provider"] == "anthropic"
    assert config["model"] == "claude-sonnet-4-5"
    assert config["fallback_from"] == "google"
    assert any(e.get("event") == "fallback" for e in events)


def test_quota_failure_records_health_and_falls_back(monkeypatch):
    from solstone.think.talents import _execute_with_tools

    events = []
    record_mock = MagicMock()

    async def fail_quota(*_args, **_kwargs):
        raise QuotaExhaustedError("quota exhausted", retry_delay_ms=1000)

    async def pass_cogitate(*_args, **kwargs):
        on_event = kwargs.get("on_event")
        if on_event:
            on_event({"event": "finish", "result": "backup result"})
        return "backup result"

    monkeypatch.setattr(
        "solstone.think.providers.PROVIDER_REGISTRY", {"google": "x", "anthropic": "y"}
    )
    monkeypatch.setattr(
        "solstone.think.providers.get_provider_module",
        lambda provider: SimpleNamespace(
            run_cogitate=fail_quota if provider == "google" else pass_cogitate
        ),
    )
    monkeypatch.setattr(
        "solstone.think.models.get_backup_provider", lambda _type: "anthropic"
    )
    monkeypatch.setattr(
        "solstone.think.models.resolve_model_for_provider",
        lambda _context, _provider, _type="cogitate": "claude-sonnet-4-5",
    )
    monkeypatch.setattr("solstone.think.models.record_provider_failure", record_mock)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    config = {
        "type": "cogitate",
        "provider": "google",
        "tier": "flash",
        "model": "gemini-3-flash-preview",
        "health_stale": False,
        "context": "talent.system.default",
    }
    before_ms = now_ms()

    asyncio.run(_execute_with_tools(config, events.append))

    quota_event = next(e for e in events if e.get("reason") == "quota_exhausted")
    assert quota_event["terminal"] is False
    assert quota_event["reset_at_ms"] >= before_ms + 1000
    record_mock.assert_called_once_with(
        "google",
        "flash",
        "gemini-3-flash-preview",
        "cogitate",
        quota_event["reset_at_ms"],
    )
    assert config["provider"] == "anthropic"
    assert events[-1]["event"] == "finish"


def test_on_failure_retry_cogitate_uses_context_from_name(monkeypatch):
    from solstone.think.talents import _execute_with_tools

    events = []
    seen = {}

    async def fail_cogitate(*_args, **_kwargs):
        raise RuntimeError("primary down")

    async def pass_cogitate(*_args, **kwargs):
        on_event = kwargs.get("on_event")
        if on_event:
            on_event({"event": "finish", "result": "backup result"})
        return "backup result"

    def resolve_model(context, _provider, _type="cogitate"):
        seen["context"] = context
        return "claude-sonnet-4-5"

    monkeypatch.setattr(
        "solstone.think.providers.PROVIDER_REGISTRY", {"google": "x", "anthropic": "y"}
    )
    monkeypatch.setattr(
        "solstone.think.providers.get_provider_module",
        lambda provider: SimpleNamespace(
            run_cogitate=fail_cogitate if provider == "google" else pass_cogitate
        ),
    )
    monkeypatch.setattr(
        "solstone.think.talent.key_to_context",
        lambda _name: "talent.system.default",
    )
    monkeypatch.setattr(
        "solstone.think.models.get_backup_provider", lambda _type: "anthropic"
    )
    monkeypatch.setattr(
        "solstone.think.models.resolve_model_for_provider", resolve_model
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    config = {
        "name": "chat",
        "provider": "google",
        "model": "gemini-3-flash-preview",
        "health_stale": False,
    }

    asyncio.run(_execute_with_tools(config, events.append))

    assert seen["context"] == "talent.system.default"


def test_execute_generate_uses_messages_when_present(monkeypatch):
    from solstone.think.talents import _execute_generate

    events = []
    seen = {}
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]

    def mock_generate_with_result(**kwargs):
        seen["contents"] = kwargs["contents"]
        return {"text": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}

    monkeypatch.setattr(
        "solstone.think.talent.key_to_context", lambda _name: "talent.system.default"
    )
    monkeypatch.setattr(
        "solstone.think.models.generate_with_result", mock_generate_with_result
    )

    config = {
        "name": "chat",
        "messages": messages,
        "transcript": "ignored transcript",
        "user_instruction": "ignored instruction",
        "prompt": "ignored prompt",
        "health_stale": False,
    }

    asyncio.run(_execute_generate(config, events.append))

    assert seen["contents"] == messages
    assert events[-1]["event"] == "finish"


def test_execute_generate_preserves_string_contents_order(monkeypatch):
    from solstone.think.talents import _execute_generate

    events = []
    seen = {}

    def mock_generate_with_result(**kwargs):
        seen["contents"] = kwargs["contents"]
        return {"text": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}

    monkeypatch.setattr(
        "solstone.think.talent.key_to_context", lambda _name: "talent.system.default"
    )
    monkeypatch.setattr(
        "solstone.think.models.generate_with_result", mock_generate_with_result
    )

    config = {
        "name": "chat",
        "transcript": "transcript",
        "user_instruction": "instruction",
        "prompt": "prompt",
        "health_stale": False,
    }

    asyncio.run(_execute_generate(config, events.append))

    assert seen["contents"] == ["transcript", "instruction", "prompt"]
    assert events[-1]["event"] == "finish"


def test_execute_generate_passes_prepared_provider_and_model(monkeypatch):
    from solstone.think.talents import _execute_generate

    events = []
    seen = {}

    def mock_generate_with_result(**kwargs):
        seen["provider"] = kwargs.get("provider")
        seen["model"] = kwargs.get("model")
        return {"text": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}

    monkeypatch.setattr(
        "solstone.think.talent.key_to_context", lambda _name: "talent.system.default"
    )
    monkeypatch.setattr(
        "solstone.think.models.generate_with_result", mock_generate_with_result
    )

    config = {
        "name": "chat",
        "provider": "google",
        "model": "gemini-3-flash-preview",
        "prompt": "hello",
        "health_stale": False,
    }

    asyncio.run(_execute_generate(config, events.append))

    assert seen["provider"] == "google"
    assert seen["model"] == "gemini-3-flash-preview"
    assert events[-1]["event"] == "finish"


def test_execute_generate_local_failure_does_not_consult_backup(monkeypatch):
    from solstone.think.talents import _execute_generate

    events = []
    calls = {"count": 0}

    def mock_generate_with_result(**_kwargs):
        calls["count"] += 1
        raise RuntimeError("binary_missing")

    def fail_backup(_agent_type):
        raise AssertionError("local failure must not consult cloud backup")

    monkeypatch.setattr(
        "solstone.think.talent.key_to_context", lambda _name: "talent.system.default"
    )
    monkeypatch.setattr(
        "solstone.think.models.generate_with_result", mock_generate_with_result
    )
    monkeypatch.setattr("solstone.think.models.get_backup_provider", fail_backup)

    config = {
        "name": "chat",
        "provider": "local",
        "prompt": "hello",
        "health_stale": False,
    }

    with pytest.raises(RuntimeError, match="binary_missing"):
        asyncio.run(_execute_generate(config, events.append))

    assert calls["count"] == 1
    assert not any(e.get("event") == "fallback" for e in events)


@pytest.mark.parametrize(
    ("base_temperature", "expected_retry_temperature"),
    [
        (None, 0.7),
        (0.8, 0.8),
    ],
)
def test_execute_generate_local_length_retry_succeeds(
    monkeypatch, base_temperature, expected_retry_temperature
):
    from solstone.think.talents import _execute_generate

    events = []
    calls = []

    def mock_generate_with_result(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise IncompleteJSONError("length", '{"partial":')
        assert kwargs["temperature"] == expected_retry_temperature
        return {"text": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}}

    def fail_backup(_agent_type):
        raise AssertionError("local retry must not consult cloud backup")

    monkeypatch.setattr(
        "solstone.think.talent.key_to_context", lambda _name: "talent.system.default"
    )
    monkeypatch.setattr(
        "solstone.think.models.generate_with_result", mock_generate_with_result
    )
    monkeypatch.setattr("solstone.think.models.get_backup_provider", fail_backup)

    config = {
        "name": "chat",
        "provider": "local",
        "model": LOCAL_MODEL,
        "prompt": "hello",
        "health_stale": False,
    }
    if base_temperature is not None:
        config["temperature"] = base_temperature

    asyncio.run(_execute_generate(config, events.append))

    assert len(calls) == 2
    assert calls[0]["temperature"] == (base_temperature or 0.3)
    assert calls[1]["temperature"] == expected_retry_temperature
    assert not any(e.get("event") == "fallback" for e in events)
    assert events[-1]["event"] == "finish"
    assert events[-1]["retries"] == 1


def test_execute_generate_local_length_retry_success_writes_once_and_runs_hook_once(
    tmp_path, monkeypatch
):
    from solstone.think import models, talents
    from solstone.think.talents import _execute_generate

    output_path = tmp_path / "out.json"
    events = []
    generate_calls = []
    hook_calls = []

    def mock_generate_with_result(**kwargs):
        generate_calls.append(kwargs)
        if len(generate_calls) == 1:
            raise IncompleteJSONError("length", '{"partial":')
        return {
            "text": '{"summary": "ok"}',
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }

    def post_hook(result, config):
        hook_calls.append((result, config["name"]))
        return result

    def fail_backup(_agent_type):
        raise AssertionError("local retry must not consult cloud backup")

    monkeypatch.setattr(
        "solstone.think.talent.key_to_context", lambda _name: "talent.system.default"
    )
    monkeypatch.setattr(models, "generate_with_result", mock_generate_with_result)
    monkeypatch.setattr(models, "get_backup_provider", fail_backup)
    monkeypatch.setattr(talents, "load_post_hook", lambda _config: post_hook)

    asyncio.run(
        _execute_generate(
            {
                "name": "chat",
                "provider": "local",
                "model": LOCAL_MODEL,
                "prompt": "hello",
                "health_stale": False,
                "output": "json",
                "output_path": str(output_path),
                "hook": {"post": "test"},
                "json_schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["summary"],
                    "properties": {"summary": {"type": "string"}},
                },
            },
            events.append,
        )
    )

    assert len(generate_calls) == 2
    assert hook_calls == [('{"summary": "ok"}', "chat")]
    assert output_path.read_text(encoding="utf-8") == '{"summary": "ok"}'
    assert [path.name for path in tmp_path.iterdir()] == ["out.json"]
    assert events[-1]["event"] == "finish"
    assert events[-1]["retries"] == 1


def test_main_async_local_length_retry_second_failure_emits_one_error(
    monkeypatch, capsys
):
    from solstone.think import talents

    ndjson_input = json.dumps({"name": "chat", "prompt": "hello"})
    calls = {"count": 0}

    def mock_generate_with_result(**_kwargs):
        calls["count"] += 1
        raise IncompleteJSONError("length", '{"partial":')

    mock_args = MagicMock()
    mock_args.verbose = False
    mock_args.dry_run = False
    mock_args.subcommand = None

    config = {
        "type": "generate",
        "name": "chat",
        "provider": "local",
        "model": LOCAL_MODEL,
        "prompt": "hello",
        "health_stale": False,
    }

    monkeypatch.setattr("sys.stdin", StringIO(ndjson_input))
    monkeypatch.setattr("solstone.think.talents.setup_cli", lambda _parser: mock_args)
    monkeypatch.setattr(
        "solstone.think.talents.setup_logging",
        lambda _verbose=False: MagicMock(),
    )
    monkeypatch.setattr(
        "solstone.think.talents.prepare_config", lambda _request: config
    )
    monkeypatch.setattr("solstone.think.talents.validate_config", lambda _config: None)
    monkeypatch.setattr("solstone.think.talents._run_pre_hooks", lambda _config: {})
    monkeypatch.setattr(
        "solstone.think.talent.key_to_context", lambda _name: "talent.system.default"
    )
    monkeypatch.setattr(
        "solstone.think.models.generate_with_result", mock_generate_with_result
    )
    monkeypatch.setattr(
        "solstone.think._extraction_utils.log_extraction_failure",
        lambda _exc, _name: None,
    )

    asyncio.run(talents.main_async())

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]
    error_events = [event for event in events if event.get("event") == "error"]
    finish_events = [event for event in events if event.get("event") == "finish"]

    assert calls["count"] == 2
    assert len(error_events) == 1
    assert error_events[0]["reason_code"] == "incomplete_json_length"
    assert error_events[0]["retries"] == 1
    assert finish_events == []


@pytest.mark.parametrize(
    "exc",
    [
        IncompleteJSONError("safety", '{"partial":'),
        RuntimeError("binary_missing"),
    ],
)
def test_execute_generate_local_non_length_errors_do_not_retry(monkeypatch, exc):
    from solstone.think.talents import _execute_generate

    events = []
    calls = {"count": 0}

    def mock_generate_with_result(**_kwargs):
        calls["count"] += 1
        raise exc

    def fail_backup(_agent_type):
        raise AssertionError("local failure must not consult cloud backup")

    monkeypatch.setattr(
        "solstone.think.talent.key_to_context", lambda _name: "talent.system.default"
    )
    monkeypatch.setattr(
        "solstone.think.models.generate_with_result", mock_generate_with_result
    )
    monkeypatch.setattr("solstone.think.models.get_backup_provider", fail_backup)

    config = {
        "name": "chat",
        "provider": "local",
        "model": LOCAL_MODEL,
        "prompt": "hello",
        "health_stale": False,
    }

    with pytest.raises(type(exc)):
        asyncio.run(_execute_generate(config, events.append))

    assert calls["count"] == 1
    assert not any(e.get("event") == "fallback" for e in events)


def test_execute_generate_cloud_incomplete_json_preserves_existing_nonlocal_path(
    monkeypatch,
):
    from solstone.think.talents import _execute_generate

    events = []
    calls = {"count": 0}

    def mock_generate_with_result(**_kwargs):
        calls["count"] += 1
        raise IncompleteJSONError("length", '{"partial":')

    def fail_backup(_agent_type):
        raise AssertionError("IncompleteJSONError is ValueError; no fallback today")

    monkeypatch.setattr(
        "solstone.think.talent.key_to_context", lambda _name: "talent.system.default"
    )
    monkeypatch.setattr(
        "solstone.think.models.generate_with_result", mock_generate_with_result
    )
    monkeypatch.setattr("solstone.think.models.get_backup_provider", fail_backup)

    config = {
        "name": "chat",
        "provider": "google",
        "model": "gemini-3-flash-preview",
        "prompt": "hello",
        "health_stale": False,
    }

    with pytest.raises(IncompleteJSONError):
        asyncio.run(_execute_generate(config, events.append))

    assert calls["count"] == 1
    assert not any(e.get("event") == "fallback" for e in events)


def test_on_failure_retry_generate(monkeypatch):
    from solstone.think.talents import _execute_generate

    events = []
    calls = {"count": 0}

    def mock_generate_with_result(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("primary generate failed")
        assert kwargs.get("provider") == "anthropic"
        assert kwargs.get("model") == "claude-sonnet-4-5"
        return {"text": "backup text", "usage": {"input_tokens": 1, "output_tokens": 1}}

    monkeypatch.setattr(
        "solstone.think.talent.key_to_context", lambda _name: "talent.system.default"
    )
    monkeypatch.setattr(
        "solstone.think.models.generate_with_result", mock_generate_with_result
    )
    monkeypatch.setattr(
        "solstone.think.models.get_backup_provider", lambda _type: "anthropic"
    )
    monkeypatch.setattr(
        "solstone.think.models.resolve_model_for_provider",
        lambda _context, _provider, _type="generate": "claude-sonnet-4-5",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    config = {
        "name": "chat",
        "provider": "google",
        "model": "gemini-3-flash-preview",
        "prompt": "hello",
        "health_stale": False,
    }

    asyncio.run(_execute_generate(config, events.append))

    assert calls["count"] == 2
    assert config["provider"] == "anthropic"
    assert config["fallback_from"] == "google"
    assert any(e.get("event") == "fallback" for e in events)
    assert events[-1]["event"] == "finish"
    assert events[-1]["result"] == "backup text"


def test_on_failure_no_retry_value_error(monkeypatch):
    from solstone.think.talents import _execute_generate

    events = []
    assert _is_retryable_error(ValueError("bad input")) is False

    def bad_generate(**_kwargs):
        raise ValueError("bad input")

    monkeypatch.setattr(
        "solstone.think.talent.key_to_context", lambda _name: "talent.system.default"
    )
    monkeypatch.setattr("solstone.think.models.generate_with_result", bad_generate)

    config = {
        "name": "chat",
        "provider": "google",
        "model": "gemini-3-flash-preview",
        "prompt": "hello",
        "health_stale": False,
    }

    with pytest.raises(ValueError, match="bad input"):
        asyncio.run(_execute_generate(config, events.append))

    assert not any(e.get("event") == "fallback" for e in events)


def test_talent_hook_error_is_non_retryable():
    exc = TalentHookError("post", "broken", "chat", RuntimeError("boom"))

    assert _is_retryable_error(exc) is False
    assert _should_fallback(exc) is False


def test_on_failure_both_fail_raises_original(monkeypatch):
    from solstone.think.talents import _execute_generate

    events = []
    calls = {"count": 0}

    def always_fail(**kwargs):
        calls["count"] += 1
        if kwargs.get("provider") == "anthropic":
            raise RuntimeError("backup failed")
        raise RuntimeError("primary failed")

    monkeypatch.setattr(
        "solstone.think.talent.key_to_context", lambda _name: "talent.system.default"
    )
    monkeypatch.setattr("solstone.think.models.generate_with_result", always_fail)
    monkeypatch.setattr(
        "solstone.think.models.get_backup_provider", lambda _type: "anthropic"
    )
    monkeypatch.setattr(
        "solstone.think.models.resolve_model_for_provider",
        lambda _context, _provider, _type="generate": "claude-sonnet-4-5",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    config = {
        "name": "chat",
        "provider": "google",
        "model": "gemini-3-flash-preview",
        "prompt": "hello",
        "health_stale": False,
    }

    with pytest.raises(RuntimeError, match="primary failed"):
        asyncio.run(_execute_generate(config, events.append))

    assert calls["count"] == 2


def test_fallback_event_emitted():
    from solstone.think.talents import _run_talent

    events = []
    config = {
        "type": "cogitate",
        "name": "chat",
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "prompt": "hello",
        "fallback_from": "google",
    }

    asyncio.run(_run_talent(config, events.append, dry_run=True))

    fallback_events = [e for e in events if e.get("event") == "fallback"]
    assert len(fallback_events) == 1
    assert fallback_events[0]["reason"] == "preflight"


def test_run_talent_refresh_bypasses_output_exists_guard(tmp_path, monkeypatch):
    from solstone.think import talents

    out = tmp_path / "out"
    out.write_text("STALE", encoding="utf-8")
    events = []
    called = {"execute": False}

    async def fake_execute(config, emit_event):
        called["execute"] = True
        emit_event({"event": "finish", "ts": 0, "result": "FRESH"})

    monkeypatch.setattr(talents, "_execute_with_tools", fake_execute)
    monkeypatch.setattr(talents, "_run_pre_hooks", lambda config: {})

    config = {
        "type": "cogitate",
        "name": "alpha",
        "provider": "google",
        "model": "x",
        "prompt": "hi",
        "output_path": str(out),
        "refresh": True,
    }

    asyncio.run(talents._run_talent(config, events.append, dry_run=False))

    finish_events = [event for event in events if event.get("event") == "finish"]
    assert called["execute"] is True
    assert finish_events[-1]["result"] == "FRESH"


def test_run_talent_regenerates_existing_output_without_provenance(
    tmp_path, monkeypatch
):
    from solstone.think import talents

    out = tmp_path / "out"
    out.write_text("STALE", encoding="utf-8")
    events = []
    called = {"execute": False}

    async def fake_execute(config, emit_event):
        called["execute"] = True
        emit_event({"event": "finish", "ts": 0, "result": "FRESH"})

    monkeypatch.setattr(talents, "_execute_with_tools", fake_execute)
    monkeypatch.setattr(talents, "_run_pre_hooks", lambda config: {})

    config = {
        "type": "cogitate",
        "name": "alpha",
        "provider": "google",
        "model": "x",
        "prompt": "hi",
        "output_path": str(out),
    }

    asyncio.run(talents._run_talent(config, events.append, dry_run=False))

    finish_events = [event for event in events if event.get("event") == "finish"]
    assert called["execute"] is True
    assert finish_events[-1]["result"] == "FRESH"


def test_recheck_requested_on_stale(monkeypatch):
    from solstone.think.talents import _execute_with_tools

    async def pass_cogitate(*_args, **kwargs):
        on_event = kwargs.get("on_event")
        if on_event:
            on_event({"event": "finish", "result": "ok"})
        return "ok"

    recheck_mock = MagicMock()

    monkeypatch.setattr("solstone.think.providers.PROVIDER_REGISTRY", {"google": "x"})
    monkeypatch.setattr(
        "solstone.think.providers.get_provider_module",
        lambda _provider: SimpleNamespace(run_cogitate=pass_cogitate),
    )
    monkeypatch.setattr("solstone.think.models.request_health_recheck", recheck_mock)

    config = {
        "provider": "google",
        "model": "gemini-3-flash-preview",
        "health_stale": True,
    }

    asyncio.run(_execute_with_tools(config, lambda _e: None))

    recheck_mock.assert_called_once()
    assert config["health_stale"] is False


def test_main_async_no_duplicate_error_when_evented(monkeypatch, capsys):
    from solstone.think.talents import main_async

    ndjson_input = json.dumps({"name": "chat", "prompt": "hello"})
    monkeypatch.setattr("sys.stdin", StringIO(ndjson_input))

    async def fake_run_talent(_config, emit_event, dry_run=False):
        emit_event({"event": "error", "error": "provider failed"})
        exc = RuntimeError("provider failed")
        setattr(exc, "_evented", True)
        raise exc

    mock_args = MagicMock()
    mock_args.verbose = False
    mock_args.dry_run = False
    mock_args.subcommand = None

    monkeypatch.setattr("solstone.think.talents.setup_cli", lambda _parser: mock_args)
    monkeypatch.setattr(
        "solstone.think.talents.setup_logging",
        lambda _verbose=False: MagicMock(),
    )
    monkeypatch.setattr(
        "solstone.think.talents.prepare_config", lambda _request: {"type": "cogitate"}
    )
    monkeypatch.setattr("solstone.think.talents.validate_config", lambda _config: None)
    monkeypatch.setattr("solstone.think.talents._run_talent", fake_run_talent)

    asyncio.run(main_async())

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]
    error_events = [event for event in events if event.get("event") == "error"]
    assert len(error_events) == 1


def test_main_async_cogitate_hook_error_no_fallback_no_double_emit(
    monkeypatch,
    capsys,
):
    from solstone.think import talents

    ndjson_input = json.dumps({"name": "chat", "prompt": "hello"})
    monkeypatch.setattr("sys.stdin", StringIO(ndjson_input))

    provider_calls = []

    async def fake_run_cogitate(config, on_event):
        provider_calls.append(config["provider"])
        on_event({"event": "finish", "result": "provider result"})

    def broken_post_hook(result, context):
        raise RuntimeError("hook exploded")

    mock_args = MagicMock()
    mock_args.verbose = False
    mock_args.dry_run = False
    mock_args.subcommand = None

    config = {
        "type": "cogitate",
        "name": "chat",
        "provider": "google",
        "model": "gemini-3-flash-preview",
        "tier": "flash",
        "prompt": "hello",
        "hook": {"post": "broken_hook"},
    }

    monkeypatch.setattr("solstone.think.talents.setup_cli", lambda _parser: mock_args)
    monkeypatch.setattr(
        "solstone.think.talents.setup_logging",
        lambda _verbose=False: MagicMock(),
    )
    monkeypatch.setattr(
        "solstone.think.talents.prepare_config", lambda _request: config
    )
    monkeypatch.setattr("solstone.think.talents.validate_config", lambda _config: None)
    monkeypatch.setattr(
        "solstone.think.talents.load_post_hook", lambda _config: broken_post_hook
    )
    monkeypatch.setattr(
        "solstone.think.providers.PROVIDER_REGISTRY",
        {"google": object(), "anthropic": object()},
    )
    monkeypatch.setattr(
        "solstone.think.providers.get_provider_module",
        lambda _provider: SimpleNamespace(run_cogitate=fake_run_cogitate),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    asyncio.run(talents.main_async())

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]
    error_events = [event for event in events if event.get("event") == "error"]

    assert provider_calls == ["google"]
    assert [event for event in events if event.get("event") == "fallback"] == []
    assert [event for event in events if event.get("event") == "finish"] == []
    assert len(error_events) == 1
    assert error_events[0]["reason_code"] == "hook_error"
    assert error_events[0]["terminal"] is True
    assert (
        "post-hook 'broken_hook' failed for talent 'chat'" in error_events[0]["error"]
    )
    assert "hook exploded" in error_events[0]["error"]
