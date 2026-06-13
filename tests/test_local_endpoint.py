# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import pytest

from solstone.think.providers import local_endpoint


def _config(payload: dict) -> dict:
    return {"providers": {"local": payload}}


@pytest.mark.parametrize(
    ("config", "is_bundled"),
    [
        ({}, True),
        ({"providers": {"local": {}}}, True),
        (_config({"endpoint_url": "http://h:8080"}), True),
        (_config({"served_model_id": "model"}), True),
        ({"providers": {"local": "not-a-dict"}}, True),
        (
            _config(
                {"endpoint_url": " http://h:8080/v1/ ", "served_model_id": " model "}
            ),
            False,
        ),
    ],
)
def test_resolve_local_endpoint_active_requires_url_and_model(
    monkeypatch,
    config,
    is_bundled,
):
    monkeypatch.setattr(local_endpoint, "read_journal_config", lambda: config)

    endpoint = local_endpoint.resolve_local_endpoint()

    assert endpoint.is_bundled is is_bundled
    if not is_bundled:
        assert endpoint.base_url == "http://h:8080"
        assert endpoint.served_model_id == "model"
        assert endpoint.credential is None


def test_resolve_local_endpoint_carries_placeholder_credential(monkeypatch):
    monkeypatch.setattr(
        local_endpoint,
        "read_journal_config",
        lambda: _config(
            {
                "endpoint_url": "http://h:8080",
                "served_model_id": "model",
                "credential": "test-token-PLACEHOLDER",
            }
        ),
    )

    endpoint = local_endpoint.resolve_local_endpoint()

    assert endpoint.is_bundled is False
    assert endpoint.credential == "test-token-PLACEHOLDER"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://h:8080", "http://h:8080"),
        ("http://h:8080/v1", "http://h:8080"),
        ("http://h:8080/v1/", "http://h:8080"),
        (" http://h:8080/openai/v1/ ", "http://h:8080/openai"),
    ],
)
def test_normalize_local_endpoint_url(raw, expected):
    assert local_endpoint.normalize_local_endpoint_url(raw) == expected


def test_probe_local_endpoint_treats_any_response_as_reachable(monkeypatch):
    calls = []

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return object()

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)
    endpoint = local_endpoint.LocalEndpoint(
        base_url="http://h:8080",
        served_model_id="model",
        credential=None,
        is_bundled=False,
    )

    assert local_endpoint.probe_local_endpoint(endpoint, timeout_s=0.2) == (True, None)
    assert calls == [("http://h:8080", 0.2)]


@pytest.mark.parametrize("exc", ["connect", "timeout"])
def test_probe_local_endpoint_reports_transport_failures(monkeypatch, exc):
    import httpx

    error = (
        httpx.ConnectError("connection refused")
        if exc == "connect"
        else httpx.ReadTimeout("too slow")
    )

    def fake_get(url, timeout):
        raise error

    monkeypatch.setattr(httpx, "get", fake_get)
    endpoint = local_endpoint.LocalEndpoint(
        base_url="http://h:8080",
        served_model_id="model",
        credential=None,
        is_bundled=False,
    )

    reachable, detail = local_endpoint.probe_local_endpoint(endpoint)

    assert reachable is False
    assert detail == str(error)


class BadRequestError(Exception):
    status_code = 400


class InternalServerError(Exception):
    status_code = 500


class APIConnectionError(Exception):
    pass


class ConnectError(Exception):
    pass


def test_classify_byo_cogitate_error_contract_by_status_or_name():
    assert (
        local_endpoint.classify_byo_cogitate_error(BadRequestError("bad request"))
        == "local_endpoint_contract_failed"
    )


@pytest.mark.parametrize(
    "inner", [APIConnectionError("api down"), ConnectError("down")]
)
def test_classify_byo_cogitate_error_unreachable_by_cause_chain(inner):
    exc = RuntimeError("outer")
    exc.__cause__ = inner

    assert (
        local_endpoint.classify_byo_cogitate_error(exc) == "local_endpoint_unreachable"
    )


def test_classify_byo_cogitate_error_unreachable_by_internal_server():
    assert (
        local_endpoint.classify_byo_cogitate_error(InternalServerError("connection"))
        == "local_endpoint_unreachable"
    )


def test_classify_byo_cogitate_error_returns_none_for_unknown():
    assert local_endpoint.classify_byo_cogitate_error(RuntimeError("unknown")) is None
