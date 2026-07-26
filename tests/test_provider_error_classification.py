# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from pathlib import Path

import pytest

from solstone.think.providers import openhands
from solstone.think.providers.cli import ProviderKeyMissingError, QuotaExhaustedError
from solstone.think.providers.shared import (
    _CONTEXT_WINDOW_PATTERNS,
    classify_provider_error,
    mark_cloud_model_request,
)

_OPENHANDS_BAD_REQUEST_GOOGLE_OUTAGE = (
    "litellm.BadRequestError: GeminiException BadRequestError - "
    '{"error": {"code": 400, "message": "Request contains an invalid argument.", '
    '"status": "INVALID_ARGUMENT"}}'
)


def _require_attrs(module, *names: str):
    missing = [name for name in names if not hasattr(module, name)]
    if missing:
        pytest.skip(f"{module.__name__} does not expose {', '.join(missing)}")
    return [getattr(module, name) for name in names]


def test_classifies_quota_exhausted_error():
    exc = QuotaExhaustedError("quota exhausted", retry_delay_ms=1000)
    assert classify_provider_error(exc, "google") == "provider_quota_exceeded"


def test_provider_key_missing_reason_passes_through():
    exc = ProviderKeyMissingError("google", "GOOGLE_API_KEY", "msg")
    assert classify_provider_error(exc, "google") == "provider_key_missing"

    wrapped = RuntimeError("wrapper")
    wrapped.__cause__ = exc

    assert (
        classify_provider_error(openhands._unwrap_provider_exception(wrapped), "google")
        == "provider_key_missing"
    )


def test_classifies_litellm_context_window_exceeded():
    from litellm.exceptions import ContextWindowExceededError

    exc = ContextWindowExceededError(
        "context window exceeded",
        model="gpt-5",
        llm_provider="openai",
    )

    assert classify_provider_error(exc, "openhands") == "context_window_exceeded"


@pytest.mark.parametrize(
    "message",
    [
        "request (16942 tokens) exceeds the available context size (16384 tokens)",
        "prompt exceeds the context window for this model",
        "This model's maximum context length is 16384 tokens",
        "context length exceeded while evaluating prompt",
    ],
)
def test_classifies_litellm_bad_request_context_window_messages(message):
    from litellm.exceptions import BadRequestError

    exc = BadRequestError(message, model="local-model", llm_provider="local")

    assert classify_provider_error(exc, "local") == "context_window_exceeded"


def test_classifies_litellm_bad_request_unrelated_unknown():
    from litellm.exceptions import BadRequestError

    exc = BadRequestError(
        "Invalid value for parameter 'temperature'",
        model="local-model",
        llm_provider="local",
    )

    assert classify_provider_error(exc, "local") == "unknown"
    assert classify_provider_error(exc, "local") != "context_window_exceeded"


def test_classifies_litellm_bad_request_google_request_rejected():
    from litellm.exceptions import BadRequestError

    exc = BadRequestError(
        "Invalid value for parameter 'temperature'",
        model="gemini-test",
        llm_provider="google",
    )

    assert exc.status_code == 400
    assert classify_provider_error(exc, "google") == "provider_request_rejected"


def test_classifies_openhands_bad_request_google_request_rejected():
    from openhands.sdk.llm.exceptions.types import LLMBadRequestError

    exc = LLMBadRequestError(_OPENHANDS_BAD_REQUEST_GOOGLE_OUTAGE)

    assert classify_provider_error(exc, "google") == "provider_request_rejected"


def _litellm_not_found_error():
    from litellm.exceptions import NotFoundError

    return NotFoundError("model not found", model="m", llm_provider="gemini")


def _openai_response(status_code: int):
    import httpx

    request = httpx.Request("GET", "https://api.openai.example/models/missing")
    return httpx.Response(status_code, request=request)


def _openai_not_found_error():
    import openai

    return openai.NotFoundError(
        "model not found",
        response=_openai_response(404),
        body=None,
    )


def _mapped_provider_exception(exc: Exception) -> Exception:
    from openhands.sdk.llm.exceptions.mapping import map_provider_exception

    return map_provider_exception(exc)


@pytest.mark.parametrize(
    "factory",
    [
        _litellm_not_found_error,
        _openai_not_found_error,
        lambda: _mapped_provider_exception(_litellm_not_found_error()),
        lambda: _mapped_provider_exception(_openai_not_found_error()),
    ],
)
def test_cloud_not_found_shapes_classify_model_not_found(factory):
    for provider in ("google", "openai", "anthropic"):
        assert classify_provider_error(factory(), provider) == "model_not_found"


