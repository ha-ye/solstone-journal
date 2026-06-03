# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import asyncio
import logging
import re
import time
from unittest.mock import Mock

import solstone.think.supervisor as mod
from solstone.think.providers import local_server


def _set_local_port(monkeypatch, port: int = 9999) -> None:
    monkeypatch.setattr(mod, "read_service_port", lambda service: port)


def _capture_callosum_messages() -> list[dict]:
    received: list[dict] = []
    listener = mod.CallosumConnection()
    listener.start(callback=received.append)
    emitter = mod.CallosumConnection()
    emitter.start()
    mod._supervisor_callosum = emitter
    return received


def _drain_messages(messages: list[dict]) -> list[dict]:
    return [
        message
        for message in messages
        if message.get("tract") == "supervisor" and message.get("event") == "drain"
    ]


class _ProcessStub:
    def __init__(self, returncode: int = 1):
        self.poll = Mock(return_value=returncode)
        self.returncode = returncode
        self.pid = 12345


class _ManagedStub:
    def __init__(self, name: str, cmd: list[str], returncode: int = 1):
        self.name = name
        self.cmd = cmd
        self.process = _ProcessStub(returncode)
        self.ref = f"{name}-ref"
        self.cleanup = Mock()


def _setup_runner_exit_test(monkeypatch) -> None:
    monkeypatch.setattr(mod, "_SERVICE_STATE", {})
    monkeypatch.setattr(mod, "_RESTART_POLICIES", {})
    monkeypatch.setattr(mod, "shutdown_requested", False)
    monkeypatch.setattr(mod, "_supervisor_callosum", None)


def _seed_policy(name: str, last_start_offset: float) -> mod.RestartPolicy:
    policy = mod.RestartPolicy()
    policy.last_start = time.time() - last_start_offset
    mod._RESTART_POLICIES[name] = policy
    return policy


def _error_records(caplog):
    return [record for record in caplog.records if record.levelno >= logging.ERROR]


def test_rising_edge_fires_once(monkeypatch, mock_callosum):
    _set_local_port(monkeypatch)
    received = _capture_callosum_messages()
    monkeypatch.setattr(
        local_server,
        "_probe_health",
        lambda port: (local_server.STATE_READY, None),
    )
    mod._recovery_state["local_server_down"] = True

    asyncio.run(mod._check_local_server_recovery())

    assert len(_drain_messages(received)) == 1
    assert mod._recovery_state["local_server_down"] is False


def test_startup_ready_does_not_nudge(monkeypatch, mock_callosum):
    read_service_port = Mock(return_value=9999)
    probe_health = Mock(return_value=(local_server.STATE_READY, None))
    monkeypatch.setattr(mod, "read_service_port", read_service_port)
    monkeypatch.setattr(local_server, "_probe_health", probe_health)
    received = _capture_callosum_messages()
    mod._recovery_state["local_server_down"] = False

    asyncio.run(mod._check_local_server_recovery())

    read_service_port.assert_not_called()
    probe_health.assert_not_called()
    assert _drain_messages(received) == []


def test_steady_state_no_nudge(monkeypatch, mock_callosum):
    _set_local_port(monkeypatch)
    probe_health = Mock(return_value=(local_server.STATE_READY, None))
    monkeypatch.setattr(local_server, "_probe_health", probe_health)
    received = _capture_callosum_messages()
    mod._recovery_state["local_server_down"] = False

    asyncio.run(mod._check_local_server_recovery())
    asyncio.run(mod._check_local_server_recovery())

    probe_health.assert_not_called()
    assert _drain_messages(received) == []

    probe_health = Mock(return_value=(local_server.STATE_LOADING, None))
    monkeypatch.setattr(local_server, "_probe_health", probe_health)
    mod._recovery_state["local_server_down"] = True

    asyncio.run(mod._check_local_server_recovery())

    probe_health.assert_called_once_with(9999)
    assert _drain_messages(received) == []
    assert mod._recovery_state["local_server_down"] is True


def test_flap_two_nudges(monkeypatch, mock_callosum):
    _set_local_port(monkeypatch)
    probe_health = Mock(return_value=(local_server.STATE_READY, None))
    monkeypatch.setattr(local_server, "_probe_health", probe_health)
    received = _capture_callosum_messages()

    mod._recovery_state["local_server_down"] = True
    asyncio.run(mod._check_local_server_recovery())

    asyncio.run(mod._check_local_server_recovery())

    mod._recovery_state["local_server_down"] = True
    asyncio.run(mod._check_local_server_recovery())

    assert len(_drain_messages(received)) == 2
    assert probe_health.call_count == 2
    assert mod._recovery_state["local_server_down"] is False


