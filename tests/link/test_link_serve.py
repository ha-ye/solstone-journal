# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import argparse
import http.client
import logging
import queue
import socket
import threading
from concurrent.futures import Future
from pathlib import Path

import pytest

from solstone.think.link import serve_cli
from solstone.think.link.client import BodySource
from solstone.think.link.dialer import (
    TunnelLifecycleError,
    TunnelRequestError,
    TunnelResponseHead,
)
from solstone.think.link.paths import DEFAULT_RELAY_URL


class _StubTunnel:
    def __init__(
        self,
        items: list[TunnelResponseHead | bytes | Exception | None],
    ) -> None:
        self.items = items
        self.requests: list[tuple[str, str, dict[str, str], bytes | BodySource]] = []
        self.consumed_bodies: list[bytes] = []
        self.queue_maxsizes: list[int] = []

    def proxy_stream_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | BodySource = b"",
        chunks: queue.Queue[TunnelResponseHead | bytes | Exception | None],
    ) -> Future[None]:
        self.requests.append((method, path, headers or {}, body))
        self.queue_maxsizes.append(chunks.maxsize)
        future: Future[None] = Future()

        def run() -> None:
            self.consumed_bodies.append(_collect_body(body))
            for item in self.items:
                chunks.put(item)
            future.set_result(None)

        threading.Thread(target=run, daemon=True).start()
        return future

    def status(self) -> dict[str, object]:
        return {
            "health": "healthy",
            "state": "connected",
            "manager_alive": True,
            "connected_age_seconds": 12.0,
            "last_connected_at": 1_800_000_000.0,
            "last_failure": None,
            "next_retry_at": None,
            "reconnect_count": 0,
            "active_requests": 0,
        }


def _collect_body(body: bytes | BodySource) -> bytes:
    if isinstance(body, BodySource):
        return b"".join(body.chunks)
    return body


