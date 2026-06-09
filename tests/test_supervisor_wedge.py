# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import asyncio
import logging
from collections import OrderedDict
from unittest.mock import Mock

import httpx
import pytest

import solstone.think.supervisor as mod
from solstone.think.providers import local_server
from solstone.think.providers.shared import classify_provider_error


@pytest.fixture(autouse=True)
def isolate_supervisor_wedge_state(monkeypatch):
    monkeypatch.setattr(
        mod,
        "_wedge_state",
        {
            "providers": OrderedDict(),
            "failures": set(),
            "cooldown_until": 0.0,
            "awaiting_recovery": False,
        },
    )
    monkeypatch.setattr(mod, "_recovery_state", {"local_server_down": False})
    monkeypatch.setattr(mod, "_managed_procs", [])
    monkeypatch.setattr(mod, "_SERVICE_STATE", {})
    monkeypatch.setattr(mod, "_RESTART_POLICIES", {})
    monkeypatch.setattr(mod, "_is_remote_mode", False)
    monkeypatch.setattr(mod, "shutdown_requested", False)
    monkeypatch.setattr(mod, "_supervisor_callosum", None)


class _ProcessStub:
    def __init__(self, returncode: int | None = None):
        self.returncode = returncode
        self.pid = 12345

    def poll(self):
        return self.returncode


class _ManagedStub:
    def __init__(self, name: str, cmd: list[str], returncode: int | None = None):
        self.name = name
        self.cmd = cmd
        self.process = _ProcessStub(returncode)
        self.ref = f"{name}-ref"
        self.cleanup = Mock()


def _start(use_id: str, provider: str = "local") -> dict:
    return {
        "tract": "cortex",
        "event": "start",
        "use_id": use_id,
        "provider": provider,
    }


def _error(use_id: str, reason_code: str | None = "provider_unavailable") -> dict:
    message = {
        "tract": "cortex",
        "event": "error",
        "use_id": use_id,
        "error": "generation failed",
    }
    if reason_code is not None:
        message["reason_code"] = reason_code
    return message


def _finish(use_id: str) -> dict:
    return {
        "tract": "cortex",
        "event": "finish",
        "use_id": use_id,
        "result": {"ok": True},
    }


def _drive_wedge(handler=mod._handle_cortex_outcome, prefix: str = "fail") -> None:
    for idx in range(mod.LOCAL_WEDGE_THRESHOLD):
        use_id = f"{prefix}-{idx}"
        handler(_start(use_id))
        handler(_error(use_id))


def _ready_local_server(monkeypatch, port: int = 9999) -> None:
    monkeypatch.setattr(mod, "read_service_port", Mock(return_value=port))
    monkeypatch.setattr(
        local_server,
        "_probe_health",
        Mock(return_value=(local_server.STATE_READY, None)),
    )


def test_remote_mode_ignores_cortex_events_without_state_or_io(monkeypatch):
    class FailingState(dict):
        def __getitem__(self, key):
            raise AssertionError("remote mode should not read wedge state")

    monkeypatch.setattr(mod, "_is_remote_mode", True)
    monkeypatch.setattr(mod, "_wedge_state", FailingState())
    monkeypatch.setattr(
        mod,
        "read_service_port",
        Mock(side_effect=AssertionError("should not read service port")),
    )
    monkeypatch.setattr(
        mod,
        "_restart_service",
        Mock(side_effect=AssertionError("should not restart service")),
    )

    mod._handle_cortex_outcome(_start("u1"))
    mod._handle_cortex_outcome(_error("u1"))
    mod._handle_cortex_outcome(_finish("u1"))


def test_unknown_use_id_terminal_does_not_count_or_reset():
    mod._handle_cortex_outcome(_start("known"))
    mod._handle_cortex_outcome(_error("known"))

    mod._handle_cortex_outcome(_finish("unknown"))
    mod._handle_cortex_outcome(_error("missing"))

    assert mod._wedge_state["failures"] == {"known"}


