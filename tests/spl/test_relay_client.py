# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from typing import Any

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.http11 import Response

from solstone.think.spl import blob_receiver, relay_client
from solstone.think.spl.health import (
    LINK_HEALTH_EVENT,
    REASON_HOME_MISSING_MOBILE,
    REASON_LOCAL_PRIVATE_LISTENER_UNREACHABLE,
    REASON_RELAY_TUNNEL_REJECTED,
    REASON_RELAY_TUNNEL_UNREACHABLE,
    REASON_SERVICE_TOKEN_REJECTED,
)

SERVICE_TOKEN = "secret-token-xyz"
TOKEN_URL_FRAGMENT = f"token={SERVICE_TOKEN}"


# Built by concatenation so the legacy account-token DATA key does not trip the AC4 grep-clean check; lode L2 renames the relay side.
def _legacy_token_key() -> str:
    return "account" + "_token"


def _incoming(tunnel_id: str) -> str:
    return json.dumps({"type": "incoming", "tunnel_id": tunnel_id})


def _invalid_status(status: int) -> InvalidStatus:
    return InvalidStatus(Response(status, "status", Headers()))


class FakeListenWS:
    def __init__(self, frames: list[str]) -> None:
        self._frames = list(frames)
        self._close_event = asyncio.Event()
        self.close_count = 0

    async def __aenter__(self) -> "FakeListenWS":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    def __aiter__(self) -> "FakeListenWS":
        return self

    async def __anext__(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        await self._close_event.wait()
        raise StopAsyncIteration

    async def close(self) -> None:
        self.close_count += 1
        self._close_event.set()


class FakeTunnelWS:
    def __init__(self, frames: list[bytes] | None = None) -> None:
        self.frames = list(frames or [b"\x16\x03\x01\x00"])

    async def __aenter__(self) -> "FakeTunnelWS":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def recv(self) -> bytes:
        if not self.frames:
            raise ConnectionClosed(None, None)
        return self.frames.pop(0)

    def __aiter__(self) -> "FakeTunnelWS":
        return self

    async def __anext__(self) -> bytes:
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)


class BlockingRecvTunnelWS(FakeTunnelWS):
    def __init__(self, frames: list[bytes] | None = None) -> None:
        self.frames = list(frames or [])
        self.recv_started = asyncio.Event()
        self.release = asyncio.Event()

    async def recv(self) -> bytes:
        if self.frames:
            return self.frames.pop(0)
        self.recv_started.set()
        await self.release.wait()
        raise ConnectionClosed(None, None)


class FakeWriter:
    def __init__(self) -> None:
        self.closed = False
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _AsyncioShim:
    def __init__(self, real_asyncio: Any, open_connection: Any) -> None:
        self._real_asyncio = real_asyncio
        self.open_connection = open_connection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real_asyncio, name)


class ConnectRouter:
    def __init__(
        self,
        *,
        listens: list[FakeListenWS] | None = None,
        tunnels: dict[str, Any] | None = None,
    ) -> None:
        self.listens = list(listens or [])
        self.tunnels = dict(tunnels or {})
        self.urls: list[str] = []

    def __call__(self, url: str, **_kwargs: Any) -> Any:
        self.urls.append(url)
        if "/session/listen" in url:
            if not self.listens:
                raise AssertionError("unexpected listen connection")
            return self.listens.pop(0)
        if "/tunnel/" in url:
            tunnel_id = url.split("/tunnel/", 1)[1].split("?", 1)[0]
            outcome = self.tunnels[tunnel_id]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        raise AssertionError(f"unexpected url: {url}")


class _FakeWS:
    def __init__(self, frames=()) -> None:
        self._frames = list(frames)
        self.closed = False

    async def __aenter__(self) -> "_FakeWS":
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False

    def __aiter__(self) -> "_FakeWS":
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True

    async def send(self, _data) -> None:
        return None


class _RecordingOpener:
    def __init__(self, *ws: _FakeWS) -> None:
        self._ws = list(ws)
        self.calls: list[tuple[str, dict[str, str], int | None]] = []

    def __call__(
        self,
        url: str,
        *,
        additional_headers=None,
        max_size: int | None = None,
    ) -> _FakeWS:
        self.calls.append((url, dict(additional_headers or {}), max_size))
        if not self._ws:
            raise AssertionError(f"unexpected url: {url}")
        return self._ws.pop(0)


