# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from solstone.think.spl import blob_receiver, relay_client
from tests.helpers.module_mocks import module_mock


class FakeWs:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames = list(frames)
        self.sent: list[bytes] = []
        self.closed = False

    async def __aenter__(self) -> "FakeWs":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def recv(self) -> bytes:
        if not self.frames:
            raise relay_client.ConnectionClosed(None, None)
        return self.frames.pop(0)

    def __aiter__(self) -> "FakeWs":
        return self

    async def __anext__(self) -> bytes:
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class FakeTcpReader:
    async def read(self, _n: int) -> bytes:
        return b""


class FakeTcpWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _client() -> relay_client.RelayClient:
    return relay_client.RelayClient(
        instance_id="instance.test",
        relay_endpoint="https://relay.test",
        service_token="tok",
    )


@pytest.mark.asyncio
async def test_handle_tunnel_tls_writes_peeked_bytes_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeWs([b"\x16\x03\x01abcdef"])
    writer = FakeTcpWriter()
    monkeypatch.setattr(relay_client.websockets, "connect", Mock(return_value=ws))
    monkeypatch.setattr(
        relay_client,
        "asyncio",
        module_mock(
            relay_client.asyncio,
            open_connection=AsyncMock(return_value=(FakeTcpReader(), writer)),
        ),
    )

    await _client()._handle_tunnel("tls")

    assert writer.writes[0] == b"\x16\x03\x01abcdef"
    assert writer.closed is True


@pytest.mark.asyncio
async def test_handle_tunnel_blob_branch_does_not_open_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeWs([b"SBO1rest"])
    called = False
    monkeypatch.setattr(relay_client.websockets, "connect", Mock(return_value=ws))

    async def fail_open_connection(*_args: Any) -> tuple[FakeTcpReader, FakeTcpWriter]:
        raise AssertionError("blob branch must not open loopback")

    async def fake_receive(reader, got_ws, **_kwargs: Any) -> None:
        nonlocal called
        assert got_ws is ws
        assert await reader.read_exactly(4) == b"SBO1"
        called = True

    open_connection = AsyncMock(side_effect=fail_open_connection)
    monkeypatch.setattr(
        relay_client,
        "asyncio",
        module_mock(relay_client.asyncio, open_connection=open_connection),
    )
    monkeypatch.setattr(
        blob_receiver,
        "receive_blob",
        AsyncMock(side_effect=fake_receive),
    )

    await _client()._handle_tunnel("blob")

    assert called is True
    open_connection.assert_not_awaited()