def test_local_finish_resets_failure_counter_by_use_id_attribution():
    mod._handle_cortex_outcome(_start("u1"))
    mod._handle_cortex_outcome(_error("u1"))
    mod._handle_cortex_outcome(_start("u2"))
    mod._handle_cortex_outcome(_error("u2"))
    mod._handle_cortex_outcome(_start("remote", provider="google"))
    mod._handle_cortex_outcome(_finish("remote"))

    assert mod._wedge_state["failures"] == {"u1", "u2"}

    mod._handle_cortex_outcome(_start("ok"))
    mod._handle_cortex_outcome(_finish("ok"))

    assert mod._wedge_state["failures"] == set()


def test_only_real_500_provider_unavailable_counts(monkeypatch):
    monkeypatch.setattr(
        mod,
        "read_service_port",
        Mock(side_effect=AssertionError("should not probe below threshold")),
    )
    req = httpx.Request("POST", "http://localhost:8080/v1/chat/completions")
    err500 = httpx.HTTPStatusError(
        "server error",
        request=req,
        response=httpx.Response(500, request=req),
    )
    err400 = httpx.HTTPStatusError(
        "bad request",
        request=req,
        response=httpx.Response(400, request=req),
    )
    errto = httpx.TimeoutException("timed out")

    reason500 = classify_provider_error(err500, "local")
    reason400 = classify_provider_error(err400, "local")
    reasonto = classify_provider_error(errto, "local")
    assert reason500 == "provider_unavailable"
    assert reason400 == "unknown"
    assert reasonto == "chat_timeout"

    for use_id, reason in (
        ("u500", reason500),
        ("u400", reason400),
        ("uto", reasonto),
        ("umissing", None),
    ):
        mod._handle_cortex_outcome(_start(use_id))
        mod._handle_cortex_outcome(_error(use_id, reason))

    assert mod._wedge_state["failures"] == {"u500"}


@pytest.mark.parametrize(
    ("platform", "proctitle"),
    [
        ("darwin", mod.MLX_SERVER_PROCESS_NAME),
        ("linux", mod.LOCAL_SERVER_PROCESS_NAME),
    ],
)
def test_recycles_through_existing_restart_machinery_by_platform(
    monkeypatch, platform, proctitle
):
    monkeypatch.setattr(mod.sys, "platform", platform)
    _ready_local_server(monkeypatch)
    managed = _ManagedStub(proctitle, ["/tmp/server"])
    mod._managed_procs.append(managed)
    monkeypatch.setattr(mod, "_start_termination_thread", Mock())

    _drive_wedge()

    assert mod._SERVICE_STATE[proctitle]["restart"] is True

    managed.process.returncode = 1
    launches = []
    replacement = _ManagedStub(proctitle, managed.cmd)

    def fake_launch(name, cmd, *, restart=False, shutdown_timeout=15, ref=None):
        launches.append(
            {
                "name": name,
                "cmd": cmd,
                "restart": restart,
                "shutdown_timeout": shutdown_timeout,
                "ref": ref,
            }
        )
        return replacement

    monkeypatch.setattr(mod, "_launch_process", fake_launch)

    asyncio.run(mod.handle_runner_exits([managed]))

    assert launches == [
        {
            "name": proctitle,
            "cmd": managed.cmd,
            "restart": True,
            "shutdown_timeout": 15,
            "ref": None,
        }
    ]
    assert mod._recovery_state["local_server_down"] is True


