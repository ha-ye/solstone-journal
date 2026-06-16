# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sys
import types
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


def _provider():
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


def test_openhands_local_llm_kwargs(monkeypatch):
    from solstone.think.providers import openhands

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
        "input_cost_per_token": 0,
        "output_cost_per_token": 0,
        "litellm_extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
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
    assert result["text"] == "hello"


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