def _start_server(tunnel: object) -> tuple[serve_cli._ProxyServer, threading.Thread]:
    server = serve_cli._build_server(0, tunnel)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_server(server: serve_cli._ProxyServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _connection(server: serve_cli._ProxyServer) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(
        serve_cli.LOOPBACK_HOST,
        server.server_address[1],
        timeout=2,
    )


def _configure_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return xdg / "solstone-observer" / "spl"


def test_serve_request_mapping() -> None:
    tunnel = _StubTunnel(
        [
            TunnelResponseHead(201, {"Content-Length": "2", "X-Test": "yes"}),
            b"ok",
            None,
        ]
    )
    server, thread = _start_server(tunnel)
    try:
        conn = _connection(server)
        conn.request(
            "POST",
            "/app/path?x=1",
            body=b"hello",
            headers={
                "Authorization": "Bearer secret",
                "Content-Length": "5",
                "X-Keep": "value",
            },
        )
        response = conn.getresponse()
        body = response.read()
        conn.close()
    finally:
        _stop_server(server, thread)

    assert response.status == 201
    assert response.getheader("X-Test") == "yes"
    assert body == b"ok"
    method, path, headers, forwarded_body = tunnel.requests[0]
    assert method == "POST"
    assert path == "/app/path?x=1"
    assert isinstance(forwarded_body, BodySource)
    assert forwarded_body.length == 5
    assert tunnel.consumed_bodies == [b"hello"]
    assert tunnel.queue_maxsizes == [serve_cli.RESPONSE_QUEUE_MAX_ITEMS]
    assert headers["Content-Length"] == "5"
    assert headers["Host"] == f"{serve_cli.LOOPBACK_HOST}:{server.server_address[1]}"
    assert headers["X-Keep"] == "value"
    assert "Authorization" not in headers


def test_serve_streaming_incremental() -> None:
    release = threading.Event()
    produced_b = threading.Event()

    class StreamingTunnel:
        def proxy_stream_request(
            self,
            _method: str,
            _path: str,
            *,
            headers: dict[str, str] | None = None,
            body: bytes | BodySource = b"",
            chunks: queue.Queue[TunnelResponseHead | bytes | Exception | None],
        ) -> Future[None]:
            _ = (headers, body)
            future: Future[None] = Future()

            def run() -> None:
                chunks.put(TunnelResponseHead(200, {"Transfer-Encoding": "chunked"}))
                chunks.put(b"1\r\nA\r\n")
                release.wait(timeout=2)
                produced_b.set()
                chunks.put(b"1\r\nB\r\n0\r\n\r\n")
                chunks.put(None)
                future.set_result(None)

            threading.Thread(target=run, daemon=True).start()
            return future

    server, thread = _start_server(StreamingTunnel())
    try:
        conn = _connection(server)
        conn.request("GET", "/events")
        response = conn.getresponse()
        assert response.status == 200
        assert response.read(1) == b"A"
        assert not produced_b.is_set()
        release.set()
        assert response.read(1) == b"B"
        assert response.read() == b""
        conn.close()
    finally:
        release.set()
        _stop_server(server, thread)


def test_serve_rejects_transfer_encoding_before_tunnel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tunnel = _StubTunnel([TunnelResponseHead(200, {"Content-Length": "0"}), None])
    server, thread = _start_server(tunnel)
    caplog.set_level(logging.WARNING, logger=serve_cli.LOG.name)
    try:
        with socket.create_connection(
            (serve_cli.LOOPBACK_HOST, server.server_address[1]),
            timeout=2,
        ) as sock:
            sock.sendall(
                b"POST /chunked HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
                b"1\r\nx\r\n0\r\n\r\n"
            )
            response = sock.recv(4096)
    finally:
        _stop_server(server, thread)

    assert b"400" in response
    assert tunnel.requests == []
    assert "Transfer-Encoding" in caplog.text


def test_serve_times_out_waiting_for_response_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    returned_future: Future[None] = Future()

    class HangingTunnel:
        def proxy_stream_request(
            self,
            _method: str,
            _path: str,
            *,
            headers: dict[str, str] | None = None,
            body: bytes | BodySource = b"",
            chunks: queue.Queue[TunnelResponseHead | bytes | Exception | None],
        ) -> Future[None]:
            _ = (headers, body, chunks)
            return returned_future

    monkeypatch.setattr(serve_cli, "RESPONSE_HEAD_TIMEOUT_SECONDS", 0.05)
    server, thread = _start_server(HangingTunnel())
    try:
        conn = _connection(server)
        conn.request("GET", "/hang")
        response = conn.getresponse()
        body = response.read()
        conn.close()
    finally:
        _stop_server(server, thread)

    assert response.status == 502
    assert b"TimeoutError" in body
    assert returned_future.cancelled()


def test_serve_status_path_is_local_json() -> None:
    tunnel = _StubTunnel([TunnelResponseHead(200, {"Content-Length": "0"}), None])
    server, thread = _start_server(tunnel)
    try:
        conn = _connection(server)
        conn.request("GET", serve_cli.STATUS_PATH)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
    finally:
        _stop_server(server, thread)

    assert response.status == 200
    assert response.getheader("Content-Type") == "application/json"
    assert b'"health": "healthy"' in payload
    assert b'"state": "connected"' in payload
    assert b"secret" not in payload.lower()
    assert tunnel.requests == []


def test_serve_lifecycle_error_is_retryable_json() -> None:
    tunnel = _StubTunnel(
        [
            TunnelLifecycleError("connecting", "no live tunnel session available"),
            None,
        ]
    )
    server, thread = _start_server(tunnel)
    try:
        conn = _connection(server)
        conn.request("GET", "/app/path?token=secret")
        response = conn.getresponse()
        payload = response.read()
        conn.close()
    finally:
        _stop_server(server, thread)

    assert response.status == 503
    assert response.getheader("Content-Type") == "application/json"
    assert b'"error": "link_lifecycle"' in payload
    assert b'"retryable": true' in payload
    assert b'"state": "connecting"' in payload
    assert b"secret" not in payload


def test_serve_midstream_failure() -> None:
    tunnel = _StubTunnel(
        [
            TunnelResponseHead(200, {"Content-Length": "6"}),
            b"abc",
            ConnectionError("lost"),
            None,
        ]
    )
    server, thread = _start_server(tunnel)
    try:
        conn = _connection(server)
        conn.request("GET", "/broken")
        response = conn.getresponse()
        assert response.status == 200
        with pytest.raises(http.client.IncompleteRead):
            response.read()
        conn.close()
    finally:
        _stop_server(server, thread)


def test_serve_bundle_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _configure_xdg(tmp_path, monkeypatch)
    valid = {"solo"}
    (root / "solo").mkdir(parents=True)

    def fake_load(bundle_dir: Path) -> object:
        if bundle_dir.name not in valid:
            raise ValueError("invalid")
        return object()

    monkeypatch.setattr(serve_cli, "load_client_identity", fake_load)

    selection = serve_cli._resolve_bundle_dir(None)
    assert selection.label == "solo"
    assert selection.bundle_dir == root / "solo"

    valid.update({"alpha", "beta"})
    (root / "alpha").mkdir()
    (root / "beta").mkdir()
    with pytest.raises(ValueError) as multiple:
        serve_cli._resolve_bundle_dir(None)
    assert "alpha" in str(multiple.value)
    assert "beta" in str(multiple.value)
    assert "--label" in str(multiple.value)

    with pytest.raises(ValueError) as missing:
        serve_cli._resolve_bundle_dir("missing")
    assert "missing" in str(missing.value)
    assert "sol link join" in str(missing.value)

    valid.clear()
    with pytest.raises(ValueError) as empty:
        serve_cli._resolve_bundle_dir(None)
    assert "no observer link bundles" in str(empty.value)
    assert "sol link join" in str(empty.value)


def test_serve_relay_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://env.example/")
    assert serve_cli._resolve_relay_url("https://flag.example/") == (
        "https://flag.example"
    )
    assert serve_cli._resolve_relay_url(None) == "https://env.example"

    monkeypatch.delenv("SOL_LINK_RELAY_URL", raising=False)
    monkeypatch.setattr("solstone.think.utils.get_config", lambda: {})
    assert serve_cli._resolve_relay_url(None) == DEFAULT_RELAY_URL


def test_serve_loopback_bind() -> None:
    tunnel = _StubTunnel([TunnelResponseHead(200, {"Content-Length": "0"}), None])
    server = serve_cli._build_server(0, tunnel)
    try:
        assert server.server_address[0] == serve_cli.LOOPBACK_HOST
        assert serve_cli.LOOPBACK_HOST == "127.0.0.1"
    finally:
        server.server_close()


def test_serve_port_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sock = socket.socket()
    sock.bind((serve_cli.LOOPBACK_HOST, 0))
    port = sock.getsockname()[1]

    class FakeTunnel:
        def __init__(self, *_args: object) -> None:
            self.closed = False

        def start(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        serve_cli,
        "_resolve_bundle_dir",
        lambda _label: serve_cli._BundleSelection("label", tmp_path),
    )
    monkeypatch.setattr(serve_cli, "load_client_identity", lambda _path: object())
    monkeypatch.setattr(serve_cli, "TunnelClient", FakeTunnel)
    try:
        result = serve_cli.main(
            argparse.Namespace(label=None, port=port, relay_url=None)
        )
    finally:
        sock.close()

    assert result == 1
    captured = capsys.readouterr()
    assert f"{serve_cli.LOOPBACK_HOST}:{port}" in captured.err
    assert "address already in use" in captured.err


def test_serve_gateway_failure() -> None:
    tunnel = _StubTunnel(
        [
            TunnelRequestError("ConnectionError", "down"),
            None,
        ]
    )
    server, thread = _start_server(tunnel)
    try:
        conn = _connection(server)
        conn.request("GET", "/")
        response = conn.getresponse()
        body = response.read()
        conn.close()
    finally:
        _stop_server(server, thread)

    assert response.status == 502
    assert b"ConnectionError" in body