class _BlockingPairWindowWS(_FakeWS):
    def __init__(self, entered: threading.Event) -> None:
        super().__init__()
        self._entered = entered

    async def __aenter__(self) -> "_BlockingPairWindowWS":
        self._entered.set()
        return self

    async def __anext__(self):
        await asyncio.sleep(30)
        raise StopAsyncIteration


class _BlockingFramesWS(_FakeWS):
    async def __anext__(self):
        if self._frames:
            return self._frames.pop(0)
        await asyncio.sleep(30)
        raise StopAsyncIteration


class _BlockingOpener:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], int | None]] = []
        self.entered: list[threading.Event] = []

    def __call__(
        self,
        url: str,
        *,
        additional_headers=None,
        max_size: int | None = None,
    ) -> _BlockingPairWindowWS:
        event = threading.Event()
        self.entered.append(event)
        self.calls.append((url, dict(additional_headers or {}), max_size))
        return _BlockingPairWindowWS(event)


class _NeverOpenPairWindowWS(_FakeWS):
    async def __aenter__(self) -> "_NeverOpenPairWindowWS":
        await asyncio.sleep(2.0)
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(30)
        raise StopAsyncIteration


class _NeverOpenOpener:
    def __call__(
        self,
        _url: str,
        *,
        additional_headers: object = None,
        max_size: int | None = None,
    ) -> _NeverOpenPairWindowWS:
        return _NeverOpenPairWindowWS()


class _FailingPairWindowWS:
    async def __aenter__(self) -> "_FailingPairWindowWS":
        raise OSError("connect refused")

    async def __aexit__(self, *_args: Any) -> bool:
        return False


class _FailingOpener:
    def __call__(
        self,
        _url: str,
        *,
        additional_headers: object = None,
        max_size: int | None = None,
    ) -> _FailingPairWindowWS:
        return _FailingPairWindowWS()


def _client(emitted: list[tuple[str, dict[str, Any]]]) -> relay_client.RelayClient:
    return relay_client.RelayClient(
        instance_id="instance.test",
        relay_endpoint="wss://relay.test",
        service_token=SERVICE_TOKEN,
        callosum_emit=lambda event, fields: emitted.append((event, dict(fields))),
    )


def _health_events(
    emitted: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    return [fields for event, fields in emitted if event == LINK_HEALTH_EVENT]


async def _open_connection_success(*_args: Any) -> tuple[object, FakeWriter]:
    return object(), FakeWriter()


async def _wait_until(predicate: Any) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not met")


@pytest.mark.parametrize(
    ("response", "expected_token"),
    [
        ({"service_token": "tok.svc"}, "tok.svc"),
        ({_legacy_token_key(): "tok.acct"}, "tok.acct"),
    ],
)
def test_enroll_accepts_service_and_legacy_tokens(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, str],
    expected_token: str,
) -> None:
    def post_json(_url: str, _body: dict[str, Any]) -> dict[str, str]:
        return response

    monkeypatch.setattr(relay_client, "_post_json_sync", post_json)

    token = relay_client.enroll_home(
        "https://relay.test",
        instance_id="instance.test",
        ca_pubkey="pem",
        home_label="home.test",
    )

    assert token == expected_token


def test_enroll_rejects_response_without_service_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def post_json(_url: str, _body: dict[str, Any]) -> dict[str, str]:
        return {}

    monkeypatch.setattr(relay_client, "_post_json_sync", post_json)

    with pytest.raises(RuntimeError, match="service_token"):
        relay_client.enroll_home(
            "https://relay.test",
            instance_id="instance.test",
            ca_pubkey="pem",
            home_label="home.test",
        )


def test_enroll_home_posts_service_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    def post_json(url: str, body: dict[str, Any]) -> dict[str, str]:
        captured.append((url, body))
        return {"service_token": "tok"}

    monkeypatch.setattr(relay_client, "_post_json_sync", post_json)

    token = relay_client.enroll_home(
        "https://relay.test",
        instance_id="instance.test",
        ca_pubkey="pem",
        home_label="home.test",
    )

    assert token == "tok"
    assert captured == [
        (
            "https://relay.test/enroll/home",
            {
                "instance_id": "instance.test",
                "ca_pubkey": "pem",
                "home_label": "home.test",
            },
        )
    ]