def test_undeliverable_callosum_none(monkeypatch, caplog):
    _set_local_port(monkeypatch)
    monkeypatch.setattr(
        local_server,
        "_probe_health",
        lambda port: (local_server.STATE_READY, None),
    )
    mod._supervisor_callosum = None
    mod._recovery_state["local_server_down"] = True
    caplog.set_level(logging.WARNING)

    asyncio.run(mod._check_local_server_recovery())

    assert mod._recovery_state["local_server_down"] is False
    assert "supervisor callosum unavailable" in caplog.text


def test_undeliverable_emit_raises(monkeypatch, caplog):
    _set_local_port(monkeypatch)
    probe_health = Mock(return_value=(local_server.STATE_READY, None))
    monkeypatch.setattr(local_server, "_probe_health", probe_health)
    callosum = Mock()
    callosum.emit.side_effect = RuntimeError("boom")
    mod._supervisor_callosum = callosum
    mod._recovery_state["local_server_down"] = True
    caplog.set_level(logging.WARNING)

    asyncio.run(mod._check_local_server_recovery())
    asyncio.run(mod._check_local_server_recovery())

    assert mod._recovery_state["local_server_down"] is False
    callosum.emit.assert_called_once_with("supervisor", "drain")
    probe_health.assert_called_once_with(9999)
    assert "Cannot nudge catchup drain: boom" in caplog.text


def test_nudge_no_targeting():
    callosum = Mock()
    mod._supervisor_callosum = callosum

    mod._nudge_catchup_drain()

    callosum.emit.assert_called_once_with("supervisor", "drain")


def test_remote_mode_inert(monkeypatch, mock_callosum):
    read_service_port = Mock(return_value=9999)
    probe_health = Mock(return_value=(local_server.STATE_READY, None))
    monkeypatch.setattr(mod, "read_service_port", read_service_port)
    monkeypatch.setattr(local_server, "_probe_health", probe_health)
    received = _capture_callosum_messages()
    mod._is_remote_mode = True
    mod._recovery_state["local_server_down"] = True

    asyncio.run(mod._check_local_server_recovery())

    read_service_port.assert_not_called()
    probe_health.assert_not_called()
    assert _drain_messages(received) == []
    assert mod._recovery_state["local_server_down"] is True


def test_handle_runner_exits_sets_flag_for_llama_server(monkeypatch):
    monkeypatch.setattr(mod, "_SERVICE_STATE", {})
    monkeypatch.setattr(mod, "_RESTART_POLICIES", {})
    monkeypatch.setattr(mod, "shutdown_requested", False)
    monkeypatch.setattr(mod, "_supervisor_callosum", None)
    mod._SERVICE_STATE[mod.LOCAL_SERVER_PROCESS_NAME] = {"restart": True}
    managed = _ManagedStub(
        mod.LOCAL_SERVER_PROCESS_NAME,
        ["/tmp/llama-server", "-m", "/tmp/model.gguf"],
    )
    replacement = _ManagedStub(mod.LOCAL_SERVER_PROCESS_NAME, managed.cmd)

    def fake_launch(name, cmd, *, restart=False, shutdown_timeout=15, ref=None):
        return replacement

    monkeypatch.setattr(mod, "_launch_process", fake_launch)
    mod._recovery_state["local_server_down"] = False

    asyncio.run(mod.handle_runner_exits([managed]))

    assert mod._recovery_state["local_server_down"] is True


def test_handle_runner_exits_sets_flag_for_mlx_server(monkeypatch):
    monkeypatch.setattr(mod, "_SERVICE_STATE", {})
    monkeypatch.setattr(mod, "_RESTART_POLICIES", {})
    monkeypatch.setattr(mod, "shutdown_requested", False)
    monkeypatch.setattr(mod, "_supervisor_callosum", None)
    mod._SERVICE_STATE[mod.MLX_SERVER_PROCESS_NAME] = {"restart": True}
    managed = _ManagedStub(
        mod.MLX_SERVER_PROCESS_NAME,
        ["/tmp/mlx-vlm-server", "--model", "/tmp/model"],
    )
    replacement = _ManagedStub(mod.MLX_SERVER_PROCESS_NAME, managed.cmd)

    def fake_launch(name, cmd, *, restart=False, shutdown_timeout=15, ref=None):
        return replacement

    monkeypatch.setattr(mod, "_launch_process", fake_launch)
    mod._recovery_state["local_server_down"] = False

    asyncio.run(mod.handle_runner_exits([managed]))

    assert mod._recovery_state["local_server_down"] is True


