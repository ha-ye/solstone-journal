# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import base64
import copy
import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from solstone.think.models import (
    LOCAL_MODEL,
    PROVIDER_DEFAULTS,
    TIER_FLASH,
    TIER_LITE,
    TIER_PRO,
    get_model_provider,
)
from solstone.think.talents import TalentHookError


def _provider():
    providers_pkg = importlib.import_module("solstone.think.providers")
    if hasattr(providers_pkg, "local_budget"):
        delattr(providers_pkg, "local_budget")
    sys.modules.pop("solstone.think.providers.local_budget", None)
    return importlib.reload(importlib.import_module("solstone.think.providers.local"))


def test_local_model_prefix_maps_to_provider():
    assert get_model_provider(LOCAL_MODEL) == "local"


def test_local_model_specs():
    provider = _provider()

    assert set(provider.LOCAL_MODEL_SPECS) == {LOCAL_MODEL}
    spec = provider.LOCAL_MODEL_SPECS[LOCAL_MODEL]
    assert spec.repo == "unsloth/Qwen3.5-4B-GGUF"
    assert spec.filename == "Qwen3.5-4B-Q4_K_M.gguf"
    assert (
        spec.sha256
        == "00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4"
    )
    assert spec.size_bytes == 2740937888
    assert spec.min_ram_bytes == 8 * 1024**3
    assert spec.mmproj_filename == "mmproj-F16.gguf"
    assert (
        spec.mmproj_sha256
        == "cd88edcf8d031894960bb0c9c5b9b7e1fea6ebee02b9f7ce925a00d12891f864"
    )
    assert spec.mmproj_size_bytes == 672423616


def test_local_provider_defaults_and_registry():
    from solstone.think.providers import PROVIDER_METADATA, PROVIDER_REGISTRY

    assert PROVIDER_DEFAULTS["local"][TIER_PRO] == LOCAL_MODEL
    assert PROVIDER_DEFAULTS["local"][TIER_FLASH] == LOCAL_MODEL
    assert PROVIDER_DEFAULTS["local"][TIER_LITE] == LOCAL_MODEL
    assert PROVIDER_REGISTRY["local"] == "solstone.think.providers.local"
    assert PROVIDER_METADATA["local"] == {
        "label": "Local (on-device)",
        "env_key": "",
    }


def test_context_budget_exceeded_classifies_by_reason_code():
    provider = _provider()

    assert (
        provider.classify_provider_error(
            provider.ContextBudgetExceeded("too large"), "local"
        )
        == "context_budget_exceeded"
    )


def test_cloud_generate_providers_do_not_reference_local_budget():
    root = Path(__file__).resolve().parents[1]

    for rel_path in (
        "solstone/think/providers/anthropic.py",
        "solstone/think/providers/google.py",
        "solstone/think/providers/openai.py",
    ):
        text = (root / rel_path).read_text(encoding="utf-8")
        assert "local_budget" not in text
        assert "fit_contents" not in text


def test_list_models_returns_specs():
    models = _provider().list_models("local")

    assert [model["model"] for model in models] == [LOCAL_MODEL]
    assert models[0]["min_ram_bytes"] == 8 * 1024**3


def test_validate_key_uses_tiny_generate(monkeypatch):
    provider = _provider()
    calls = []

    def fake_generate(*args, **kwargs):
        calls.append((args, kwargs))
        return {"text": "OK"}

    monkeypatch.setattr(provider, "run_generate", fake_generate)

    assert provider.validate_key("local", "") == {"valid": True}
    assert calls[0][0] == ("Say OK",)
    assert calls[0][1]["model"] == LOCAL_MODEL
    assert calls[0][1]["max_output_tokens"] == 8