@pytest.mark.parametrize(
    "factory",
    [
        _litellm_not_found_error,
        _openai_not_found_error,
        lambda: _mapped_provider_exception(_litellm_not_found_error()),
        lambda: _mapped_provider_exception(_openai_not_found_error()),
    ],
)
def test_local_not_found_shapes_stay_unknown(factory):
    assert classify_provider_error(factory(), "local") == "unknown"


def test_anthropic_not_found_shape_classifies_model_not_found_when_installed():
    import httpx

    anthropic = pytest.importorskip("anthropic")
    (not_found_error,) = _require_attrs(anthropic, "NotFoundError")
    request = httpx.Request("GET", "https://api.anthropic.example/v1/models/missing")
    response = httpx.Response(404, request=request)
    exc = not_found_error("model not found", response=response, body=None)

    for provider in ("google", "openai", "anthropic"):
        assert classify_provider_error(exc, provider) == "model_not_found"
    assert classify_provider_error(exc, "local") == "unknown"


class ProviderModelLookupError(Exception):
    status_code = 404


class ProviderWrapperError(Exception):
    status_code = 500


def test_marked_cloud_model_request_404_classifies_model_not_found():
    for provider in ("google", "openai", "anthropic"):
        exc = ProviderModelLookupError("missing model")
        mark_cloud_model_request(exc)
        assert classify_provider_error(exc, provider) == "model_not_found"


def test_marked_cloud_model_request_uses_status_from_chain_after_unwrap():
    inner = ProviderModelLookupError("missing model")
    outer = RuntimeError("transport wrapper")
    outer.__cause__ = inner

    mark_cloud_model_request(outer)

    assert classify_provider_error(outer, "google") == "model_not_found"
    assert classify_provider_error(inner, "google") == "model_not_found"


def test_marked_cloud_model_request_uses_inner_404_when_outer_has_status():
    inner = ProviderModelLookupError("missing model")
    outer = ProviderWrapperError("transport wrapper")
    outer.__cause__ = inner

    mark_cloud_model_request(outer)

    assert classify_provider_error(outer, "google") == "model_not_found"
    assert classify_provider_error(inner, "google") == "model_not_found"


def test_unmarked_cloud_model_request_404_stays_unknown():
    for provider in ("google", "openai", "anthropic", "local"):
        assert classify_provider_error(
            ProviderModelLookupError("missing"), provider
        ) == ("unknown")

    inner = ProviderModelLookupError("missing model")
    outer = RuntimeError("transport wrapper")
    outer.__cause__ = inner

    assert classify_provider_error(outer, "google") == "unknown"


def test_lookalike_not_found_name_without_trusted_identity_stays_unknown():
    class WidgetNotFoundError(Exception):
        status_code = 404

    assert classify_provider_error(WidgetNotFoundError("missing"), "google") == (
        "unknown"
    )


def test_status_only_404_shapes_do_not_classify_model_not_found():
    import httpx

    class StatusOnly:
        status_code = 404

    class ResponseOnly:
        response = httpx.Response(
            404, request=httpx.Request("GET", "https://example.invalid/missing")
        )

    unrelated = httpx.HTTPStatusError(
        "not found",
        request=httpx.Request("GET", "https://example.invalid/unrelated"),
        response=httpx.Response(
            404, request=httpx.Request("GET", "https://example.invalid/unrelated")
        ),
    )

    for exc in (StatusOnly(), ResponseOnly(), unrelated):
        for provider in ("google", "openai", "local"):
            assert classify_provider_error(exc, provider) == "unknown"


def test_sibling_classifications_stay_ahead_of_model_not_found():
    import httpx
    import openai
    from litellm.exceptions import BadRequestError

    auth = openai.AuthenticationError(
        "bad key",
        response=_openai_response(401),
        body=None,
    )
    quota = openai.RateLimitError(
        "rate limit",
        response=_openai_response(429),
        body=None,
    )
    ordinary_400 = BadRequestError(
        "Invalid value for parameter 'temperature'",
        model="gemini-test",
        llm_provider="google",
    )
    status_500 = httpx.HTTPStatusError(
        "server error",
        request=httpx.Request("GET", "https://api.example.invalid/models"),
        response=httpx.Response(
            500, request=httpx.Request("GET", "https://api.example.invalid/models")
        ),
    )
    status_503 = httpx.HTTPStatusError(
        "unavailable",
        request=httpx.Request("GET", "https://api.example.invalid/models"),
        response=httpx.Response(
            503, request=httpx.Request("GET", "https://api.example.invalid/models")
        ),
    )

    cases = [
        (auth, "provider_key_invalid"),
        (quota, "provider_quota_exceeded"),
        (ordinary_400, "provider_request_rejected"),
        (status_500, "provider_unavailable"),
        (status_503, "provider_unavailable"),
        (httpx.ReadTimeout("timeout"), "chat_timeout"),
        (httpx.ConnectError("connection failed"), "network_unreachable"),
        (Exception("plain failure"), "unknown"),
    ]

    for exc, expected in cases:
        assert classify_provider_error(exc, "google") == expected