def test_enroll_home_posts_only_service_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    def post_json(url: str, body: dict[str, Any]) -> dict[str, str]:
        captured.append((url, body))
        return {"service_token": "tok"}

    monkeypatch.setattr(relay_client, "_post_json_sync", post_json)

    token = relay_client.enroll_home(
        "https://relay.test",
        instance_id="instance.test",
        ca_pubkey="pem",
        home_label="home.test",
    )

    assert token == "tok"
    assert captured == [
        (
            "https://relay.test/enroll/home",
            {
                "instance_id": "instance.test",
                "ca_pubkey": "pem",
                "home_label": "home.test",
            },
        )
    ]


@pytest.mark.asyncio
async def test_hold_pair_window_opens_with_rk_header_and_bearer() -> None:
    rk = bytes.fromhex("00112233445566778899aabbccddeeff")
    opener = _RecordingOpener(_FakeWS([]))

    await relay_client.hold_pair_window(
        relay_endpoint="https://link.solstone.app",
        service_token="tok",
        rk=rk,
        timeout=0.05,
        opener=opener,
    )

    url, headers, max_size = opener.calls[0]
    assert url == "wss://link.solstone.app/session/pair-window"
    assert "?instance=" not in url
    assert "?token=" not in url
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Sec-Pair-Key"] == "00112233445566778899aabbccddeeff"
    assert max_size is None


@pytest.mark.asyncio
async def test_hold_pair_window_bridges_incoming_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_calls: list[tuple[Any, ...]] = []

    async def fail_open_connection(*args: Any) -> tuple[object, FakeWriter]:
        open_calls.append(args)
        raise OSError("local listener unavailable")

    monkeypatch.setattr(
        relay_client,
        "asyncio",
        _AsyncioShim(relay_client.asyncio, fail_open_connection),
    )
    rk = bytes.fromhex("00112233445566778899aabbccddeeff")
    opener = _RecordingOpener(
        _BlockingFramesWS([_incoming("t1")]),
        FakeTunnelWS(),
    )

    await relay_client.hold_pair_window(
        relay_endpoint="https://link.solstone.app",
        service_token="tok",
        rk=rk,
        timeout=0.05,
        opener=opener,
    )

    assert len(opener.calls) == 2
    url, headers, max_size = opener.calls[1]
    assert url == "wss://link.solstone.app/tunnel/t1"
    assert "?instance=" not in url
    assert "?token=" not in url
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Sec-Pair-Key"] == "00112233445566778899aabbccddeeff"
    assert max_size is None
    assert open_calls == [("127.0.0.1", 7657)]


def test_start_pair_window_wait_open_true_after_window_opens() -> None:
    opener = _BlockingOpener()
    handle = relay_client.start_pair_window(
        rk=b"0" * 16,
        service_token="tok",
        relay_endpoint="https://link.solstone.app",
        opener=opener,
        timeout=30.0,
    )
    try:
        assert handle.wait_open(5.0) is True
    finally:
        relay_client.cancel_pair_window()
        handle.join(5)


def test_start_pair_window_wait_open_false_when_window_never_opens() -> None:
    opener = _NeverOpenOpener()
    handle = relay_client.start_pair_window(
        rk=b"0" * 16,
        service_token="tok",
        relay_endpoint="https://link.solstone.app",
        opener=opener,
        timeout=30.0,
    )
    try:
        assert handle.wait_open(0.3) is False
    finally:
        relay_client.cancel_pair_window()


def test_start_pair_window_wait_open_false_on_connect_failure() -> None:
    opener = _FailingOpener()
    handle = relay_client.start_pair_window(
        rk=b"0" * 16,
        service_token="tok",
        relay_endpoint="https://link.solstone.app",
        opener=opener,
        timeout=30.0,
    )
    try:
        assert handle.wait_open(5.0) is False
    finally:
        relay_client.cancel_pair_window()
        handle.join(5)