def test_run_generate_posts_to_loopback(monkeypatch):
    provider = _provider()
    served_model_id = (
        "/Users/sol/.cache/huggingface/hub/"
        "models--mlx-community--Qwen3.5-9B/snapshots/abc123"
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: SimpleNamespace(
            port=4321,
            base_url="http://127.0.0.1:4321",
            served_model_id=served_model_id,
        ),
    )
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": served_model_id,
                "choices": [
                    {
                        "message": {"content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            }

    def fake_post(url, json, timeout):
        captured.update({"url": url, "json": json, "timeout": timeout})
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    result = provider.run_generate("hello", model=LOCAL_MODEL, max_output_tokens=16)

    assert captured["url"] == "http://127.0.0.1:4321/v1/chat/completions"
    assert captured["json"]["model"] == served_model_id
    assert captured["json"]["messages"] == [{"role": "user", "content": "hello"}]
    assert captured["json"]["max_tokens"] == 16
    assert captured["json"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["json"]["top_p"] == 0.8
    assert captured["json"]["top_k"] == 20
    assert captured["json"]["min_p"] == 0.0
    assert captured["json"]["presence_penalty"] == 1.5
    assert result["text"] == "hello"
    assert result["model"] == LOCAL_MODEL
    assert result["usage"] == {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
    }


def test_run_generate_emits_chat_completions_image_url(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: SimpleNamespace(
            port=4321,
            base_url="http://127.0.0.1:4321",
            served_model_id=LOCAL_MODEL,
        ),
    )
    png = b"\x89PNG\r\n\x1a\npayload"
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": LOCAL_MODEL,
                "choices": [
                    {
                        "message": {"content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            }

    def fake_post(url, json, timeout):
        captured.update({"url": url, "json": json, "timeout": timeout})
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    provider.run_generate(["look", png], model=LOCAL_MODEL)

    assert captured["json"]["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,"
                        + base64.b64encode(png).decode("ascii")
                    },
                },
            ],
        }
    ]


def test_run_generate_bundled_clips_oversized_text_block(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: SimpleNamespace(
            port=4321,
            base_url="http://127.0.0.1:4321",
            served_model_id=LOCAL_MODEL,
        ),
    )
    from solstone.think.providers import local_budget

    monkeypatch.setattr(local_budget, "count_tokens", lambda text, _base_url: len(text))
    chunks = [
        "## 2026-06-23 09:00:00 - 09:05:00\n",
        "### Transcript\noldest " + ("o" * 5000) + "\n",
        "### Screen Activity\nmiddle " + ("m" * 5000) + "\n",
        "## 2026-06-23 09:05:00 - 09:10:00\n",
        "### Transcript\nrecent " + ("r" * 5000) + "\n",
        "### Screen Activity\nlatest " + ("l" * 5000) + "\n",
    ]
    big_block = "".join(chunks)
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": LOCAL_MODEL,
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }

    def fake_post(url, json, timeout):
        captured.update({"url": url, "json": json, "timeout": timeout})
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    schema = {"type": "object"}
    result = provider.run_generate(
        [big_block, "talent prompt"],
        model=LOCAL_MODEL,
        max_output_tokens=8192 * 6,
        system_instruction="system",
        json_schema=schema,
    )

    assert captured["json"]["messages"][0] == {"role": "system", "content": "system"}
    user_message = captured["json"]["messages"][1]["content"]
    assert local_budget.TRUNCATION_MARKER in user_message
    assert "oldest " not in user_message
    assert "latest " in user_message
    assert "talent prompt" in user_message
    assert len(user_message) < len(big_block)
    assert captured["json"]["response_format"]["json_schema"]["schema"] == schema
    assert result["input_budget"]["clipped"] is True


def test_run_generate_normalizes_schema_pattern_shorthand(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: SimpleNamespace(
            port=4321,
            base_url="http://127.0.0.1:4321",
            served_model_id=LOCAL_MODEL,
        ),
    )
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": LOCAL_MODEL,
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }

    def fake_post(url, json, timeout):
        captured.update({"url": url, "json": json, "timeout": timeout})
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    schema = {
        "type": "object",
        "properties": {
            "timestamp": {
                "type": "string",
                "pattern": r"^\d{2}:\d{2}:\d{2}$",
            },
            "slots": {
                "type": "array",
                "items": {
                    "type": "string",
                    "pattern": r"^([01]\d|2[0-3]):[0-5]\d$",
                },
            },
        },
        "anyOf": [
            {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "pattern": r"^\d{2}:\d{2}:\d{2}$",
                    }
                },
            }
        ],
    }

    provider.run_generate("hello", model=LOCAL_MODEL, json_schema=schema)

    posted_schema = captured["json"]["response_format"]["json_schema"]["schema"]
    patterns = []

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("pattern"), str):
                patterns.append(node["pattern"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(posted_schema)

    assert patterns
    for pattern in patterns:
        assert "[0-9]" in pattern
        assert "\\d" not in pattern
    assert posted_schema["properties"]["slots"]["maxItems"] == 192


def test_run_generate_does_not_mutate_caller_schema(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: SimpleNamespace(
            port=4321,
            base_url="http://127.0.0.1:4321",
            served_model_id=LOCAL_MODEL,
        ),
    )
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": LOCAL_MODEL,
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }

    def fake_post(url, json, timeout):
        captured.update({"url": url, "json": json, "timeout": timeout})
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    schema = {
        "type": "object",
        "properties": {
            "timestamp": {
                "type": "string",
                "pattern": r"^\d{2}:\d{2}:\d{2}$",
            },
            "slots": {"type": "array", "items": {"type": "string"}},
        },
    }
    original_schema = copy.deepcopy(schema)

    provider.run_generate("hello", model=LOCAL_MODEL, json_schema=schema)

    posted_schema = captured["json"]["response_format"]["json_schema"]["schema"]
    assert (
        posted_schema["properties"]["timestamp"]["pattern"]
        == "^[0-9]{2}:[0-9]{2}:[0-9]{2}$"
    )
    assert posted_schema["properties"]["slots"]["maxItems"] == 192
    assert schema == original_schema
    assert schema["properties"]["timestamp"]["pattern"] == r"^\d{2}:\d{2}:\d{2}$"
    assert "maxItems" not in schema["properties"]["slots"]


def test_prepare_local_schema_bounds_arrays_only_and_preserves_input():
    provider = _provider()
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "string", "pattern": r"^\d+$"},
            },
            "nullable_items": {
                "type": ["array", "null"],
                "items": {"type": "string"},
            },
            "prebounded": {
                "type": "array",
                "maxItems": 7,
                "items": {"type": "string"},
            },
            "status": {"type": "string", "enum": ["open", "closed"]},
            "empty": {"type": "null"},
            "name": {"type": "string"},
        },
    }
    original_schema = copy.deepcopy(schema)

    prepared = provider._prepare_local_schema(schema)

    assert not hasattr(provider, "_normalize_schema_patterns")
    assert schema == original_schema
    assert prepared["properties"]["items"]["maxItems"] == 192
    assert prepared["properties"]["nullable_items"]["maxItems"] == 192
    assert prepared["properties"]["prebounded"]["maxItems"] == 7
    assert prepared["properties"]["items"]["items"]["pattern"] == "^[0-9]+$"
    assert prepared["properties"]["status"] == schema["properties"]["status"]
    assert prepared["properties"]["empty"] == schema["properties"]["empty"]
    assert prepared["properties"]["name"] == schema["properties"]["name"]

    forbidden = {"maxLength", "minItems", "minLength", "minimum", "maximum"}
    found = set()

    def walk(node):
        if isinstance(node, dict):
            found.update(forbidden & node.keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(prepared)
    assert found == set()


def test_run_generate_preserves_non_pattern_backslash_d(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: SimpleNamespace(
            port=4321,
            base_url="http://127.0.0.1:4321",
            served_model_id=LOCAL_MODEL,
        ),
    )
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": LOCAL_MODEL,
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }

    def fake_post(url, json, timeout):
        captured.update({"url": url, "json": json, "timeout": timeout})
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    schema = {
        "type": "object",
        "properties": {
            "timestamp": {
                "type": "string",
                "description": r"Matches \d time groups.",
                "const": r"\d literal example",
                "pattern": r"^\d{2}:\d{2}:\d{2}$",
            }
        },
    }

    provider.run_generate("hello", model=LOCAL_MODEL, json_schema=schema)

    posted_timestamp = captured["json"]["response_format"]["json_schema"]["schema"][
        "properties"
    ]["timestamp"]
    assert posted_timestamp["description"] == r"Matches \d time groups."
    assert posted_timestamp["const"] == r"\d literal example"
    assert posted_timestamp["pattern"] == "^[0-9]{2}:[0-9]{2}:[0-9]{2}$"


