# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import asyncio
import contextlib
import json
import logging
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from solstone.convey.secure_listener import runtime as rt
from solstone.convey.secure_listener.accept import (
    CERTLESS_TUNNEL_CAP,
    SecureListener,
    certless_admission_mode,
)
from solstone.think.link.ca import load_or_generate_ca
from solstone.think.link.nonces import NONCE_TTL_SECONDS, NonceStore
from solstone.think.link.paths import ca_dir, nonces_path, state_path
from tests.link.certless_helpers import write_config


def test_reuse_port_allows_coexisting_bind():
    executor = ThreadPoolExecutor(max_workers=1)
    listener = SecureListener(
        app=MagicMock(),
        strict_tls_ctx=MagicMock(),
        relaxed_tls_ctx=MagicMock(),
        authorized=set(),
        executor=executor,
        callosum_emit=lambda *a, **kw: None,
        host="127.0.0.1",
        port=0,
    )
    loop = asyncio.new_event_loop()
    s2 = None
    try:
        loop.run_until_complete(listener.start())
        assert listener.sockets
        port = listener.sockets[0].getsockname()[1]
        assert port != 0

        s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s2.bind(("127.0.0.1", port))
    finally:
        if listener.sockets:
            loop.run_until_complete(listener.stop())
        if s2 is not None:
            s2.close()
        loop.close()
        executor.shutdown(wait=True, cancel_futures=True)


def test_stop_all_after_loop_closed_does_not_raise():
    from solstone.convey.secure_listener import runtime as rt

    previous_runtime = rt._runtime
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)

        loop = asyncio.new_event_loop()
        loop.close()
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
        app = SimpleNamespace(secure_listener_started=True)
        listener = SimpleNamespace(sockets=(s,))
        state = rt.RuntimeState(
            loop=loop,
            thread=thread,
            apps=[app],
            executor=executor,
            listener=listener,
            sockets=(s,),
        )
        rt._runtime = state

        rt.stop_all_secure_listener()

        assert s.fileno() == -1
    finally:
        rt._runtime = previous_runtime
        s.close()
        executor.shutdown(wait=True, cancel_futures=True)


def test_start_secure_listener_setup_incomplete_does_not_establish_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_runtime = rt._runtime
    try:
        rt._runtime = None
        _runtime_journal(tmp_path, monkeypatch, {"setup": {}})
        app = _enabled_listener_app()

        rt.start_secure_listener(app)

        assert rt._runtime is None
        assert not getattr(app, "secure_listener_started", False)
        ca_path = ca_dir()
        assert not (ca_path / "cert.pem").exists()
        assert not (ca_path / "private.pem").exists()
        state_file = state_path()
        if state_file.exists():
            state = json.loads(state_file.read_text("utf-8"))
            assert not state.get("instance_id")
    finally:
        rt._runtime = previous_runtime


def test_start_secure_listener_setup_incomplete_no_thread_no_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    previous_runtime = rt._runtime
    try:
        rt._runtime = None
        _runtime_journal(tmp_path, monkeypatch, {"setup": {}})
        app = _enabled_listener_app()

        with caplog.at_level(
            logging.WARNING,
            logger="convey.secure_listener.runtime",
        ):
            rt.start_secure_listener(app)

        assert rt._runtime is None
        assert not getattr(app, "secure_listener_started", False)
        assert not any(
            record.name == "convey.secure_listener.runtime" for record in caplog.records
        )
    finally:
        rt._runtime = previous_runtime


def test_start_secure_listener_setup_complete_starts_in_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_runtime = rt._runtime
    try:
        rt._runtime = None
        _runtime_journal(
            tmp_path,
            monkeypatch,
            {"setup": {"completed_at": 1700000000000}},
        )
        load_or_generate_ca(ca_dir())
        app = _enabled_listener_app()

        rt.start_secure_listener(app)

        runtime = rt._runtime
        assert runtime is not None
        assert runtime.started_event.is_set()
        assert runtime.start_error is None
        assert runtime.sockets
        assert app.secure_listener_started is True
    finally:
        rt.stop_all_secure_listener()
        rt._runtime = previous_runtime


def test_start_secure_listener_committed_but_setup_incomplete_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    previous_runtime = rt._runtime
    try:
        rt._runtime = None
        _runtime_journal(tmp_path, monkeypatch, {"setup": {}})
        load_or_generate_ca(ca_dir())
        app = _enabled_listener_app()

        with caplog.at_level(
            logging.WARNING,
            logger="convey.secure_listener.runtime",
        ):
            rt.start_secure_listener(app)

        assert any(
            "identity is committed but setup" in record.getMessage()
            for record in caplog.records
        )
        assert rt._runtime is None
        assert not getattr(app, "secure_listener_started", False)
    finally:
        rt._runtime = previous_runtime