def test_handle_runner_exits_no_flag_for_other_service(monkeypatch):
    monkeypatch.setattr(mod, "_SERVICE_STATE", {})
    monkeypatch.setattr(mod, "_RESTART_POLICIES", {})
    monkeypatch.setattr(mod, "shutdown_requested", False)
    monkeypatch.setattr(mod, "_supervisor_callosum", None)
    mod._SERVICE_STATE["journal:cortex"] = {"restart": True}
    managed = _ManagedStub("journal:cortex", ["journal", "cortex"])
    replacement = _ManagedStub("journal:cortex", managed.cmd)

    def fake_launch(name, cmd, *, restart=False, shutdown_timeout=15, ref=None):
        return replacement

    monkeypatch.setattr(mod, "_launch_process", fake_launch)
    mod._recovery_state["local_server_down"] = False

    asyncio.run(mod.handle_runner_exits([managed]))

    assert mod._recovery_state["local_server_down"] is False


def test_handle_runner_exits_error_describes_sigkill(monkeypatch, caplog):
    _setup_runner_exit_test(monkeypatch)
    _seed_policy("convey", 0.4)
    caplog.set_level(logging.INFO)
    managed = _ManagedStub("convey", ["journal", "convey"], returncode=-9)

    asyncio.run(mod.handle_runner_exits([managed]))

    errors = _error_records(caplog)
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "convey" in message
    assert "SIGKILL" in message
    assert "-9" in message


def test_handle_runner_exits_error_describes_unknown_signal(monkeypatch, caplog):
    _setup_runner_exit_test(monkeypatch)
    _seed_policy("convey", 0.4)
    caplog.set_level(logging.INFO)
    managed = _ManagedStub("convey", ["journal", "convey"], returncode=-99)

    asyncio.run(mod.handle_runner_exits([managed]))

    errors = _error_records(caplog)
    assert len(errors) == 1
    assert "-99" in errors[0].getMessage()


def test_handle_runner_exits_error_describes_positive_exit(monkeypatch, caplog):
    _setup_runner_exit_test(monkeypatch)
    _seed_policy("convey", 0.4)
    caplog.set_level(logging.INFO)
    managed = _ManagedStub("convey", ["journal", "convey"], returncode=1)

    asyncio.run(mod.handle_runner_exits([managed]))

    errors = _error_records(caplog)
    assert len(errors) == 1
    assert "exit 1" in errors[0].getMessage()


def test_handle_runner_exits_error_describes_multiple_sorted(monkeypatch, caplog):
    _setup_runner_exit_test(monkeypatch)
    _seed_policy("convey", 0.4)
    _seed_policy("cortex", 3601)
    caplog.set_level(logging.INFO)
    managed = [
        _ManagedStub("convey", ["journal", "convey"], returncode=-9),
        _ManagedStub("cortex", ["journal", "cortex"], returncode=1),
    ]

    asyncio.run(mod.handle_runner_exits(managed))

    errors = _error_records(caplog)
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "convey (exit -9 / SIGKILL" in message
    assert "cortex (exit 1" in message
    assert "SIGKILL" in message
    assert "-9" in message
    assert "exit 1" in message
    assert message.index("convey") < message.index("cortex")


def test_handle_runner_exits_all_tempfail_does_not_error(monkeypatch, caplog):
    _setup_runner_exit_test(monkeypatch)
    _seed_policy("convey", 0.4)
    caplog.set_level(logging.INFO)
    managed = _ManagedStub(
        "convey",
        ["journal", "convey"],
        returncode=mod.EXIT_TEMPFAIL,
    )

    asyncio.run(mod.handle_runner_exits([managed]))

    assert _error_records(caplog) == []
    assert "Runner waiting for session:" in caplog.text


def test_handle_runner_exits_error_uses_fresh_uptime(monkeypatch, caplog):
    _setup_runner_exit_test(monkeypatch)
    isolated_policy = _seed_policy("isolated", 3600)
    isolated_policy.attempts = 5
    _seed_policy("rapid", 0.1)
    caplog.set_level(logging.INFO)

    asyncio.run(
        mod.handle_runner_exits(
            [_ManagedStub("isolated", ["journal", "isolated"], returncode=1)]
        )
    )
    asyncio.run(
        mod.handle_runner_exits(
            [_ManagedStub("rapid", ["journal", "rapid"], returncode=1)]
        )
    )

    errors = _error_records(caplog)
    assert len(errors) == 2
    messages = [record.getMessage() for record in errors]
    isolated_message = next(message for message in messages if "isolated" in message)
    rapid_message = next(message for message in messages if "rapid" in message)
    isolated_uptime = float(re.search(r"up ([0-9.]+)s", isolated_message).group(1))
    rapid_uptime = float(re.search(r"up ([0-9.]+)s", rapid_message).group(1))
    assert isolated_uptime >= 1000
    assert rapid_uptime < 60
    assert "restart" not in isolated_message
    assert "attempt" not in isolated_message