def test_run_generate_bundled_non_overflow_keeps_body_unmarked(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: SimpleNamespace(
            port=4321,
            base_url="http://127.0.0.1:4321",
            served_model_id=LOCAL_MODEL,
        ),
    )
    from solstone.think.providers import local_budget

    monkeypatch.setattr(local_budget, "count_tokens", lambda text, _base_url: len(text))
    small_block = "## Segment\n### Transcript\nsmall\n"
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": LOCAL_MODEL,
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }

    def fake_post(url, json, timeout):
        captured.update({"url": url, "json": json, "timeout": timeout})
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    result = provider.run_generate(
        [small_block, "talent prompt"],
        model=LOCAL_MODEL,
        max_output_tokens=1024,
        system_instruction="system",
    )

    assert captured["json"]["messages"][1]["content"] == (
        small_block + "\ntalent prompt"
    )
    assert (
        local_budget.TRUNCATION_MARKER not in captured["json"]["messages"][1]["content"]
    )
    assert "input_budget" not in result


def test_run_generate_bundled_preserved_exceeds_budget_skips_post(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: SimpleNamespace(
            port=4321,
            base_url="http://127.0.0.1:4321",
            served_model_id=LOCAL_MODEL,
        ),
    )
    from solstone.think.providers import local_budget

    monkeypatch.setattr(local_budget, "count_tokens", lambda text, _base_url: len(text))

    def fake_post(*_args, **_kwargs):
        raise AssertionError("httpx.post not expected")

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(provider.ContextBudgetExceeded) as exc:
        provider.run_generate(
            "## Segment\n### Transcript\nsmall\n",
            model=LOCAL_MODEL,
            max_output_tokens=8192 * 6,
            system_instruction="s" * 13000,
        )

    assert exc.value.reason_code == "context_budget_exceeded"


def test_run_generate_bundled_context_rejection_backstop(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: SimpleNamespace(
            port=4321,
            base_url="http://127.0.0.1:4321",
            served_model_id=LOCAL_MODEL,
        ),
    )
    from solstone.think.providers import local_budget

    monkeypatch.setattr(local_budget, "count_tokens", lambda text, _base_url: len(text))

    def fake_post(url, json, timeout):
        del json, timeout
        request = httpx.Request("POST", url)
        return httpx.Response(
            400,
            request=request,
            text='{"error":{"message":"the request exceeds the available context size"}}',
        )

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(provider.ContextBudgetExceeded) as exc:
        provider.run_generate("hello", model=LOCAL_MODEL, max_output_tokens=16)

    assert exc.value.reason_code == "context_budget_exceeded"


def test_run_generate_bundled_context_rejection_backstop_alt_phrasing(monkeypatch):
    # llama-server emits a second context-overflow phrasing observed in the
    # field ("Context size has been exceeded.") distinct from the token-count
    # form; the backstop must convert both to a clean ContextBudgetExceeded.
    provider = _provider()
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: SimpleNamespace(
            port=4321,
            base_url="http://127.0.0.1:4321",
            served_model_id=LOCAL_MODEL,
        ),
    )
    from solstone.think.providers import local_budget

    monkeypatch.setattr(local_budget, "count_tokens", lambda text, _base_url: len(text))

    def fake_post(url, json, timeout):
        del json, timeout
        request = httpx.Request("POST", url)
        return httpx.Response(
            400,
            request=request,
            text='{"error":{"message":"Context size has been exceeded."}}',
        )

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(provider.ContextBudgetExceeded) as exc:
        provider.run_generate("hello", model=LOCAL_MODEL, max_output_tokens=16)

    assert exc.value.reason_code == "context_budget_exceeded"