@pytest.mark.parametrize("message", _CONTEXT_WINDOW_PATTERNS)
def test_context_window_messages_stay_context_window_before_model_not_found(message):
    from litellm.exceptions import BadRequestError

    exc = BadRequestError(message, model="gemini-test", llm_provider="google")

    assert classify_provider_error(exc, "google") == "context_window_exceeded"


@pytest.mark.parametrize("message", _CONTEXT_WINDOW_PATTERNS)
def test_classifies_openhands_bad_request_context_window_messages_before_rejection(
    message,
):
    from openhands.sdk.llm.exceptions.types import LLMBadRequestError

    exc = LLMBadRequestError(message)

    assert classify_provider_error(exc, "google") == "context_window_exceeded"


def test_preserves_existing_openhands_llm_exception_classifications():
    from openhands.sdk.llm.exceptions.types import (
        LLMAuthenticationError,
        LLMContextWindowExceedError,
        LLMContextWindowTooSmallError,
        LLMNoResponseError,
        LLMRateLimitError,
        LLMResponseError,
        LLMServiceUnavailableError,
        LLMTimeoutError,
    )

    cases = [
        (LLMAuthenticationError("auth failed"), "provider_key_invalid"),
        (LLMContextWindowExceedError("context window"), "context_window_exceeded"),
        (LLMContextWindowTooSmallError(8192), "context_window_exceeded"),
        (LLMRateLimitError("rate limit"), "provider_quota_exceeded"),
        (LLMServiceUnavailableError("unavailable"), "provider_unavailable"),
        (LLMTimeoutError("timeout"), "chat_timeout"),
        (LLMNoResponseError("no response"), "provider_response_invalid"),
        (LLMResponseError("bad response"), "provider_response_invalid"),
    ]

    for exc, expected in cases:
        assert classify_provider_error(exc, "google") == expected


def test_openhands_bad_request_local_not_request_rejected():
    from openhands.sdk.llm.exceptions.types import LLMBadRequestError

    exc = LLMBadRequestError(_OPENHANDS_BAD_REQUEST_GOOGLE_OUTAGE)

    assert classify_provider_error(exc, "local") != "provider_request_rejected"


def test_bare_openhands_llm_error_not_request_rejected():
    from openhands.sdk.llm.exceptions.types import LLMError

    assert classify_provider_error(LLMError("boom"), "google") != (
        "provider_request_rejected"
    )


def test_preserves_existing_litellm_cloud_exception_classifications():
    from litellm.exceptions import (
        ContextWindowExceededError,
        InternalServerError,
        RateLimitError,
        ServiceUnavailableError,
        Timeout,
    )

    cases = [
        (
            Timeout("timeout", model="gemini-test", llm_provider="google"),
            408,
            "chat_timeout",
        ),
        (
            InternalServerError(
                "internal server error",
                model="gemini-test",
                llm_provider="google",
            ),
            500,
            "provider_unavailable",
        ),
        (
            RateLimitError(
                "rate limit",
                model="gemini-test",
                llm_provider="google",
            ),
            429,
            "provider_quota_exceeded",
        ),
        (
            ContextWindowExceededError(
                "context window exceeded",
                model="gemini-test",
                llm_provider="google",
            ),
            400,
            "context_window_exceeded",
        ),
        (
            ServiceUnavailableError(
                "unavailable",
                model="gemini-test",
                llm_provider="google",
            ),
            503,
            "unknown",
        ),
    ]

    for exc, status_code, expected in cases:
        assert exc.status_code == status_code
        assert classify_provider_error(exc, "google") == expected


def test_runtime_reason_codes_are_registered_with_owner_copy():
    from solstone.convey import provider_readiness
    from solstone.think.providers.shared import RUNTIME_REASON_CODES

    projection = provider_readiness.chat_reason_projection()
    chat_reasons = Path("solstone/convey/static/chat_reasons.js").read_text(
        encoding="utf-8"
    )

    for reason_code in (
        "context_window_exceeded",
        "context_budget_exceeded",
        "model_not_found",
        "provider_request_rejected",
    ):
        assert reason_code in RUNTIME_REASON_CODES
        assert reason_code in provider_readiness.mapped_reason_codes()
        assert reason_code in projection
        assert f'"{reason_code}"' in chat_reasons


def test_classifies_max_turns_exhausted():
    from solstone.think.cogitate_policy import MaxTurnsExhausted

    exc = MaxTurnsExhausted("max_turns_exhausted: OpenHands cogitate exceeded 60 turns")

    assert classify_provider_error(exc, "openhands") == "max_turns_exhausted"


