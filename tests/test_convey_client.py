# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from typing import Any

import pytest
import requests
import typer
from typer.testing import CliRunner

from solstone.think import convey_client
from solstone.think.convey_client import (
    MALFORMED_RESPONSE_MESSAGE,
    SERVER_ERROR_MESSAGE,
    TIMEOUT_MESSAGE,
    UNREACHABLE_MESSAGE,
    ConveyClient,
    ConveyClientError,
    ConveyTimeoutError,
    ConveyUnreachableError,
    convey_cli,
    paginate_collection,
    resolve_base_url,
)


class FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, queued: list[FakeResponse | Exception]) -> None:
        self.queued = queued
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return self._next()

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        if "files" in kwargs:
            source = kwargs["files"]
            items = source.items() if isinstance(source, dict) else source
            recorded_files = []
            for field, (filename, handle, content_type) in items:
                content = handle.read() if hasattr(handle, "read") else handle
                recorded_files.append((field, (filename, content, content_type)))
            if isinstance(source, dict):
                recorded_files = dict(recorded_files)
            kwargs = {**kwargs, "files": recorded_files}
        self.calls.append(("POST", url, kwargs))
        return self._next()

    def close(self) -> None:
        self.closed = True

    def _next(self) -> FakeResponse:
        item = self.queued.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RecordingSession:
    def __init__(self, queued: list[FakeResponse | Exception]) -> None:
        self.queued = queued
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("GET", url, kwargs)

    def delete(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("DELETE", url, kwargs)

    def put(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("PUT", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._record("POST", url, kwargs)

    def close(self) -> None:
        self.closed = True

    def _record(self, method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        item = self.queued.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _json_response(status_code: int, body: dict[str, Any]) -> FakeResponse:
    return FakeResponse(status_code, json.dumps(body))


def test_paginate_collection_pages_until_total_and_honors_top() -> None:
    session = RecordingSession(
        [
            _json_response(200, {"items": [{"id": "a"}, {"id": "b"}], "total": 5}),
            _json_response(200, {"items": [{"id": "c"}, {"id": "d"}], "total": 5}),
            _json_response(200, {"items": [{"id": "e"}], "total": 5}),
        ]
    )
    client = ConveyClient(session=session, base_url="http://localhost:5015")

    assert [
        item["id"] for item in paginate_collection(client, "/api/items", page_size=2)
    ] == ["a", "b", "c", "d", "e"]
    assert [call[1] for call in session.calls] == [
        "http://localhost:5015/api/items?limit=2&offset=0",
        "http://localhost:5015/api/items?limit=2&offset=2",
        "http://localhost:5015/api/items?limit=2&offset=4",
    ]

    session = RecordingSession(
        [
            _json_response(200, {"items": [{"id": "a"}, {"id": "b"}], "total": 5}),
            _json_response(200, {"items": [{"id": "c"}, {"id": "d"}], "total": 5}),
        ]
    )
    client = ConveyClient(session=session, base_url="http://localhost:5015")

    assert [
        item["id"]
        for item in paginate_collection(client, "/api/items", page_size=2, top=3)
    ] == ["a", "b", "c"]
    assert [call[1] for call in session.calls] == [
        "http://localhost:5015/api/items?limit=2&offset=0",
        "http://localhost:5015/api/items?limit=2&offset=2",
    ]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, {"success": True, "x": 1}),
        (200, {"items": [1, 2], "total": 2}),
        (201, {"id": "abc", "name": "X"}),
    ],
)
def test_success_responses_pass_through(status: int, body: dict[str, Any]) -> None:
    fake = FakeSession([_json_response(status, body)])
    client = ConveyClient(session=fake, base_url="http://localhost:5015")

    assert client.request("GET", "/api/x") == body


@pytest.mark.parametrize("status", [400, 500])
def test_error_envelope_decodes_and_translator_prints_verbatim(status: int) -> None:
    body = {"error": "Owner message.", "reason_code": "FOO", "detail": "ctx"}
    direct = ConveyClient(
        session=FakeSession([_json_response(status, body)]),
        base_url="http://localhost:5015",
    )

    with pytest.raises(ConveyClientError) as excinfo:
        direct.request("GET", "/api/x")

    err = excinfo.value
    assert err.error == "Owner message."
    assert err.reason_code == "FOO"
    assert err.detail == "ctx"
    assert err.status == status

    wrapped = ConveyClient(
        session=FakeSession([_json_response(status, body)]),
        base_url="http://localhost:5015",
    )
    app = typer.Typer()

    @app.command()
    @convey_cli
    def command() -> None:
        wrapped.request("GET", "/api/x")

    result = CliRunner().invoke(app)

    assert result.exit_code == 1
    assert result.stderr.strip() == "Owner message."


@pytest.mark.parametrize("text", ["", "<html>"])
def test_malformed_2xx_raises_client_error(text: str) -> None:
    client = ConveyClient(
        session=FakeSession([FakeResponse(200, text)]),
        base_url="http://localhost:5015",
    )

    with pytest.raises(ConveyClientError) as excinfo:
        client.request("GET", "/api/x")

    assert excinfo.value.error == MALFORMED_RESPONSE_MESSAGE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[1, 2]", [1, 2]),
        ('"hi"', "hi"),
        ("42", 42),
        ("4.5", 4.5),
        ("true", True),
        ("null", None),
    ],
)
def test_non_dict_success_bodies_pass_through(text: str, expected: Any) -> None:
    client = ConveyClient(
        session=FakeSession([FakeResponse(200, text)]),
        base_url="http://localhost:5015",
    )

    assert client.request("GET", "/api/x") == expected


