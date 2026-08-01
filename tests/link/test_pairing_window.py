# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from solstone.think.link import pair_window, window
from solstone.think.link.nonces import NONCE_TTL_SECONDS, NonceStore
from solstone.think.link.paths import nonces_path
from solstone.think.link.window import read_posture, window_open
from tests.link.certless_helpers import write_config


class _WindowWs:
    def __init__(self, frames: list[str]) -> None:
        self.frames = list(frames)
        self.wait_forever = asyncio.Event()

    async def __aenter__(self) -> "_WindowWs":
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False

    def __aiter__(self) -> "_WindowWs":
        return self

    async def __anext__(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        await self.wait_forever.wait()
        raise StopAsyncIteration


class _TunnelWs:
    def __init__(self, first_frame: bytes) -> None:
        self.first_frame = first_frame

    async def __aenter__(self) -> "_TunnelWs":
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False

    async def recv(self) -> bytes:
        return self.first_frame


class _RecordingOpener:
    def __init__(self, *connections: object) -> None:
        self.connections = list(connections)
        self.calls: list[tuple[str, dict[str, str], int | None]] = []

    def __call__(
        self,
        url: str,
        *,
        additional_headers: dict[str, str] | None = None,
        max_size: int | None = None,
    ) -> object:
        self.calls.append((url, dict(additional_headers or {}), max_size))
        return self.connections.pop(0)


class _Writer:
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


def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return journal


def test_read_posture_exact_match_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _journal(tmp_path, monkeypatch)

    write_config(journal)
    assert read_posture() == "direct"

    for link_cfg in (
        {},
        {"posture": 123},
        {"posture": "relay"},
        {"posture": "spl "},
    ):
        write_config(journal, link=link_cfg)
        assert read_posture() == "direct"

    write_config(journal, link={"posture": "spl"})
    assert read_posture() == "spl"


@pytest.mark.parametrize("posture", ["spl", "direct"])
def test_window_open_requires_live_unused_nonce_in_any_posture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    posture: str,
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    write_config(journal, link={"posture": posture})
    store = NonceStore(nonces_path())

    assert window_open(now=1000) is False

    store.add("live", "phone", now=1000)
    assert window_open(now=1000 + NONCE_TTL_SECONDS - 1) is True

    store.consume("live", now=1001)
    assert window_open(now=1002) is False

    store.add("expired", "phone", now=2000)
    assert window_open(now=2000 + NONCE_TTL_SECONDS) is False


def test_window_open_in_direct_posture_with_live_nonce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    write_config(journal, link={"posture": "direct"})
    NonceStore(nonces_path()).add("live", "phone", now=1000)

    assert window_open(now=1001) is True


@pytest.mark.parametrize("posture", ["spl", "direct"])
def test_window_open_fail_closed_on_corrupt_nonce_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    posture: str,
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    write_config(journal, link={"posture": posture})
    path = nonces_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert window_open(now=1000) is False


@pytest.mark.parametrize("posture", ["spl", "direct"])
def test_window_open_fail_closed_on_read_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    posture: str,
) -> None:
    journal = _journal(tmp_path, monkeypatch)
    write_config(journal, link={"posture": posture})

    class BrokenNonceStore:
        def __init__(self, _path: Path) -> None:
            pass

        def snapshot(self) -> list[object]:
            raise OSError("nope")

    monkeypatch.setattr(window, "NonceStore", BrokenNonceStore)

    assert window_open(now=1000) is False
    assert "cert-less pairing window read failed" in caplog.text


@pytest.mark.asyncio
async def test_pair_window_uses_rk_header_without_url_credentials() -> None:
    opener = _RecordingOpener(_WindowWs([]))

    await pair_window.hold_pair_window(
        relay_endpoint="https://link.solstone.app",
        service_token="tok",
        rk=bytes.fromhex("00112233445566778899aabbccddeeff"),
        timeout=0.01,
        opener=opener,
    )

    url, headers, max_size = opener.calls[0]
    assert url == "wss://link.solstone.app/session/pair-window"
    assert "?instance=" not in url
    assert "?token=" not in url
    assert headers == {
        "Authorization": "Bearer tok",
        "Sec-Pair-Key": "00112233445566778899aabbccddeeff",
    }
    assert max_size is None


@pytest.mark.asyncio
async def test_tls_pair_tunnel_bridges_to_local_secure_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = _Writer()
    observed: list[tuple[object, object, object, str]] = []

    async def open_connection(host: str, port: int) -> tuple[object, _Writer]:
        assert (host, port) == ("127.0.0.1", 7657)
        return object(), writer

    async def pipe_tunnel(
        ws: object,
        tcp_reader: object,
        tcp_writer: object,
        tunnel_id: str,
    ) -> None:
        observed.append((ws, tcp_reader, tcp_writer, tunnel_id))

    class _AsyncioShim:
        def __init__(self) -> None:
            self.open_connection = open_connection

        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

    monkeypatch.setattr(pair_window, "asyncio", _AsyncioShim())
    monkeypatch.setattr(pair_window, "_pipe_tunnel", pipe_tunnel)
    tunnel = _TunnelWs(b"\x16\x03\x01\x00")

    await pair_window._bridge_pairing_tunnel(
        "wss://link.solstone.app",
        "t1",
        service_token="tok",
        rk_hex="00" * 16,
        opener=_RecordingOpener(tunnel),
    )

    assert writer.writes == [b"\x16\x03\x01\x00"]
    assert writer.closed is True
    assert len(observed) == 1
    assert observed[0][0] is tunnel
    assert observed[0][2] is writer
    assert observed[0][3] == "t1"
