# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import itertools

import httpx
import pytest

from solstone.think.providers import local_endpoint
from tests.test_local import _FAKE_MODELS_BODY


def _endpoint(
    *,
    model: str = "Qwen/Qwen3.5-4B",
    credential: str | None = None,
    confidential: bool = False,
) -> local_endpoint.LocalEndpoint:
    return local_endpoint.LocalEndpoint(
        base_url="http://byo.example/openai",
        served_model_id=model,
        credential=credential,
        is_bundled=False,
        is_confidential=confidential,
    )


def _config(local_config: dict | None = None) -> dict:
    return {"providers": {"local": local_config or {}}}


def _response(url: str, status_code: int, text: str) -> httpx.Response:
    return httpx.Response(
        status_code,
        request=httpx.Request("GET", url),
        text=text,
    )


@pytest.fixture(autouse=True)
def _reset_cache():
    local_endpoint.reset_endpoint_served_window_cache()
    yield
    local_endpoint.reset_endpoint_served_window_cache()


def test_resolve_endpoint_served_window_discovers_max_model_len(monkeypatch):
    calls = []
    monkeypatch.setattr(local_endpoint, "read_journal_config", lambda: _config())

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _response(url, 200, _FAKE_MODELS_BODY)

    monkeypatch.setattr(httpx, "get", fake_get)

    assert (
        local_endpoint.resolve_endpoint_served_window(
            _endpoint(credential="test-token-PLACEHOLDER")
        )
        == 16384
    )
    assert calls == [
        {
            "url": "http://byo.example/openai/v1/models",
            "timeout": local_endpoint.ENDPOINT_MODELS_TIMEOUT_S,
            "headers": {"Authorization": "Bearer test-token-PLACEHOLDER"},
        }
    ]


@pytest.mark.parametrize(
    "body",
    [
        '{"object":"list","data":[{"id":"Qwen/Qwen3.5-4B"}]}',
        '{"object":"list","data":[{"id":"Qwen/Qwen3.5-4B","max_model_len":"16384"}]}',
        '{"object":"list","data":[{"id":"other-model","max_model_len":16384}]}',
    ],
)
def test_resolve_endpoint_served_window_returns_none_for_bad_model_shape(
    monkeypatch,
    body,
):
    monkeypatch.setattr(local_endpoint, "read_journal_config", lambda: _config())
    monkeypatch.setattr(httpx, "get", lambda url, **_kwargs: _response(url, 200, body))

    assert local_endpoint.resolve_endpoint_served_window(_endpoint()) is None


def test_resolve_endpoint_served_window_override_wins_without_network(monkeypatch):
    monkeypatch.setattr(
        local_endpoint,
        "read_journal_config",
        lambda: _config({"served_context_window": 32768}),
    )

    def fail_get(*_args, **_kwargs):
        raise AssertionError("override should skip discovery")

    monkeypatch.setattr(httpx, "get", fail_get)

    assert local_endpoint.resolve_endpoint_served_window(_endpoint()) == 32768


@pytest.mark.parametrize("raw", [True, False, 2047, 1.5, "16384", [], {}])
def test_resolve_endpoint_served_window_invalid_override_warns_and_discovers(
    monkeypatch,
    caplog,
    raw,
):
    monkeypatch.setattr(
        local_endpoint,
        "read_journal_config",
        lambda: _config({"served_context_window": raw}),
    )
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **_kwargs: _response(url, 200, _FAKE_MODELS_BODY),
    )

    assert local_endpoint.resolve_endpoint_served_window(_endpoint()) == 16384
    assert (
        f"Invalid providers.local.served_context_window in journal config: {raw!r} - "
        "falling through to endpoint discovery"
    ) in caplog.text


def test_resolve_endpoint_served_window_confidential_none_warns(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(local_endpoint, "read_journal_config", lambda: _config())
    monkeypatch.setattr(
        "solstone.think.services.spp_transport.confidential_egress_base_url",
        lambda base_url: (
            "http://127.0.0.1:4567"
            if base_url == "http://byo.example/openai"
            else base_url
        ),
    )

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _response(
            url,
            200,
            '{"object":"list","data":[{"id":"other","max_model_len":16384}]}',
        )

    monkeypatch.setattr(httpx, "get", fake_get)

    assert (
        local_endpoint.resolve_endpoint_served_window(_endpoint(confidential=True))
        is None
    )
    assert calls[0]["url"] == "http://127.0.0.1:4567/v1/models"
    assert (
        "Could not resolve served context window for confidential local endpoint"
        in (caplog.text)
    )


def test_resolve_endpoint_served_window_cache_ttl_and_override(monkeypatch):
    bodies = iter(
        [
            _FAKE_MODELS_BODY,
            (
                '{"object":"list","data":[{"id":"Qwen/Qwen3.5-4B",'
                '"max_model_len":32768}]}'
            ),
        ]
    )
    calls = []
    current_config = {"value": _config()}
    times = itertools.chain(
        [
            100.0,
            101.0,
            100.0 + local_endpoint.ENDPOINT_SERVED_WINDOW_CACHE_TTL_S + 1.0,
        ],
        itertools.repeat(999.0),
    )
    monkeypatch.setattr(
        local_endpoint,
        "read_journal_config",
        lambda: current_config["value"],
    )
    monkeypatch.setattr(local_endpoint.time, "monotonic", lambda: next(times))

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return _response(url, 200, next(bodies))

    monkeypatch.setattr(httpx, "get", fake_get)
    endpoint = _endpoint()

    assert local_endpoint.resolve_endpoint_served_window(endpoint) == 16384
    assert local_endpoint.resolve_endpoint_served_window(endpoint) == 16384
    current_config["value"] = _config({"served_context_window": 24576})
    assert local_endpoint.resolve_endpoint_served_window(endpoint) == 24576
    current_config["value"] = _config()
    assert local_endpoint.resolve_endpoint_served_window(endpoint) == 32768
    assert len(calls) == 2


@pytest.mark.parametrize(
    "fake_get",
    [
        lambda url, **_kwargs: (_ for _ in ()).throw(httpx.ConnectError("down")),
        lambda url, **_kwargs: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
        lambda url, **_kwargs: _response(url, 500, "server error"),
        lambda url, **_kwargs: _response(url, 200, "{"),
        lambda url, **_kwargs: _response(url, 200, '{"object":"list"}'),
        lambda url, **_kwargs: _response(url, 200, '{"data":{}}'),
    ],
)
def test_resolve_endpoint_served_window_failures_return_none(monkeypatch, fake_get):
    monkeypatch.setattr(local_endpoint, "read_journal_config", lambda: _config())
    monkeypatch.setattr(httpx, "get", fake_get)

    assert local_endpoint.resolve_endpoint_served_window(_endpoint()) is None