def test_non_envelope_error_raises_server_error_message() -> None:
    client = ConveyClient(
        session=FakeSession([FakeResponse(500, "<html>oops</html>")]),
        base_url="http://localhost:5015",
    )

    with pytest.raises(ConveyClientError) as excinfo:
        client.request("GET", "/api/x")

    assert excinfo.value.error == SERVER_ERROR_MESSAGE


def test_resolve_base_url_uses_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        convey_client,
        "get_host_url_override",
        lambda: "http://192.168.1.44:5015",
    )
    monkeypatch.setattr(convey_client, "read_service_port", lambda service: 5099)

    assert resolve_base_url() == "http://192.168.1.44:5015"


def test_resolve_base_url_uses_recorded_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(convey_client, "get_host_url_override", lambda: None)
    monkeypatch.setattr(convey_client, "read_service_port", lambda service: 5099)

    assert resolve_base_url() == "http://localhost:5099"


def test_resolve_base_url_uses_default_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(convey_client, "get_host_url_override", lambda: None)
    monkeypatch.setattr(convey_client, "read_service_port", lambda service: None)

    assert resolve_base_url() == "http://localhost:5015"


def test_convey_client_reuses_single_session() -> None:
    fake = FakeSession(
        [
            _json_response(200, {"success": True, "first": True}),
            _json_response(200, {"success": True, "second": True}),
        ]
    )
    client = ConveyClient(session=fake, base_url="http://localhost:5015")

    assert client.request("GET", "/api/first") == {"success": True, "first": True}
    assert client.request("POST", "/api/second", json={"x": 1}) == {
        "success": True,
        "second": True,
    }
    assert len(fake.calls) == 2
    assert client._session is fake


def test_upload_posts_multipart_files_and_data(tmp_path) -> None:
    file_path = tmp_path / "shot.png"
    file_path.write_bytes(b"known image bytes")
    fake = FakeSession([_json_response(201, {"id": "att-1"})])
    client = ConveyClient(session=fake, base_url="http://localhost:5015")

    body = client.upload(
        "/app/support/api/tickets/1/attachments",
        files={"file": ("shot.png", str(file_path), "image/png")},
        data={"k": "v"},
    )

    assert body == {"id": "att-1"}
    assert len(fake.calls) == 1
    method, url, kwargs = fake.calls[0]
    assert method == "POST"
    assert url == "http://localhost:5015/app/support/api/tickets/1/attachments"
    assert kwargs["files"] == {"file": ("shot.png", b"known image bytes", "image/png")}
    assert kwargs["data"] == {"k": "v"}
    assert "json" not in kwargs
    assert "headers" not in kwargs