def test_openhands_local_llm_kwargs(monkeypatch):
    from solstone.think.providers import local_server, openhands

    captured = {}
    served_model_id = (
        "/Users/sol/.cache/huggingface/hub/"
        "models--mlx-community--Qwen3.5-9B/snapshots/abc123"
    )

    class FakeLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    sdk_module = types.ModuleType("openhands.sdk")
    sdk_module.LLM = FakeLLM
    monkeypatch.setitem(sys.modules, "openhands.sdk", sdk_module)
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: SimpleNamespace(port=9876, served_model_id=served_model_id),
    )

    llm = openhands._build_llm("local", LOCAL_MODEL)

    assert isinstance(llm, FakeLLM)
    assert captured == {
        "model": f"openai/{served_model_id}",
        "base_url": "http://127.0.0.1:9876/v1",
        "api_key": "EMPTY",
        "native_tool_calling": False,
        "timeout": openhands.LLM_TIMEOUT_S,
        "num_retries": openhands.LLM_NUM_RETRIES,
        "max_input_tokens": local_server.LOCAL_MIN_CONTEXT_TOKENS,
        "max_output_tokens": openhands._LOCAL_OUTPUT_RESERVE_TOKENS,
        "input_cost_per_token": 0,
        "output_cost_per_token": 0,
        "litellm_extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    capable_tier = local_server.select_server_tier(24576)
    assert capable_tier.context_tokens == 32768
    assert captured["max_input_tokens"] == 16384
    assert captured["max_input_tokens"] != capable_tier.context_tokens
    assert "chat_template_kwargs" not in captured
    assert openhands._prefixed_model("local", LOCAL_MODEL) == f"openai/{LOCAL_MODEL}"


def _byo_endpoint(credential: str | None = "test-token-PLACEHOLDER"):
    from solstone.think.providers.local_endpoint import (
        LocalEndpoint,
        normalize_local_endpoint_url,
    )

    return LocalEndpoint(
        base_url=normalize_local_endpoint_url("http://byo.example/openai/v1/"),
        served_model_id="served-model",
        credential=credential,
        is_bundled=False,
    )


def test_run_generate_byo_posts_to_normalized_endpoint_and_skips_connect(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(provider, "resolve_local_endpoint", _byo_endpoint)
    from solstone.think.providers import local_budget

    def fail_count(*_args, **_kwargs):
        raise AssertionError("count_tokens not expected")

    monkeypatch.setattr(local_budget, "count_tokens", fail_count)
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: (_ for _ in ()).throw(AssertionError("connect not expected")),
    )
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {"content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
            }

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    result = provider.run_generate("hello", model=LOCAL_MODEL)

    assert captured["url"] == "http://byo.example/openai/v1/chat/completions"
    assert captured["json"]["model"] == "served-model"
    assert captured["headers"] == {"Authorization": "Bearer test-token-PLACEHOLDER"}
    assert local_budget.TRUNCATION_MARKER not in str(captured["json"])
    assert result["text"] == "hello"


def test_run_generate_byo_body_omits_bundled_qwen_sampling(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(provider, "resolve_local_endpoint", _byo_endpoint)
    captured_posts = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {"content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
            }

    def fake_post(url, **kwargs):
        captured_posts.append({"url": url, **kwargs})
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    schema = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "string"}},
        },
    }

    provider.run_generate(
        "hello",
        model=LOCAL_MODEL,
        temperature=0.4,
        max_output_tokens=7,
    )
    provider.run_generate(
        "hello",
        model=LOCAL_MODEL,
        temperature=0.5,
        max_output_tokens=11,
        json_schema=schema,
    )

    assert len(captured_posts) == 2
    assert captured_posts[0]["json"] == {
        "model": "served-model",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.4,
        "max_tokens": 7,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert captured_posts[1]["json"] == {
        "model": "served-model",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.5,
        "max_tokens": 11,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "local_schema",
                "schema": provider._prepare_local_schema(schema),
                "strict": True,
            },
        },
    }
    for post in captured_posts:
        for key in ("top_p", "top_k", "min_p", "presence_penalty"):
            assert key not in post["json"]


def test_run_generate_byo_omits_auth_header_without_credential(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(provider, "resolve_local_endpoint", lambda: _byo_endpoint(None))
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    provider.run_generate("hello", model=LOCAL_MODEL)

    assert "headers" not in captured


def test_generate_schema_files_do_not_declare_bounds():
    bounded_keys = {
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
    }
    paths = [
        Path("solstone/talent/sense.schema.json"),
        Path("solstone/talent/participation.schema.json"),
        Path("solstone/talent/participation_entry.schema.json"),
    ]
    found = {}

    def walk(node, keys):
        if isinstance(node, dict):
            keys.update(bounded_keys & node.keys())
            for value in node.values():
                walk(value, keys)
        elif isinstance(node, list):
            for item in node:
                walk(item, keys)

    for path in paths:
        keys = set()
        walk(json.loads(path.read_text(encoding="utf-8")), keys)
        if keys:
            found[str(path)] = sorted(keys)

    assert found == {}


def test_run_generate_byo_network_error_maps_to_unreachable(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(provider, "resolve_local_endpoint", _byo_endpoint)

    import httpx

    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            httpx.ConnectError("connection refused")
        ),
    )

    with pytest.raises(provider.LocalProviderError) as exc:
        provider.run_generate("hello", model=LOCAL_MODEL)

    assert exc.value.reason_code == "local_endpoint_unreachable"
    assert str(exc.value) == provider.LOCAL_ENDPOINT_UNREACHABLE_COPY
    assert isinstance(exc.value.__cause__, httpx.ConnectError)


