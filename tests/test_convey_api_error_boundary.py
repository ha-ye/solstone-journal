# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging

import pytest
from flask import Flask

from solstone.convey import install_api_error_handlers
from solstone.convey.request_id import install_request_id_stamper


@pytest.fixture
def app() -> Flask:
    application = Flask(__name__)
    application.config["TESTING"] = False
    install_request_id_stamper(application)
    install_api_error_handlers(application)

    @application.get("/api/test/boom")
    def api_boom() -> None:
        raise RuntimeError("secret implementation detail")

    @application.get("/test/boom")
    def html_boom() -> None:
        raise RuntimeError("html implementation detail")

    return application


def test_unexpected_api_exception_returns_safe_json_and_logs_request_id(
    app: Flask,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="solstone.convey"):
        response = app.test_client().get("/api/test/boom")

    request_id = response.headers["X-Solstone-Request-Id"]
    assert request_id
    assert response.status_code == 500
    assert response.is_json
    assert response.get_json() == {
        "detail": "",
        "error": "I couldn't complete that request.",
        "reason_code": "internal_error",
    }
    assert "secret implementation detail" not in response.get_data(as_text=True)
    assert any(
        f"request_id={request_id}" in record.getMessage() for record in caplog.records
    )


def test_http_exception_on_api_path_preserves_status_in_json(app: Flask) -> None:
    response = app.test_client().get("/api/not-found")

    assert response.status_code == 404
    assert response.is_json
    assert response.get_json() == {
        "detail": "",
        "error": "I couldn't complete that request.",
        "reason_code": "http_error",
    }


def test_non_api_http_exception_keeps_default_html(app: Flask) -> None:
    response = app.test_client().get("/not-found")

    assert response.status_code == 404
    assert response.content_type.startswith("text/html")
    assert not response.is_json


def test_non_api_unexpected_exception_keeps_default_html(app: Flask) -> None:
    response = app.test_client().get("/test/boom")

    assert response.status_code == 500
    assert response.content_type.startswith("text/html")
    assert not response.is_json
