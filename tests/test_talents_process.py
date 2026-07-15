# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import io
import signal

import pytest

from solstone.think import talents


def test_talent_main_cancellation_exits_without_cancelled_traceback(
    monkeypatch, capsys
):
    def fake_run(coro):
        coro.close()
        raise asyncio.CancelledError

    monkeypatch.setattr(talents.asyncio, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        talents.main()

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "CancelledError" not in captured.err


def test_talent_main_async_registers_sigterm_cancel_handler(monkeypatch):
    monkeypatch.setattr(talents.sys, "argv", ["talents"])
    monkeypatch.setattr(talents, "require_solstone", lambda: None)
    monkeypatch.setattr(talents.sys, "stdin", io.StringIO(""))
    recorded = []

    async def runner():
        loop = asyncio.get_running_loop()

        def record_signal_handler(sig, cb, *_args):
            recorded.append((sig, cb))

        def ignore_signal_handler(_sig):
            return None

        monkeypatch.setattr(loop, "add_signal_handler", record_signal_handler)
        monkeypatch.setattr(loop, "remove_signal_handler", ignore_signal_handler)
        await talents.main_async()

    asyncio.run(runner())

    sigterm_cbs = [cb for sig, cb in recorded if sig == signal.SIGTERM]
    assert sigterm_cbs
    assert all(getattr(cb, "__name__", "") == "cancel" for cb in sigterm_cbs)
