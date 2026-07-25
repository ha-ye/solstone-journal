# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from solstone.think.link import client as link_client
from solstone.think.link import join_cli
from solstone.think.link.window import window_open
from tests.link.secure_listener_harness import SecureListenerHarness


@dataclass
class _DrainGate:
    entered: asyncio.Event
    release: asyncio.Event


@dataclass
class _PendingDrain:
    data: bytearray = field(default_factory=bytearray)
    flushed: asyncio.Event | None = None


def _pair_body(label: str) -> dict[str, str]:
    _private_key_pem, csr_pem = join_cli._build_csr(label)
    return {"csr": csr_pem, "device_label": label}


def _install_listener_drain_gate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    listener_loop: asyncio.AbstractEventLoop,
    gate_when: Callable[[], bool],
) -> _DrainGate:
    entered = asyncio.Event()
    release = asyncio.Event()
    real_write = asyncio.StreamWriter.write
    real_drain = asyncio.StreamWriter.drain
    pending: dict[asyncio.StreamWriter, _PendingDrain] = {}
    armed = True

    def _in_listener_loop() -> bool:
        try:
            return asyncio.get_running_loop() is listener_loop
        except RuntimeError:
            return False

    def gated_write(self: asyncio.StreamWriter, data: bytes) -> None:
        if _in_listener_loop() and (self in pending or (armed and gate_when())):
            pending.setdefault(self, _PendingDrain()).data.extend(data)
            return
        real_write(self, data)

    async def gated_drain(self: asyncio.StreamWriter) -> None:
        nonlocal armed
        buffered = pending.get(self)
        if buffered is None:
            await real_drain(self)
            return
        if buffered.flushed is not None:
            await buffered.flushed.wait()
            return
        buffered.flushed = asyncio.Event()
        armed = False
        entered.set()
        try:
            await release.wait()
            data = bytes(buffered.data)
            pending.pop(self, None)
            if data:
                real_write(self, data)
            await real_drain(self)
        finally:
            buffered.flushed.set()

    monkeypatch.setattr(asyncio.StreamWriter, "write", gated_write)
    monkeypatch.setattr(asyncio.StreamWriter, "drain", gated_drain)
    return _DrainGate(entered=entered, release=release)


async def _post_pair_with_payload_capture(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    body: dict[str, str],
) -> tuple[join_cli.PairResponse, dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    real_parse = join_cli._parse_pair_response

    def capture(payload: Any) -> join_cli.PairResponse:
        if isinstance(payload, dict):
            payloads.append(payload)
        return real_parse(payload)

    monkeypatch.setattr(join_cli, "_parse_pair_response", capture)
    response = await asyncio.to_thread(join_cli._post_pair_framed, url, body)
    assert payloads, "pair response payload was not parsed"
    return response, payloads[-1]


def _assert_complete_pair_response(
    harness: SecureListenerHarness,
    response: join_cli.PairResponse,
    payload: dict[str, Any],
) -> None:
    assert response.client_cert
    assert response.ca_chain
    assert response.instance_id
    fingerprint = payload.get("fingerprint")
    assert isinstance(fingerprint, str)
    assert fingerprint.startswith("sha256:")
    assert harness.authorized.get(fingerprint) is not None


def _only_certless_handle(harness: SecureListenerHarness) -> Any:
    handles = list(harness.listener._certless_connections.values())
    assert len(handles) == 1
    return handles[0]


async def _acquire_and_release(lock: asyncio.Lock) -> None:
    async with lock:
        pass


def _run_two_request_pairing_client(
    host: str,
    port: int,
    first_path: str,
    first_body: dict[str, str],
    second_path: str,
    second_body: dict[str, str],
    send_second: threading.Event,
    second_started: threading.Event,
) -> tuple[
    tuple[int, dict[str, str], bytes],
    tuple[int, dict[str, str], bytes],
]:
    async def run() -> tuple[
        tuple[int, dict[str, str], bytes],
        tuple[int, dict[str, str], bytes],
    ]:
        reader, writer = await asyncio.open_connection(host, port)
        session = await link_client._open_pairing_session(
            link_client._TcpEncryptedTransport(reader, writer),
        )
        try:
            first = asyncio.create_task(
                session.request(
                    "POST",
                    first_path,
                    headers={"content-type": "application/json"},
                    body=json.dumps(first_body).encode("utf-8"),
                )
            )
            await asyncio.to_thread(send_second.wait)
            second = asyncio.create_task(
                session.request(
                    "POST",
                    second_path,
                    headers={"content-type": "application/json"},
                    body=json.dumps(second_body).encode("utf-8"),
                )
            )
            second_started.set()
            second_result = await second
            first_result = await first
            return first_result, second_result
        finally:
            await session.close()

    return asyncio.run(run())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "nonce"),
    [
        ("/app/network/pair", "10000000000000000000000000000021"),
        ("/app/link/pair", "10000000000000000000000000000022"),
    ],
)
async def test_real_secure_listener_certless_pair_survives_forced_reap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    nonce: str,
) -> None:
    label = "e2e-phone"
    harness = await SecureListenerHarness.start(tmp_path, monkeypatch)
    gate = _install_listener_drain_gate(
        monkeypatch,
        listener_loop=asyncio.get_running_loop(),
        gate_when=lambda: not window_open(),
    )
    try:
        harness.seed_nonce(nonce, label)
        client_task = asyncio.create_task(
            _post_pair_with_payload_capture(
                monkeypatch,
                harness.pair_url(nonce, path=path),
                _pair_body(label),
            )
        )

        await asyncio.wait_for(gate.entered.wait(), timeout=5.0)
        await harness.listener._reap_certless_if_window_closed()
        gate.release.set()
        response, payload = await asyncio.wait_for(client_task, timeout=5.0)

        _assert_complete_pair_response(harness, response, payload)
    finally:
        gate.release.set()
        await harness.close()