def test_upload_passes_headers_and_in_memory_files() -> None:
    fake = FakeSession([_json_response(201, {"id": "att-1"})])
    client = ConveyClient(session=fake, base_url="http://localhost:5015")

    body = client.upload(
        "/app/observer/ingest",
        files={"files": ("browser_host.jsonl", b'{"ok":true}\n', "application/jsonl")},
        data={"k": "v"},
        headers={"X-Solstone-Observer": "handle123"},
    )

    assert body == {"id": "att-1"}
    method, url, kwargs = fake.calls[0]
    assert method == "POST"
    assert url == "http://localhost:5015/app/observer/ingest"
    assert kwargs["files"] == {
        "files": ("browser_host.jsonl", b'{"ok":true}\n', "application/jsonl")
    }
    assert kwargs["headers"] == {"X-Solstone-Observer": "handle123"}


def test_upload_allows_repeated_file_fields() -> None:
    fake = FakeSession([_json_response(200, {"status": "ok"})])
    client = ConveyClient(session=fake, base_url="http://localhost:5015")

    client.upload(
        "/app/observer/ingest",
        files=[
            ("files", ("a.jsonl", b"a\n", "application/jsonl")),
            ("files", ("b.jsonl", b"b\n", "application/jsonl")),
        ],
    )

    assert fake.calls[0][2]["files"] == [
        ("files", ("a.jsonl", b"a\n", "application/jsonl")),
        ("files", ("b.jsonl", b"b\n", "application/jsonl")),
    ]


def test_default_session_applies_api_timeout_to_request_verbs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = RecordingSession([_json_response(200, {"ok": True}) for _ in range(4)])
    monkeypatch.setattr(convey_client.requests, "Session", lambda: rec)
    client = ConveyClient(base_url="http://localhost:5015")

    client.request("GET", "/api/get")
    client.request("POST", "/api/post", json={"x": 1})
    client.request("PUT", "/api/put", json={"x": 2})
    client.request("DELETE", "/api/delete")

    assert [method for method, _url, _kwargs in rec.calls] == [
        "GET",
        "POST",
        "PUT",
        "DELETE",
    ]
    for _method, _url, kwargs in rec.calls:
        assert kwargs["timeout"] is convey_client.API_TIMEOUT


def test_default_session_applies_upload_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    file_path = tmp_path / "shot.png"
    file_path.write_bytes(b"known image bytes")
    rec = RecordingSession([_json_response(201, {"id": "att-1"})])
    monkeypatch.setattr(convey_client.requests, "Session", lambda: rec)
    client = ConveyClient(base_url="http://localhost:5015")

    client.upload(
        "/app/support/api/tickets/1/attachments",
        files={"file": ("shot.png", str(file_path), "image/png")},
        data={"k": "v"},
    )

    assert len(rec.calls) == 1
    method, _url, kwargs = rec.calls[0]
    assert method == "POST"
    assert kwargs["timeout"] is convey_client.UPLOAD_TIMEOUT


def test_injected_session_receives_no_timeout_kwargs(tmp_path) -> None:
    file_path = tmp_path / "shot.png"
    file_path.write_bytes(b"known image bytes")
    rec = RecordingSession([_json_response(200, {"ok": True}) for _ in range(5)])
    client = ConveyClient(session=rec, base_url="http://localhost:5015")

    client.request("GET", "/api/get")
    client.request("POST", "/api/post", json={"x": 1})
    client.request("PUT", "/api/put", json={"x": 2})
    client.request("DELETE", "/api/delete")
    client.upload(
        "/app/support/api/tickets/1/attachments",
        files={"file": ("shot.png", str(file_path), "image/png")},
        data={"k": "v"},
    )

    assert [method for method, _url, _kwargs in rec.calls] == [
        "GET",
        "POST",
        "PUT",
        "DELETE",
        "POST",
    ]
    for _method, _url, kwargs in rec.calls:
        assert "timeout" not in kwargs