def test_classifies_builtin_connection_error():
    assert classify_provider_error(ConnectionError("offline"), "google") == (
        "network_unreachable"
    )


def test_classifies_no_response_value_error():
    exc = ValueError("No response from model")
    assert classify_provider_error(exc, "google") == "provider_response_invalid"


def test_classifies_generic_exception_unknown():
    assert classify_provider_error(Exception("anything"), "google") == "unknown"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Gemini CLI not installed", "provider_unavailable"),
        ("command not found: gemini", "provider_unavailable"),
        ("timed out after 30s", "chat_timeout"),
        ("authentication failed", "provider_key_invalid"),
        ("unauthorized 401", "provider_key_invalid"),
        ("unexpected failure", "unknown"),
    ],
)
def test_classifies_cli_runtime_patterns(message, expected):
    assert classify_provider_error(RuntimeError(message), "google") == expected


def test_classifies_anthropic_sdk_auth_and_permission_errors():
    anthropic = pytest.importorskip("anthropic")
    httpx = pytest.importorskip("httpx")
    authentication_error, permission_denied_error = _require_attrs(
        anthropic, "AuthenticationError", "PermissionDeniedError"
    )
    request = httpx.Request("GET", "https://api.anthropic.com")

    for cls, status_code in (
        (authentication_error, 401),
        (permission_denied_error, 403),
    ):
        response = httpx.Response(status_code, request=request)
        exc = cls("auth failed", response=response, body={})
        assert classify_provider_error(exc, "anthropic") == "provider_key_invalid"


def test_classifies_anthropic_sdk_rate_timeout_network_and_5xx_errors():
    anthropic = pytest.importorskip("anthropic")
    httpx = pytest.importorskip("httpx")
    (
        rate_limit_error,
        api_timeout_error,
        api_connection_error,
        api_status_error,
    ) = _require_attrs(
        anthropic,
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "APIStatusError",
    )
    request = httpx.Request("GET", "https://api.anthropic.com")

    rate_response = httpx.Response(429, request=request)
    rate_exc = rate_limit_error("rate limited", response=rate_response, body={})
    assert classify_provider_error(rate_exc, "anthropic") == ("provider_quota_exceeded")

    assert classify_provider_error(api_timeout_error(request), "anthropic") == (
        "chat_timeout"
    )
    assert (
        classify_provider_error(api_connection_error(request=request), "anthropic")
        == "network_unreachable"
    )

    server_response = httpx.Response(503, request=request)
    server_exc = api_status_error("server failed", response=server_response, body={})
    assert classify_provider_error(server_exc, "anthropic") == "provider_unavailable"


def test_classifies_openai_sdk_auth_and_permission_errors():
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    request = httpx.Request("GET", "https://api.openai.com")

    for cls, status_code in (
        (openai.AuthenticationError, 401),
        (openai.PermissionDeniedError, 403),
    ):
        response = httpx.Response(status_code, request=request)
        exc = cls("auth failed", response=response, body={})
        assert classify_provider_error(exc, "openai") == "provider_key_invalid"


def test_classifies_openai_sdk_rate_timeout_network_and_5xx_errors():
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")
    request = httpx.Request("GET", "https://api.openai.com")

    rate_response = httpx.Response(429, request=request)
    rate_exc = openai.RateLimitError("rate limited", response=rate_response, body={})
    assert classify_provider_error(rate_exc, "openai") == "provider_quota_exceeded"

    assert classify_provider_error(openai.APITimeoutError(request), "openai") == (
        "chat_timeout"
    )
    assert (
        classify_provider_error(openai.APIConnectionError(request=request), "openai")
        == "network_unreachable"
    )

    server_response = httpx.Response(500, request=request)
    server_exc = openai.InternalServerError(
        "server failed", response=server_response, body={}
    )
    assert classify_provider_error(server_exc, "openai") == "provider_unavailable"


def test_classifies_httpx_errors():
    httpx = pytest.importorskip("httpx")
    request = httpx.Request("GET", "http://localhost:11434")

    assert (
        classify_provider_error(
            httpx.ConnectError("connect failed", request=request), "local"
        )
        == "network_unreachable"
    )
    assert (
        classify_provider_error(httpx.ReadTimeout("timeout", request=request), "local")
        == "chat_timeout"
    )

    response = httpx.Response(503, request=request)
    status_exc = httpx.HTTPStatusError(
        "server failed", request=request, response=response
    )
    assert classify_provider_error(status_exc, "local") == "provider_unavailable"


def test_classifies_local_reason_code():
    from solstone.think.providers.local import LocalProviderError

    assert (
        classify_provider_error(LocalProviderError("model_missing", "missing"), "local")
        == "model_missing"
    )