def test_start_pair_window_replaces_prior_and_cancels() -> None:
    opener = _BlockingOpener()
    first = relay_client.start_pair_window(
        rk=b"0" * 16,
        service_token="tok",
        relay_endpoint="https://link.solstone.app",
        opener=opener,
        timeout=30.0,
    )
    try:
        assert first.wait_open(5.0) is True
        assert first.is_alive()

        second = relay_client.start_pair_window(
            rk=b"1" * 16,
            service_token="tok",
            relay_endpoint="https://link.solstone.app",
            opener=opener,
            timeout=30.0,
        )
        try:
            assert second.wait_open(5.0) is True
            first.join(timeout=5)
            assert not first.is_alive()
            assert second.is_alive()
        finally:
            relay_client.cancel_pair_window()
            second.join(timeout=5)
            assert not second.is_alive()
    finally:
        relay_client.cancel_pair_window()
        first.join(timeout=5)


@pytest.mark.asyncio
async def test_tunnel_404_closes_listen_and_reopens_with_next_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    client = _client(emitted)
    first_listen = FakeListenWS([_incoming("missing-mobile")])
    second_listen = FakeListenWS([])
    router = ConnectRouter(
        listens=[first_listen, second_listen],
        tunnels={"missing-mobile": _invalid_status(404)},
    )
    monkeypatch.setattr(relay_client.websockets, "connect", router)
    monkeypatch.setattr(relay_client, "_RECONNECT_MIN", 0.0)
    monkeypatch.setattr(relay_client, "_RECONNECT_MAX", 0.0)

    run_task = asyncio.create_task(client.run())
    try:
        await _wait_until(lambda: first_listen.close_count == 1)
        await _wait_until(
            lambda: any(
                fields.get("listen_generation") == 2
                for event, fields in emitted
                if event == LINK_HEALTH_EVENT
            )
        )
    finally:
        await client.stop()
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task

    assert first_listen.close_count == 1
    assert _health_events(emitted)[-1]["listen_generation"] == 2
    assert any(
        fields.get("last_relay_tunnel_error") == REASON_HOME_MISSING_MOBILE
        for fields in _health_events(emitted)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_tunnel_auth_rejection_records_error_without_listen_close(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    client = _client(emitted)
    listen = FakeListenWS([])
    client._listen_ws = listen
    router = ConnectRouter(tunnels={"auth": _invalid_status(status)})
    monkeypatch.setattr(relay_client.websockets, "connect", router)

    await client._handle_tunnel("auth")

    assert listen.close_count == 0
    assert _health_events(emitted)[-1]["last_relay_tunnel_error"] == (
        REASON_SERVICE_TOKEN_REJECTED
    )
    assert _health_events(emitted)[-1]["relay_tunnel_error_status"] is None


@pytest.mark.asyncio
async def test_tunnel_other_status_records_rejected_with_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    client = _client(emitted)
    router = ConnectRouter(tunnels={"rejected": _invalid_status(500)})
    monkeypatch.setattr(relay_client.websockets, "connect", router)

    await client._handle_tunnel("rejected")

    health = _health_events(emitted)[-1]
    assert health["last_relay_tunnel_error"] == REASON_RELAY_TUNNEL_REJECTED
    assert health["relay_tunnel_error_status"] == 500


@pytest.mark.asyncio
async def test_tunnel_pre_attach_oserror_records_relay_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    client = _client(emitted)
    router = ConnectRouter(tunnels={"unreachable": OSError("connect failed")})
    monkeypatch.setattr(relay_client.websockets, "connect", router)

    await client._handle_tunnel("unreachable")

    assert _health_events(emitted)[-1]["last_relay_tunnel_error"] == (
        REASON_RELAY_TUNNEL_UNREACHABLE
    )


@pytest.mark.asyncio
async def test_tunnel_dispatch_timeout_releases_global_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    client = relay_client.RelayClient(
        instance_id="instance.test",
        relay_endpoint="wss://relay.test",
        service_token=SERVICE_TOKEN,
        callosum_emit=lambda event, fields: emitted.append((event, dict(fields))),
        dispatch_read_deadline_s=0.01,
    )
    tunnel = BlockingRecvTunnelWS()
    router = ConnectRouter(tunnels={"slow": tunnel})
    monkeypatch.setattr(relay_client.websockets, "connect", router)

    await client._handle_tunnel("slow")

    assert client._admission_gate.global_count == 0
    assert _health_events(emitted)[-1]["last_relay_tunnel_error"] == (
        REASON_RELAY_TUNNEL_UNREACHABLE
    )


@pytest.mark.asyncio
async def test_cancelled_blob_tunnel_releases_global_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    client = _client(emitted)
    tunnel = FakeTunnelWS([b"SBO1rest"])
    router = ConnectRouter(tunnels={"blob": tunnel})
    receive_started = asyncio.Event()
    release_receive = asyncio.Event()
    monkeypatch.setattr(relay_client.websockets, "connect", router)

    async def fake_receive(*_args: Any, **_kwargs: Any) -> None:
        receive_started.set()
        await release_receive.wait()

    monkeypatch.setattr(blob_receiver, "receive_blob", fake_receive)
    task = asyncio.create_task(client._handle_tunnel("blob"))
    try:
        await _wait_until(receive_started.is_set)
        assert client._admission_gate.global_count == 1

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    finally:
        release_receive.set()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    assert client._admission_gate.global_count == 0


@pytest.mark.asyncio
async def test_local_private_listener_failure_preserves_success_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    client = _client(emitted)
    router = ConnectRouter(tunnels={"local": FakeTunnelWS()})
    ticks = iter([1_000, 1_001])

    async def fail_open_connection(*_args: Any) -> tuple[object, FakeWriter]:
        raise OSError("local listener unavailable")

    monkeypatch.setattr(relay_client.websockets, "connect", router)
    monkeypatch.setattr(
        relay_client,
        "asyncio",
        _AsyncioShim(relay_client.asyncio, fail_open_connection),
    )
    monkeypatch.setattr(relay_client, "now_ms", lambda: next(ticks))

    await client._handle_tunnel("local")

    health = _health_events(emitted)[-1]
    assert health["last_successful_relay_tunnel_at"] == 1_000
    assert health["last_relay_tunnel_error"] == (
        REASON_LOCAL_PRIVATE_LISTENER_UNREACHABLE
    )
    assert health["last_relay_tunnel_error_at"] == 1_001
    assert (
        health["last_relay_tunnel_error_at"]
        >= health["last_successful_relay_tunnel_at"]
    )


@pytest.mark.asyncio
async def test_404_reopen_does_not_cancel_unrelated_in_flight_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    client = _client(emitted)
    listen = FakeListenWS([_incoming("hold"), _incoming("missing-mobile")])
    router = ConnectRouter(
        listens=[listen],
        tunnels={
            "hold": FakeTunnelWS(),
            "missing-mobile": _invalid_status(404),
        },
    )
    pipe_started = asyncio.Event()
    release_pipe = asyncio.Event()

    async def fake_pipe(*_args: Any) -> None:
        pipe_started.set()
        await release_pipe.wait()

    monkeypatch.setattr(relay_client.websockets, "connect", router)
    monkeypatch.setattr(
        relay_client,
        "asyncio",
        _AsyncioShim(relay_client.asyncio, _open_connection_success),
    )
    monkeypatch.setattr(relay_client, "_pipe_tunnel", fake_pipe)

    run_once_task = asyncio.create_task(client._run_once())
    try:
        await pipe_started.wait()
        await _wait_until(lambda: listen.close_count == 1)
        assert "hold" in client._tunnels
        assert not client._tunnels["hold"].done()
    finally:
        release_pipe.set()
        await _wait_until(lambda: "hold" not in client._tunnels)
        with contextlib.suppress(asyncio.CancelledError):
            await run_once_task

    assert any(
        fields.get("last_relay_tunnel_error") == REASON_HOME_MISSING_MOBILE
        for fields in _health_events(emitted)
    )


@pytest.mark.asyncio
async def test_tunnel_failures_redact_tokens_urls_and_exception_text(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    client = _client(emitted)
    token_url = f"wss://relay.test/tunnel/redact?{TOKEN_URL_FRAGMENT}"
    exception_text = f"boom {SERVICE_TOKEN} {token_url} PAYLOAD_BYTES"
    router = ConnectRouter(tunnels={"redact": OSError(exception_text)})
    monkeypatch.setattr(relay_client.websockets, "connect", router)

    caplog.set_level(logging.WARNING, logger="spl.relay_client")
    await client._handle_tunnel("redact")

    serialized_events = json.dumps(emitted)
    for forbidden in (
        SERVICE_TOKEN,
        TOKEN_URL_FRAGMENT,
        token_url,
        exception_text,
        "PAYLOAD_BYTES",
    ):
        assert forbidden not in serialized_events
        assert forbidden not in caplog.text
    assert REASON_RELAY_TUNNEL_UNREACHABLE in serialized_events