@pytest.mark.asyncio
async def test_reaper_skips_inflight_pair_after_pair_lock_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = "lock-phone"
    nonce = "10000000000000000000000000000024"
    harness = await SecureListenerHarness.start(tmp_path, monkeypatch)
    gate = _install_listener_drain_gate(
        monkeypatch,
        listener_loop=asyncio.get_running_loop(),
        gate_when=lambda: not window_open(),
    )
    try:
        harness.seed_nonce(nonce, label)
        client_task = asyncio.create_task(
            _post_pair_with_payload_capture(
                monkeypatch,
                harness.pair_url(nonce),
                _pair_body(label),
            )
        )

        await asyncio.wait_for(gate.entered.wait(), timeout=5.0)
        handle = _only_certless_handle(harness)
        await asyncio.wait_for(_acquire_and_release(handle.pair_lock), timeout=5.0)
        assert handle.pair_in_flight.active is True

        await harness.listener._reap_certless_if_window_closed()

        assert handle.connection_id in harness.listener._certless_connections
        gate.release.set()
        response, payload = await asyncio.wait_for(client_task, timeout=5.0)
        _assert_complete_pair_response(harness, response, payload)
    finally:
        gate.release.set()
        await harness.close()


@pytest.mark.asyncio
async def test_inflight_certless_pair_does_not_reopen_window_for_second_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = "second-phone"
    nonce = "10000000000000000000000000000025"
    harness = await SecureListenerHarness.start(tmp_path, monkeypatch)
    gate = _install_listener_drain_gate(
        monkeypatch,
        listener_loop=asyncio.get_running_loop(),
        gate_when=lambda: not window_open(),
    )
    send_second = threading.Event()
    second_started = threading.Event()
    try:
        harness.seed_nonce(nonce, label)
        client_task = asyncio.create_task(
            asyncio.to_thread(
                _run_two_request_pairing_client,
                harness.host,
                harness.port,
                f"/app/network/pair?token={nonce}",
                _pair_body(label),
                "/app/network/pair?token=used-window",
                _pair_body("late-phone"),
                send_second,
                second_started,
            )
        )

        await asyncio.wait_for(gate.entered.wait(), timeout=5.0)
        await harness.listener._reap_certless_if_window_closed()
        send_second.set()
        assert await asyncio.wait_for(
            asyncio.to_thread(second_started.wait, 5.0),
            timeout=5.0,
        )
        gate.release.set()
        first, second = await asyncio.wait_for(client_task, timeout=5.0)

        assert first[0] == 200
        assert second[0] == 403
        assert b"pairing window closed" in second[2]
    finally:
        gate.release.set()
        send_second.set()
        with contextlib.suppress(Exception):
            await client_task
        await harness.close()


@pytest.mark.asyncio
async def test_stop_returns_promptly_with_pair_response_in_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = "stop-phone"
    nonce = "10000000000000000000000000000026"
    harness = await SecureListenerHarness.start(tmp_path, monkeypatch)
    gate = _install_listener_drain_gate(
        monkeypatch,
        listener_loop=asyncio.get_running_loop(),
        gate_when=lambda: not window_open(),
    )
    client_task: asyncio.Task[tuple[join_cli.PairResponse, dict[str, Any]]] | None = (
        None
    )
    try:
        harness.seed_nonce(nonce, label)
        client_task = asyncio.create_task(
            _post_pair_with_payload_capture(
                monkeypatch,
                harness.pair_url(nonce),
                _pair_body(label),
            )
        )

        await asyncio.wait_for(gate.entered.wait(), timeout=5.0)
        handle = _only_certless_handle(harness)
        assert handle.pair_in_flight.active is True

        await asyncio.wait_for(harness.listener.stop(), timeout=5.0)

        assert handle.connection_id not in harness.listener._certless_connections
    finally:
        gate.release.set()
        if client_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client_task, timeout=5.0)
        await harness.close()
