# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from typing import Any

import pytest

from solstone.think.link import browser_pairing
from solstone.think.spl import relay_client
from tests.spl.test_relay_client_blob_dispatch import (
    FakeTcpReader,
    FakeTcpWriter,
    FakeWs,
)


@pytest.mark.asyncio
async def test_pairing_tunnel_tls_writes_peeked_bytes_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeWs([b"\x16\x03\x01pairing"])
    writer = FakeTcpWriter()
    monkeypatch.setattr(
        relay_client.asyncio,
        "open_connection",
        lambda *_a, **_k: _open_connection(writer),
    )

    await relay_client._bridge_pairing_tunnel(
        "wss://relay.test",
        "tls",
        service_token="tok",
        rk_hex="00" * 16,
        opener=lambda *_a, **_k: ws,
    )

    assert writer.writes[0] == b"\x16\x03\x01pairing"
    assert writer.closed is True


@pytest.mark.asyncio
async def test_pairing_tunnel_sbp_branch_does_not_open_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeWs([b"SBP1\x01rest"])
    called = False

    async def fail_open_connection(*_args: Any) -> tuple[FakeTcpReader, FakeTcpWriter]:
        raise AssertionError("SBP1 branch must not open loopback")

    async def fake_register(reader, got_ws, *, register_post=None) -> None:
        nonlocal called
        assert got_ws is ws
        assert await reader.read_exactly(5) == b"SBP1\x01"
        called = True

    monkeypatch.setattr(relay_client.asyncio, "open_connection", fail_open_connection)
    monkeypatch.setattr(browser_pairing, "register_browser", fake_register)

    await relay_client._bridge_pairing_tunnel(
        "wss://relay.test",
        "pair",
        service_token="tok",
        rk_hex="00" * 16,
        opener=lambda *_a, **_k: ws,
    )

    assert called is True


async def _open_connection(
    writer: FakeTcpWriter,
) -> tuple[FakeTcpReader, FakeTcpWriter]:
    return FakeTcpReader(), writer