def test_run_generate_byo_http_status_maps_to_contract_failed(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(provider, "resolve_local_endpoint", _byo_endpoint)

    import httpx

    request = httpx.Request("POST", "http://byo.example/openai/v1/chat/completions")
    response = httpx.Response(400, request=request)

    class Response:
        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "bad request", request=request, response=response
            )

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response())

    with pytest.raises(provider.LocalProviderError) as exc:
        provider.run_generate("hello", model=LOCAL_MODEL)

    assert exc.value.reason_code == "local_endpoint_contract_failed"
    assert str(exc.value) == provider.LOCAL_ENDPOINT_CONTRACT_COPY


def test_run_generate_byo_invalid_shape_maps_to_contract_failed(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(provider, "resolve_local_endpoint", _byo_endpoint)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": []}

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response())

    with pytest.raises(provider.LocalProviderError) as exc:
        provider.run_generate("hello", model=LOCAL_MODEL)

    assert exc.value.reason_code == "local_endpoint_contract_failed"
    assert str(exc.value) == provider.LOCAL_ENDPOINT_CONTRACT_COPY


def test_run_generate_byo_json_decode_maps_to_contract_failed(monkeypatch):
    provider = _provider()
    monkeypatch.setattr(provider, "resolve_local_endpoint", _byo_endpoint)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            raise json.JSONDecodeError("bad json", "not-json", 0)

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response())

    with pytest.raises(provider.LocalProviderError) as exc:
        provider.run_generate("hello", model=LOCAL_MODEL)

    assert exc.value.reason_code == "local_endpoint_contract_failed"
    assert str(exc.value) == provider.LOCAL_ENDPOINT_CONTRACT_COPY


def test_run_cogitate_byo_classified_error_uses_fixed_copy_and_redacts(
    monkeypatch,
):
    provider = _provider()
    token = "test-token-PLACEHOLDER"
    events: list[dict] = []

    class BadRequestError(RuntimeError):
        status_code = 400

    async def fail_cogitate(*_args, **_kwargs):
        raise BadRequestError(f"bad request with {token}")

    monkeypatch.setattr(
        provider, "resolve_local_endpoint", lambda: _byo_endpoint(token)
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: (_ for _ in ()).throw(AssertionError("connect not expected")),
    )
    monkeypatch.setattr(
        "solstone.think.providers.openhands.run_cogitate",
        fail_cogitate,
    )

    with pytest.raises(provider.LocalProviderError) as exc:
        asyncio.run(
            provider.run_cogitate({"model": LOCAL_MODEL}, on_event=events.append)
        )

    assert exc.value.reason_code == "local_endpoint_contract_failed"
    assert str(exc.value) == provider.LOCAL_ENDPOINT_CONTRACT_COPY
    assert token not in str(exc.value)
    assert getattr(exc.value, "_evented") is True
    assert events[0]["error"] == provider.LOCAL_ENDPOINT_CONTRACT_COPY
    assert events[0]["reason_code"] == "local_endpoint_contract_failed"
    assert token not in events[0]["trace"]


def test_run_cogitate_talent_hook_error_bypasses_local_error_event(monkeypatch):
    provider = _provider()
    events: list[dict] = []
    hook_exc = TalentHookError(
        "post",
        "broken_hook",
        "chat",
        RuntimeError("hook exploded"),
    )

    async def fail_cogitate(*_args, **_kwargs):
        raise hook_exc

    monkeypatch.setattr(provider, "resolve_local_endpoint", _byo_endpoint)
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: (_ for _ in ()).throw(AssertionError("connect not expected")),
    )
    monkeypatch.setattr(
        "solstone.think.providers.openhands.run_cogitate",
        fail_cogitate,
    )

    with pytest.raises(TalentHookError) as raised:
        asyncio.run(
            provider.run_cogitate({"model": LOCAL_MODEL}, on_event=events.append)
        )

    assert raised.value is hook_exc
    assert events == []
    assert not getattr(hook_exc, "_evented", False)


