# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import importlib
import os
import socket
import subprocess
import threading


def test_warm_requests_declared_modules_and_discovers_once(monkeypatch):
    from solstone.think import warm

    discovered = []

    class _FakeRegistry:
        def discover(self):
            discovered.append(True)

    monkeypatch.setattr("solstone.apps.AppRegistry", _FakeRegistry)
    requested = []

    def fake_import(name):
        requested.append(name)
        return object()

    monkeypatch.setattr(importlib, "import_module", fake_import)

    assert warm.main([]) == 0
    assert requested == warm.warm_module_names()
    assert discovered == [True]


def test_warm_names_keep_onnxruntime_without_removed_stt_import(monkeypatch):
    from solstone.think import warm

    monkeypatch.setattr(warm.sys, "platform", "linux")
    monkeypatch.setattr(warm.platform, "machine", lambda: "x86_64")

    names = warm.warm_module_names()

    assert "onnxruntime" in names
    assert "sklearn" not in names
    assert "onnx" + "_asr" not in names


def test_warm_genuine_import_failure_names_module(monkeypatch, caplog):
    from solstone.think import warm

    monkeypatch.setattr(
        warm,
        "warm_module_names",
        lambda: ["numpy", "totally_bogus_native_xyz"],
    )

    with caplog.at_level("ERROR"):
        assert warm.main([]) != 0

    assert "totally_bogus_native_xyz" in caplog.text


def test_warm_has_no_side_effects(tmp_path, monkeypatch):
    from solstone.think import warm

    class _FakeRegistry:
        def discover(self):
            return None

    monkeypatch.setattr("solstone.apps.AppRegistry", _FakeRegistry)
    monkeypatch.setattr(importlib, "import_module", lambda _name: object())

    def snapshot() -> set[str]:
        return {path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")}

    real_socket = socket.socket
    socket_attempts = {"bind": 0, "connect": 0}

    class GuardedSocket:
        def __init__(self, *args, **kwargs):
            self._socket = real_socket(*args, **kwargs)

        def bind(self, *args, **kwargs):
            socket_attempts["bind"] += 1
            raise AssertionError("warm must not bind sockets")

        def connect(self, *args, **kwargs):
            socket_attempts["connect"] += 1
            raise AssertionError("warm must not connect sockets")

        def __enter__(self):
            self._socket.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self._socket.__exit__(exc_type, exc_value, traceback)

        def __getattr__(self, name):
            return getattr(self._socket, name)

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    before_files = snapshot()
    before_threads = threading.active_count()
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("warm must not spawn child processes")
        ),
    )
    monkeypatch.setattr(
        os,
        "fork",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("warm must not fork child processes")
        ),
        raising=False,
    )
    monkeypatch.setattr(socket, "socket", GuardedSocket)

    assert warm.main([]) == 0

    assert threading.active_count() == before_threads
    assert snapshot() == before_files
    assert socket_attempts == {"bind": 0, "connect": 0}