def test_start_secure_listener_setup_complete_idempotent_identity_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_runtime = rt._runtime
    try:
        rt._runtime = None
        _runtime_journal(
            tmp_path,
            monkeypatch,
            {"setup": {"completed_at": 1700000000000}},
        )
        load_or_generate_ca(ca_dir())
        ca_path = ca_dir()
        cert_before = (ca_path / "cert.pem").read_bytes()
        key_before = (ca_path / "private.pem").read_bytes()
        state_file = state_path()
        state_before = state_file.read_bytes() if state_file.exists() else None
        app = _enabled_listener_app()

        rt.start_secure_listener(app)
        rt.start_secure_listener(app)

        runtime = rt._runtime
        assert runtime is not None
        assert runtime.started_event.is_set()
        assert runtime.start_error is None
        assert runtime.sockets
        assert runtime.apps.count(app) == 1
        assert (ca_path / "cert.pem").read_bytes() == cert_before
        assert (ca_path / "private.pem").read_bytes() == key_before
        if state_before is not None:
            assert state_file.read_bytes() == state_before
    finally:
        rt.stop_all_secure_listener()
        rt._runtime = previous_runtime


@pytest.mark.parametrize(
    ("mode", "window_is_open", "expected"),
    [
        ("pl-direct", True, "pl-direct"),
        ("pl-via-spl", True, "pl-via-spl"),
        ("pl-direct", False, None),
        ("pl-via-spl", False, None),
    ],
)
def test_certless_admission_mode(
    mode: str,
    window_is_open: bool,
    expected: str | None,
) -> None:
    assert certless_admission_mode(mode, window_is_open) == expected


@pytest.mark.asyncio
async def test_certless_reap_tears_down_on_passive_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _journal(tmp_path, monkeypatch, link={"posture": "spl"})
    NonceStore(nonces_path()).add("live", "phone", now=1000)
    listener = _listener()
    handle, writer, mux, task = _register_fake_certless(listener)

    await listener._reap_certless_if_window_closed(now=1000 + NONCE_TTL_SECONDS + 1)
    await asyncio.sleep(0)

    assert handle.connection_id not in listener._certless_connections
    assert writer.closed is True
    assert mux.closed is True
    assert task.cancelled()
    assert journal.exists()


@pytest.mark.asyncio
async def test_certless_reap_tears_down_after_nonce_consume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _journal(tmp_path, monkeypatch, link={"posture": "spl"})
    store = NonceStore(nonces_path())
    store.add("live", "phone", now=1000)
    store.consume("live", now=1001)
    listener = _listener()
    handle, writer, mux, _task = _register_fake_certless(listener)

    await listener._reap_certless_if_window_closed(now=1002)

    assert handle.connection_id not in listener._certless_connections
    assert writer.closed is True
    assert mux.closed is True


@pytest.mark.asyncio
async def test_certless_not_reaped_while_nonce_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _journal(tmp_path, monkeypatch, link={"posture": "direct"})
    NonceStore(nonces_path()).add("live", "phone", now=1000)
    listener = _listener()
    handle, writer, mux, _task = _register_fake_certless(listener)

    try:
        await listener._reap_certless_if_window_closed(now=1001)

        assert handle.connection_id in listener._certless_connections
        assert writer.closed is False
        assert mux.closed is False
    finally:
        await listener._close_certless_connection(handle)


@pytest.mark.asyncio
async def test_certless_concurrent_cap_refuses_fifth() -> None:
    listener = _listener()
    registered_tasks: list[asyncio.Task[None]] = []
    rejected_task = asyncio.create_task(_sleep_forever())
    try:
        for index in range(CERTLESS_TUNNEL_CAP):
            handle, _writer, _mux, task = _register_fake_certless(
                listener,
                connection_id=f"conn-{index}",
            )
            registered_tasks.append(task)
            assert handle is not None

        rejected = listener._register_certless_connection(
            "conn-rejected",
            _FakeWriter(),
            rejected_task,
            _FakeMux(),
        )

        assert rejected is None
        assert len(listener._certless_connections) == CERTLESS_TUNNEL_CAP
    finally:
        for handle in list(listener._certless_connections.values()):
            await listener._close_certless_connection(handle)
        rejected_task.cancel()
        for task in registered_tasks + [rejected_task]:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _listener() -> SecureListener:
    return SecureListener(
        app=MagicMock(),
        strict_tls_ctx=MagicMock(),
        relaxed_tls_ctx=MagicMock(),
        authorized=MagicMock(),
        executor=MagicMock(),
        callosum_emit=lambda *a, **kw: None,
        host="127.0.0.1",
        port=0,
    )


def _enabled_listener_app() -> SimpleNamespace:
    return SimpleNamespace(config={"SECURE_LISTENER_ENABLED": True})


def _runtime_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, object],
) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    config_dir = journal / "config"
    config_dir.mkdir()
    (config_dir / "journal.json").write_text(
        json.dumps(config, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return journal


def _journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    link: dict[str, object],
) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    write_config(journal, link=link)
    return journal


def _register_fake_certless(
    listener: SecureListener,
    *,
    connection_id: str = "conn",
) -> tuple[object, "_FakeWriter", "_FakeMux", asyncio.Task[None]]:
    writer = _FakeWriter()
    mux = _FakeMux()
    task = asyncio.create_task(_sleep_forever())
    handle = listener._register_certless_connection(connection_id, writer, task, mux)
    assert handle is not None
    return handle, writer, mux, task


async def _sleep_forever() -> None:
    await asyncio.Event().wait()


class _FakeWriter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeMux:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True