@pytest.mark.parametrize(
    "transport_exc",
    [
        requests.exceptions.ConnectTimeout(),
        requests.exceptions.ReadTimeout(),
        requests.exceptions.Timeout(),
    ],
)
def test_timeout_exceptions_raise_sanitized_timeout_error(
    transport_exc: requests.exceptions.Timeout,
) -> None:
    client = ConveyClient(
        session=RecordingSession([transport_exc]),
        base_url="http://localhost:5015",
        require_service=False,
    )

    with pytest.raises(ConveyTimeoutError) as excinfo:
        client.request("GET", "/api/x")

    err = excinfo.value
    assert not isinstance(err, ConveyUnreachableError)
    assert err.error == TIMEOUT_MESSAGE
    assert err.reason_code == "local_convey_timeout"
    assert err.status is None
    assert (
        err.detail == "GET /api/x exceeded local convey timeout "
        "(connect=2s, read=20s, total=30s)"
    )
    assert "Traceback" not in err.detail
    assert "ConnectTimeout" not in err.detail
    assert "ReadTimeout" not in err.detail
    assert "Timeout" not in err.detail


def test_transport_failure_uses_require_solstone_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("SOL_SKIP_SUPERVISOR_CHECK", raising=False)
    monkeypatch.delenv("SOL_SUPERVISOR_SPAWNED", raising=False)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    client = ConveyClient(
        session=FakeSession([requests.exceptions.ConnectionError()]),
        base_url="http://localhost:5015",
    )

    with pytest.raises(SystemExit) as excinfo:
        client.request("GET", "/api/x")

    captured = capsys.readouterr()
    assert excinfo.value.code == 1
    assert "sol: solstone isn't running. Start it with 'journal up' and retry." in (
        captured.err
    )


def test_transport_failure_falls_through_when_service_probe_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(convey_client, "require_solstone", lambda: None)
    client = ConveyClient(
        session=FakeSession([requests.exceptions.ConnectionError("refused")]),
        base_url="http://localhost:5015",
    )

    with pytest.raises(ConveyClientError) as excinfo:
        client.request("GET", "/api/x")

    err = excinfo.value
    assert err.error == convey_client.UNREACHABLE_MESSAGE
    assert err.detail


def test_require_service_false_raises_unreachable_without_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probed: list[str] = []
    monkeypatch.setattr(
        convey_client,
        "require_solstone",
        lambda: probed.append("require_solstone"),
    )
    client = ConveyClient(
        session=FakeSession([requests.exceptions.ConnectionError("refused")]),
        base_url="http://localhost:5015",
        require_service=False,
    )

    with pytest.raises(ConveyUnreachableError) as excinfo:
        client.request("GET", "/api/x")

    assert probed == []
    assert excinfo.value.error == UNREACHABLE_MESSAGE
    assert not isinstance(excinfo.value, ConveyTimeoutError)


def test_convey_cli_preserves_typer_exit_code() -> None:
    app = typer.Typer()

    @app.command()
    @convey_cli
    def command() -> None:
        raise typer.Exit(2)

    result = CliRunner().invoke(app)

    assert result.exit_code == 2


def test_convey_cli_handles_unreachable_subclass() -> None:
    app = typer.Typer()

    @app.command()
    @convey_cli
    def command() -> None:
        raise ConveyUnreachableError(UNREACHABLE_MESSAGE)

    result = CliRunner().invoke(app)

    assert result.exit_code == 1
    assert result.stderr.strip() == UNREACHABLE_MESSAGE