@pytest.mark.parametrize(
    ("credential", "expected_key"),
    [
        ("test-token-PLACEHOLDER", "test-token-PLACEHOLDER"),
        (None, "EMPTY"),
    ],
)
def test_openhands_local_byo_llm_kwargs(monkeypatch, credential, expected_key):
    from solstone.think.providers import local_endpoint, openhands

    captured = {}

    class FakeLLM:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    sdk_module = types.ModuleType("openhands.sdk")
    sdk_module.LLM = FakeLLM
    monkeypatch.setitem(sys.modules, "openhands.sdk", sdk_module)
    monkeypatch.setattr(
        local_endpoint,
        "resolve_local_endpoint",
        lambda: _byo_endpoint(credential),
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_server.connect",
        lambda: (_ for _ in ()).throw(AssertionError("connect not expected")),
    )

    llm = openhands._build_llm("local", LOCAL_MODEL)

    assert isinstance(llm, FakeLLM)
    assert captured == {
        "model": "openai/served-model",
        "base_url": "http://byo.example/openai/v1",
        "api_key": expected_key,
        "native_tool_calling": False,
        "timeout": openhands.LLM_TIMEOUT_S,
        "num_retries": openhands.LLM_NUM_RETRIES,
        "input_cost_per_token": 0,
        "output_cost_per_token": 0,
        "litellm_extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    assert "max_input_tokens" not in captured


def test_local_context_window_split_floor_vs_tier():
    import inspect

    from solstone.think import supervisor
    from solstone.think.providers import local_server, openhands

    assert local_server.LOCAL_MIN_CONTEXT_TOKENS == 16384
    removed_name = "_".join(("LOCAL", "SERVER", "CONTEXT", "TOKENS"))
    assert not hasattr(local_server, removed_name)
    src = inspect.getsource(supervisor.start_local_server)
    assert "select_server_tier" in src
    assert "tier.context_tokens" in src
    assert '"16384"' not in src
    llm_src = inspect.getsource(openhands._build_llm)
    assert "LOCAL_MIN_CONTEXT_TOKENS" in llm_src


def test_select_server_tier_vram_thresholds():
    from solstone.think.providers import local_server

    cases = [
        (
            0,
            local_server.ServerTier(
                name="floor",
                context_tokens=16384,
                parallel_slots=1,
                prompt_cache_mib=0,
            ),
        ),
        (
            15999,
            local_server.ServerTier(
                name="floor",
                context_tokens=16384,
                parallel_slots=1,
                prompt_cache_mib=0,
            ),
        ),
        (
            16000,
            local_server.ServerTier(
                name="capable",
                context_tokens=32768,
                parallel_slots=2,
                prompt_cache_mib=2048,
            ),
        ),
        (
            24576,
            local_server.ServerTier(
                name="capable",
                context_tokens=32768,
                parallel_slots=2,
                prompt_cache_mib=2048,
            ),
        ),
    ]

    for vram_mib, expected in cases:
        tier = local_server.select_server_tier(vram_mib)
        assert tier == expected
        assert tier.context_tokens >= 16384
        assert tier.context_tokens > 0


@pytest.mark.parametrize(
    ("props", "expected"),
    [
        ({"n_ctx": 32768}, 32768),
        ({"default_generation_settings": {"n_ctx": 16384}}, 16384),
        (
            {"n_ctx": 32768, "default_generation_settings": {"n_ctx": 16384}},
            32768,
        ),
        ({}, None),
        ({"default_generation_settings": {}}, None),
        ({"n_ctx": "abc"}, None),
        ({"n_ctx": None}, None),
        # Numeric strings are acceptable because _extract_n_ctx intentionally
        # uses int() coercion on reported llama-server values.
        ({"n_ctx": "32768"}, 32768),
    ],
)
def test_extract_n_ctx_props_shapes(props, expected):
    from solstone.think.providers import local_server

    assert local_server._extract_n_ctx(props) == expected


def test_read_server_context_window_fetch_props(monkeypatch):
    import httpx

    from solstone.think.providers import local_server

    class FakeResponse:
        status_code = 200

        def __init__(self, body=None, error: Exception | None = None):
            self.body = body
            self.error = error

        def json(self):
            if self.error is not None:
                raise self.error
            return self.body

    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, timeout: FakeResponse({"n_ctx": 32768, "total_slots": 2}),
    )
    assert local_server.read_server_context_window(2468) == 32768

    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, timeout: FakeResponse(error=ValueError("bad json")),
    )
    assert local_server.read_server_context_window(2468) is None

    monkeypatch.setattr(httpx, "get", lambda url, timeout: FakeResponse(["n_ctx"]))
    assert local_server.read_server_context_window(2468) is None

    def raise_get(url, timeout):
        raise RuntimeError("network down")

    monkeypatch.setattr(httpx, "get", raise_get)
    assert local_server.read_server_context_window(2468) is None


def test_context_window_tokens_fallback(monkeypatch):
    from solstone.think import utils
    from solstone.think.providers import local_budget, local_server

    monkeypatch.setattr(utils, "read_service_port", lambda service: 2468)
    monkeypatch.setattr(local_server, "read_server_context_window", lambda port: 32768)
    monkeypatch.setattr(local_server, "read_local_context_window", lambda: None)
    assert local_budget.context_window_tokens() == 32768

    monkeypatch.setattr(local_server, "read_server_context_window", lambda port: None)
    monkeypatch.setattr(local_server, "read_local_context_window", lambda: 32768)
    assert local_budget.context_window_tokens() == 32768

    monkeypatch.setattr(utils, "read_service_port", lambda service: None)
    monkeypatch.setattr(local_server, "read_local_context_window", lambda: None)
    assert local_budget.context_window_tokens() == local_server.LOCAL_MIN_CONTEXT_TOKENS


def test_llama_server_pins_are_real_b9291_digests():
    from solstone.think.providers.local_install import LLAMA_SERVER_PINS

    mac = LLAMA_SERVER_PINS["aarch64-apple-darwin"]
    linux = LLAMA_SERVER_PINS["x86_64-unknown-linux-gnu"]
    assert mac["release_tag"] == "b9291"
    assert mac["filename"] == "llama-b9291-bin-macos-arm64.tar.gz"
    assert (
        mac["sha256"]
        == "0e985f87dd71f96a9cb9ebc3ad26f8388030342d000e7e82d4a38d14913373ff"
    )
    assert linux["release_tag"] == "b9291"
    assert linux["filename"] == "llama-b9291-bin-ubuntu-vulkan-x64.tar.gz"
    assert (
        linux["sha256"]
        == "7e3bf4202bedc71c2c9fbfbe02d10075b8d596bb963e7ab006663582dc2e92c2"
    )