def test_cooldown_ignores_terminal_events_and_prevents_rerecycle(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    _ready_local_server(monkeypatch)
    restart_service = Mock(return_value=True)
    monkeypatch.setattr(mod, "_restart_service", restart_service)

    _drive_wedge()

    assert restart_service.call_count == 1
    assert mod._wedge_state["cooldown_until"] == 1120.0
    assert mod._wedge_state["awaiting_recovery"] is True

    now[0] = 1050.0
    for idx in range(mod.LOCAL_WEDGE_THRESHOLD):
        use_id = f"cooldown-{idx}"
        mod._handle_cortex_outcome(_start(use_id))
        mod._handle_cortex_outcome(_error(use_id))
    mod._handle_cortex_outcome(_start("cooldown-finish"))
    mod._handle_cortex_outcome(_finish("cooldown-finish"))

    assert restart_service.call_count == 1
    assert mod._wedge_state["failures"] == set()
    assert mod._wedge_state["awaiting_recovery"] is True


def test_wedge_logs_declared_recycling_and_recovered(monkeypatch, caplog):
    now = [2000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    _ready_local_server(monkeypatch)
    monkeypatch.setattr(mod, "_restart_service", Mock(return_value=True))
    caplog.set_level(logging.INFO)

    _drive_wedge()
    now[0] = mod._wedge_state["cooldown_until"] + 1.0
    mod._handle_cortex_outcome(_start("recovered"))
    mod._handle_cortex_outcome(_finish("recovered"))

    assert "local server wedge: declared" in caplog.text
    assert "local server wedge: recycling" in caplog.text
    assert "local server wedge: recovered after recycle" in caplog.text


def test_dispatch_routes_cortex_events_and_duplicate_errors_are_idempotent(
    monkeypatch, mock_callosum
):
    _ready_local_server(monkeypatch)
    restart_service = Mock(return_value=True)
    monkeypatch.setattr(mod, "_restart_service", restart_service)

    mod._handle_callosum_message(_start("dupe"))
    mod._handle_callosum_message(_error("dupe"))
    mod._handle_callosum_message(_error("dupe"))
    mod._handle_callosum_message(_start("u2"))
    mod._handle_callosum_message(_error("u2"))
    mod._handle_callosum_message(_start("u3"))
    mod._handle_callosum_message(_error("u3"))

    restart_service.assert_called_once_with(mod.LOCAL_SERVER_PROCESS_NAME)


def test_provider_map_cap_evicts_oldest_and_evicted_terminal_is_ignored(monkeypatch):
    monkeypatch.setattr(mod, "LOCAL_WEDGE_PROVIDER_MAP_CAP", 2)

    mod._handle_cortex_outcome(_start("u1"))
    mod._handle_cortex_outcome(_start("u2"))
    mod._handle_cortex_outcome(_start("u3"))
    mod._handle_cortex_outcome(_error("u1"))

    assert list(mod._wedge_state["providers"].keys()) == ["u2", "u3"]
    assert mod._wedge_state["failures"] == set()


@pytest.mark.parametrize(
    ("port", "health_state", "expected_log"),
    [
        (None, local_server.STATE_READY, "local service port unavailable"),
        (9999, local_server.STATE_LOADING, "health state=loading"),
    ],
)
def test_probe_deferral_clears_failures_without_cooldown(
    monkeypatch, caplog, port, health_state, expected_log
):
    monkeypatch.setattr(mod, "read_service_port", Mock(return_value=port))
    probe_health = Mock(return_value=(health_state, None))
    monkeypatch.setattr(local_server, "_probe_health", probe_health)
    restart_service = Mock()
    monkeypatch.setattr(mod, "_restart_service", restart_service)
    caplog.set_level(logging.WARNING)

    _drive_wedge()

    assert expected_log in caplog.text
    assert mod._wedge_state["failures"] == set()
    assert mod._wedge_state["cooldown_until"] == 0.0
    assert mod._wedge_state["awaiting_recovery"] is False
    restart_service.assert_not_called()


def test_restart_service_false_defers_without_cooldown(monkeypatch, caplog):
    _ready_local_server(monkeypatch)
    restart_service = Mock(return_value=False)
    monkeypatch.setattr(mod, "_restart_service", restart_service)
    caplog.set_level(logging.WARNING)

    _drive_wedge()

    assert "local server wedge: recycle deferred; service not running" in caplog.text
    assert mod._wedge_state["failures"] == set()
    assert mod._wedge_state["cooldown_until"] == 0.0
    assert mod._wedge_state["awaiting_recovery"] is False
