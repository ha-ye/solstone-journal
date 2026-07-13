# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import sys
import types
from unittest.mock import Mock, patch

import httpx
import pytest

from solstone.think.providers import anthropic, google, openai, validate_model


@pytest.fixture(autouse=True)
def reset_google_backend_cache():
    original = google._detected_backend
    google._detected_backend = None
    yield
    google._detected_backend = original


@pytest.fixture(autouse=True)
def install_google_errors_stub(monkeypatch):
    errors_mod = types.ModuleType("google.genai.errors")

    class ClientError(Exception):
        def __init__(self, code: int, response_json: object):
            self.code = code
            self.status = "NOT_FOUND" if code == 404 else "UNAUTHENTICATED"
            super().__init__(f"{code} {self.status}. {response_json}")

    ClientError.__module__ = "google.genai.errors"
    errors_mod.ClientError = ClientError
    monkeypatch.setitem(sys.modules, "google.genai.errors", errors_mod)
    if "google.genai" in sys.modules:
        monkeypatch.setattr(
            sys.modules["google.genai"], "errors", errors_mod, raising=False
        )


def _anthropic_error(exc_type: type[BaseException], status_code: int) -> BaseException:
    import anthropic as anthropic_sdk

    response = httpx.Response(
        status_code,
        request=httpx.Request("GET", "https://api.anthropic.com/v1/models/model"),
    )
    assert issubclass(exc_type, anthropic_sdk.APIStatusError)
    return exc_type(
        "provider error", response=response, body={"error": "provider error"}
    )


def _openai_error(exc_type: type[BaseException], status_code: int) -> BaseException:
    import openai as openai_sdk

    response = httpx.Response(
        status_code,
        request=httpx.Request("GET", "https://api.openai.com/v1/models/model"),
    )
    assert issubclass(exc_type, openai_sdk.APIStatusError)
    return exc_type(
        "provider error", response=response, body={"error": "provider error"}
    )


def _google_client_error(status_code: int) -> BaseException:
    from google.genai.errors import ClientError

    status = "NOT_FOUND" if status_code == 404 else "UNAUTHENTICATED"
    return ClientError(
        status_code,
        {"error": {"message": "provider error", "status": status}},
    )


def test_validate_model_anthropic_success():
    client = Mock()

    with patch("anthropic.Anthropic", return_value=client) as mock_cls:
        result = anthropic.validate_model("claude-opus-4-8", "test-key")

    assert result == {"valid": True}
    mock_cls.assert_called_once_with(api_key="test-key", timeout=10)
    client.models.retrieve.assert_called_once_with("claude-opus-4-8")


def test_validate_model_anthropic_auth_error():
    import anthropic as anthropic_sdk

    client = Mock()
    client.models.retrieve.side_effect = _anthropic_error(
        anthropic_sdk.AuthenticationError,
        401,
    )

    with patch("anthropic.Anthropic", return_value=client):
        result = anthropic.validate_model("claude-opus-4-8", "bad-key")

    assert result["valid"] is False
    assert result["reason_code"] == "provider_key_invalid"


def test_validate_model_anthropic_model_not_found():
    import anthropic as anthropic_sdk

    client = Mock()
    client.models.retrieve.side_effect = _anthropic_error(
        anthropic_sdk.NotFoundError,
        404,
    )

    with patch("anthropic.Anthropic", return_value=client):
        result = anthropic.validate_model("missing-model", "test-key")

    assert result["valid"] is False
    assert result["reason_code"] == "model_not_found"


def test_validate_model_openai_success():
    client = Mock()

    with patch("openai.OpenAI", return_value=client) as mock_cls:
        result = openai.validate_model("gpt-5.5", "test-key")

    assert result == {"valid": True}
    mock_cls.assert_called_once_with(api_key="test-key", timeout=10)
    client.models.retrieve.assert_called_once_with("gpt-5.5")


def test_validate_model_openai_auth_error():
    import openai as openai_sdk

    client = Mock()
    client.models.retrieve.side_effect = _openai_error(
        openai_sdk.AuthenticationError,
        401,
    )

    with patch("openai.OpenAI", return_value=client):
        result = openai.validate_model("gpt-5.5", "bad-key")

    assert result["valid"] is False
    assert result["reason_code"] == "provider_key_invalid"


def test_validate_model_openai_model_not_found():
    import openai as openai_sdk

    client = Mock()
    client.models.retrieve.side_effect = _openai_error(openai_sdk.NotFoundError, 404)

    with patch("openai.OpenAI", return_value=client):
        result = openai.validate_model("missing-model", "test-key")

    assert result["valid"] is False
    assert result["reason_code"] == "model_not_found"


def test_validate_model_google_success():
    client = Mock()

    with (
        patch("google.genai.Client", return_value=client) as mock_cls,
        patch.object(google, "_probe_backend", return_value="aistudio"),
    ):
        result = google.validate_model("gemini-flash-latest", "test-key")

    assert result == {"valid": True}
    assert mock_cls.call_args.kwargs["api_key"] == "test-key"
    assert mock_cls.call_args.kwargs["vertexai"] is False
    assert mock_cls.call_args.kwargs["http_options"].timeout == 10000
    client.models.get.assert_called_once_with(model="gemini-flash-latest")


def test_validate_model_google_auth_error():
    client = Mock()
    client.models.get.side_effect = _google_client_error(401)

    with (
        patch("google.genai.Client", return_value=client),
        patch.object(google, "_probe_backend", return_value="aistudio"),
    ):
        result = google.validate_model("gemini-flash-latest", "bad-key")

    assert result["valid"] is False
    assert result["reason_code"] == "provider_key_invalid"


def test_validate_model_google_model_not_found():
    client = Mock()
    client.models.get.side_effect = _google_client_error(404)

    with (
        patch("google.genai.Client", return_value=client),
        patch.object(google, "_probe_backend", return_value="aistudio"),
    ):
        result = google.validate_model("missing-model", "test-key")

    assert result["valid"] is False
    assert result["reason_code"] == "model_not_found"


def test_validate_model_google_vertex_alias_uses_probed_backend():
    client = Mock()

    with (
        patch("google.genai.Client", return_value=client) as mock_cls,
        patch.object(google, "_probe_backend", return_value="vertex"),
    ):
        result = google.validate_model("gemini-pro-latest", "test-key")

    assert result == {"valid": True}
    assert mock_cls.call_args.kwargs["vertexai"] is True
    client.models.get.assert_called_once_with(model="gemini-3.1-pro-preview")


def test_validate_model_google_does_not_mutate_detected_backend():
    client = Mock()
    google._detected_backend = None

    with (
        patch("google.genai.Client", return_value=client),
        patch.object(google, "_probe_backend", return_value="vertex"),
    ):
        result = google.validate_model("gemini-flash-latest", "test-key")

    assert result == {"valid": True}
    assert google._detected_backend is None


def test_resolve_model_for_vertex_still_uses_active_backend():
    with patch.object(google, "_active_backend", return_value="aistudio"):
        assert (
            google._resolve_model_for_vertex("gemini-pro-latest") == "gemini-pro-latest"
        )
    with patch.object(google, "_active_backend", return_value="vertex"):
        assert (
            google._resolve_model_for_vertex("gemini-pro-latest")
            == "gemini-3.1-pro-preview"
        )


def test_validate_model_dispatcher_success():
    with patch(
        "solstone.think.providers.google.validate_model",
        return_value={"valid": True},
    ) as mock_validate:
        result = validate_model("google", "gemini-pro-latest", "test-key")

    assert result == {"valid": True}
    mock_validate.assert_called_once_with("gemini-pro-latest", "test-key")