def _select_local_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        "solstone.think.models.get_config",
        lambda: {"providers": {"generate": {"provider": "local"}}},
    )


def test_build_provider_status_local_not_selected_is_inert(monkeypatch):
    from solstone.think.providers import build_provider_status

    health_calls = []
    monkeypatch.setattr(
        "solstone.think.models.get_config",
        lambda: {"providers": {"generate": {"provider": "google"}}},
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_install.inspect_readiness",
        lambda: {
            "binary_installed": True,
            "model_installed": True,
            "ram_sufficient": True,
            "gpu_available": True,
            "binary_path": "/fake/llama-server",
        },
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_server.is_healthy",
        lambda: health_calls.append("health") or True,
    )

    status = build_provider_status(
        [{"name": "local", "label": "Local (on-device)", "env_key": ""}]
    )["local"]

    assert status["selected"] is False
    assert status["configured"] is True
    assert status["generate_ready"] is False
    assert status["cogitate_ready"] is False
    assert status["issues"] == []
    assert health_calls == []


def test_build_provider_status_local_readiness(monkeypatch):
    from solstone.think.providers import build_provider_status

    _select_local_provider(monkeypatch)
    monkeypatch.setattr(
        "solstone.think.providers.local_install.inspect_readiness",
        lambda: {
            "binary_installed": True,
            "model_installed": True,
            "ram_sufficient": True,
            "gpu_available": True,
        },
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_server.is_healthy", lambda: True
    )

    status = build_provider_status(
        [{"name": "local", "label": "Local (on-device)", "env_key": ""}]
    )["local"]

    assert status["configured"] is True
    assert status["generate_ready"] is True
    assert status["cogitate_ready"] is True
    assert status["cogitate_cli"] == "llama-server"
    assert status["issues"] == []


def test_build_provider_status_local_launch_failure_adds_probe_detail_and_hint(
    monkeypatch,
):
    from solstone.think.providers import build_provider_status

    _select_local_provider(monkeypatch)
    detail = "dyld: Library not loaded: @rpath/libllama.dylib"
    monkeypatch.setattr(
        "solstone.think.providers.local_install.inspect_readiness",
        lambda: {
            "binary_installed": True,
            "model_installed": True,
            "ram_sufficient": True,
            "gpu_available": True,
            "binary_path": "/fake/llama-server",
        },
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_server.is_healthy", lambda: False
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_install.probe_binary_runnable",
        lambda _path: (False, detail),
    )

    status = build_provider_status(
        [{"name": "local", "label": "Local (on-device)", "env_key": ""}]
    )["local"]

    assert status["issues"] == [
        f"failed to launch: {detail}",
        "run `journal install-provider local`",
    ]
    assert "server_unhealthy" not in status["issues"]


def test_build_provider_status_local_server_unhealthy_when_probe_runnable(
    monkeypatch,
):
    from solstone.think.providers import build_provider_status

    _select_local_provider(monkeypatch)
    monkeypatch.setattr(
        "solstone.think.providers.local_install.inspect_readiness",
        lambda: {
            "binary_installed": True,
            "model_installed": True,
            "ram_sufficient": True,
            "gpu_available": True,
            "binary_path": "/fake/llama-server",
        },
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_server.is_healthy", lambda: False
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_install.probe_binary_runnable",
        lambda _path: (True, None),
    )

    status = build_provider_status(
        [{"name": "local", "label": "Local (on-device)", "env_key": ""}]
    )["local"]

    assert status["issues"] == ["server_unhealthy"]


def test_build_provider_status_local_healthy_skips_probe(monkeypatch):
    from solstone.think.providers import build_provider_status

    _select_local_provider(monkeypatch)
    calls: list[str] = []

    def probe(_path):
        calls.append(_path)
        return False, "should not run"

    monkeypatch.setattr(
        "solstone.think.providers.local_install.inspect_readiness",
        lambda: {
            "binary_installed": True,
            "model_installed": True,
            "ram_sufficient": True,
            "gpu_available": True,
            "binary_path": "/fake/llama-server",
        },
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_server.is_healthy", lambda: True
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_install.probe_binary_runnable", probe
    )

    status = build_provider_status(
        [{"name": "local", "label": "Local (on-device)", "env_key": ""}]
    )["local"]

    assert status["issues"] == []
    assert calls == []


def test_local_provider_status_carries_install_hint_substring(monkeypatch):
    from solstone.think.providers import build_provider_status

    _select_local_provider(monkeypatch)
    monkeypatch.setattr(
        "solstone.think.providers.local_install.inspect_readiness",
        lambda: {
            "binary_installed": False,
            "model_installed": False,
            "ram_sufficient": False,
            "gpu_available": True,
        },
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_server.is_healthy", lambda: False
    )

    status = build_provider_status(
        [{"name": "local", "label": "Local (on-device)", "env_key": ""}]
    )["local"]

    assert status["configured"] is False
    assert status["generate_ready"] is False
    assert status["cogitate_ready"] is False
    assert status["cogitate_cli"] == "llama-server"
    assert status["cogitate_cli_found"] is False
    assert status["issues"] == [
        "binary_missing",
        "model_missing",
        "run `journal install-provider local`",
    ]
    assert any("journal install-provider local" in issue for issue in status["issues"])


def test_local_provider_status_reports_gpu_unavailable_issue(monkeypatch):
    from solstone.think.providers import build_provider_status

    _select_local_provider(monkeypatch)
    monkeypatch.setattr(
        "solstone.think.providers.local_install.inspect_readiness",
        lambda: {
            "binary_installed": True,
            "model_installed": True,
            "ram_sufficient": True,
            "gpu_available": False,
            "binary_path": "/fake/llama-server",
        },
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_server.is_healthy", lambda: True
    )

    status = build_provider_status(
        [{"name": "local", "label": "Local (on-device)", "env_key": ""}]
    )["local"]

    assert status["issues"] == ["gpu_unavailable"]


def test_build_provider_status_local_configured_ignores_ram_flag(monkeypatch):
    from solstone.think.providers import build_provider_status

    _select_local_provider(monkeypatch)
    monkeypatch.setattr(
        "solstone.think.providers.local_install.inspect_readiness",
        lambda: {
            "binary_installed": True,
            "model_installed": True,
            "ram_sufficient": False,
            "gpu_available": True,
            "binary_path": "/fake/llama-server",
        },
    )
    monkeypatch.setattr(
        "solstone.think.providers.local_server.is_healthy", lambda: True
    )

    status = build_provider_status(
        [{"name": "local", "label": "Local (on-device)", "env_key": ""}]
    )["local"]

    assert status["configured"] is True
    assert status["generate_ready"] is True
    assert status["cogitate_ready"] is True
    assert status["issues"] == []


def test_local_server_connect_returns_healthy_service(monkeypatch):
    from solstone.think.providers import local_server

    monkeypatch.setattr(local_server, "read_service_port", lambda service: 2468)
    monkeypatch.setattr(
        local_server,
        "_fetch_health",
        lambda port: ("ready", None, {"loaded_model": "/path/to/snapshot"}),
    )

    info = local_server.connect()

    assert info.model_id == LOCAL_MODEL
    assert info.served_model_id == "/path/to/snapshot"
    assert info.base_url == "http://127.0.0.1:2468"
    assert info.state == local_server.STATE_READY


def test_resolve_served_model_id_returns_valid_loaded_model_verbatim():
    from solstone.think.providers import local_server

    assert (
        local_server._resolve_served_model_id({"loaded_model": "/snap/dir"})
        == "/snap/dir"
    )


def test_resolve_served_model_id_falls_back_when_loaded_model_absent():
    from solstone.think.providers import local_server

    assert local_server._resolve_served_model_id({}) == LOCAL_MODEL
    assert local_server._resolve_served_model_id(None) == LOCAL_MODEL


@pytest.mark.parametrize(
    "body",
    [
        {"loaded_model": None},
        {"loaded_model": ""},
        {"loaded_model": "   "},
        {"loaded_model": 123},
    ],
)
def test_resolve_served_model_id_rejects_invalid_loaded_model(body):
    from solstone.think.providers import local_server

    assert local_server._resolve_served_model_id(body) is None


def test_local_server_connect_missing_port_raises_named_copy(monkeypatch):
    from solstone.think.providers import local_server

    monkeypatch.setattr(local_server, "read_service_port", lambda service: None)

    with pytest.raises(local_server.LocalProviderError) as exc:
        local_server.connect()

    assert exc.value.reason_code == "local_model_not_ready"
    assert str(exc.value) == local_server.LOCAL_MODEL_NOT_READY_COPY


def test_local_server_connect_failed_health_raises_named_copy(monkeypatch):
    from solstone.think.providers import local_server

    monkeypatch.setattr(local_server, "read_service_port", lambda service: 2468)
    monkeypatch.setattr(
        local_server, "_fetch_health", lambda port: ("starting", None, None)
    )

    with pytest.raises(local_server.LocalProviderError) as exc:
        local_server.connect()

    assert exc.value.reason_code == "local_model_not_ready"
    assert str(exc.value) == local_server.LOCAL_MODEL_NOT_READY_COPY


@pytest.mark.parametrize(
    "body",
    [
        {"loaded_model": None},
        {"loaded_model": ""},
    ],
)
def test_local_server_connect_invalid_loaded_model_raises_named_copy(monkeypatch, body):
    from solstone.think.providers import local_server

    monkeypatch.setattr(local_server, "read_service_port", lambda service: 2468)
    monkeypatch.setattr(
        local_server, "_fetch_health", lambda port: ("ready", None, body)
    )

    with pytest.raises(local_server.LocalProviderError) as exc:
        local_server.connect()

    assert exc.value.reason_code == "local_model_not_ready"
    assert str(exc.value) == local_server.LOCAL_MODEL_NOT_READY_COPY


def test_local_server_connect_linux_health_shape_uses_logical_model(monkeypatch):
    from solstone.think.providers import local_server

    monkeypatch.setattr(local_server, "read_service_port", lambda service: 2468)
    monkeypatch.setattr(
        local_server,
        "_fetch_health",
        lambda port: ("ready", None, {"status": "ok"}),
    )

    info = local_server.connect()

    assert info.model_id == LOCAL_MODEL
    assert info.served_model_id == LOCAL_MODEL
