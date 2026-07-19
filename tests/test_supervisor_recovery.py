# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import asyncio
import logging
import re
import time
from unittest.mock import Mock

import pytest

import solstone.think.supervisor as mod


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
    monkeypatch.setattr(
        mod,
        "_recovery_state",
        {
            "local": mod.ProviderRecoveryState(),
            "parakeet": mod.ProviderRecoveryState(),
        },
    )


def _seed_policy(name: str, last_start_offset: float) -> mod.RestartPolicy:
    policy = mod.RestartPolicy()
    policy.last_start = time.time() - last_start_offset
    mod._RESTART_POLICIES[name] = policy
    return policy


def _error_records(caplog):
    return [record for record in caplog.records if record.levelno >= logging.ERROR]


def _reset_runtime_recovery(monkeypatch) -> dict[str, mod.ProviderRuntimeState]:
    states = {
        "local": mod.ProviderRuntimeState("local"),
        "parakeet": mod.ProviderRuntimeState("parakeet"),
    }
    monkeypatch.setattr(mod, "_is_remote_mode", False)
    monkeypatch.setattr(mod, "_provider_runtime_states", states)
    monkeypatch.setattr(
        mod,
        "_recovery_state",
        {
            "local": mod.ProviderRecoveryState(),
            "parakeet": mod.ProviderRecoveryState(),
        },
    )
    return states


def test_rising_edge_fires_once_per_local_generation(monkeypatch):
    states = _reset_runtime_recovery(monkeypatch)
    callosum = Mock()
    mod._supervisor_callosum = callosum
    states["local"].generation = 7
    mod._mark_provider_recovery_down("local")

    mod._finish_provider_startup_condition(states["local"], "ready")
    mod._finish_provider_startup_condition(states["local"], "ready")

    callosum.emit.assert_called_once_with("supervisor", "drain")
    assert mod._recovery_state["local"].down_generation == 7
    assert mod._recovery_state["local"].nudged_generation == 7


def test_startup_ready_does_not_nudge(monkeypatch):
    states = _reset_runtime_recovery(monkeypatch)
    callosum = Mock()
    mod._supervisor_callosum = callosum
    states["local"].generation = 1

    mod._finish_provider_startup_condition(states["local"], "ready")

    callosum.emit.assert_not_called()
    assert mod._recovery_state["local"].down_generation is None


def test_parakeet_ready_never_nudges_local_recovery(monkeypatch):
    states = _reset_runtime_recovery(monkeypatch)
    callosum = Mock()
    mod._supervisor_callosum = callosum
    states["parakeet"].generation = 4
    mod._mark_provider_recovery_down("parakeet")

    mod._finish_provider_startup_condition(states["parakeet"], "ready")

    callosum.emit.assert_not_called()
    assert mod._recovery_state["parakeet"].down_generation == 4
    assert mod._recovery_state["local"].down_generation is None


def test_flap_two_local_generations_nudge_twice(monkeypatch):
    states = _reset_runtime_recovery(monkeypatch)
    callosum = Mock()
    mod._supervisor_callosum = callosum

    states["local"].generation = 1
    mod._mark_provider_recovery_down("local")
    mod._finish_provider_startup_condition(states["local"], "ready")

    states["local"].generation = 2
    mod._mark_provider_recovery_down("local")
    mod._finish_provider_startup_condition(states["local"], "ready")
    mod._finish_provider_startup_condition(states["local"], "ready")

    assert [call.args for call in callosum.emit.call_args_list] == [
        ("supervisor", "drain"),
        ("supervisor", "drain"),
    ]
    assert mod._recovery_state["local"].nudged_generation == 2


def test_undeliverable_callosum_none(monkeypatch, caplog):
    states = _reset_runtime_recovery(monkeypatch)
    mod._supervisor_callosum = None
    states["local"].generation = 3
    mod._mark_provider_recovery_down("local")
    caplog.set_level(logging.WARNING)

    mod._finish_provider_startup_condition(states["local"], "ready")

    assert mod._recovery_state["local"].nudged_generation == 3
    assert "supervisor callosum unavailable" in caplog.text


def test_undeliverable_emit_raises(monkeypatch, caplog):
    states = _reset_runtime_recovery(monkeypatch)
    callosum = Mock()
    callosum.emit.side_effect = RuntimeError("boom")
    mod._supervisor_callosum = callosum
    states["local"].generation = 5
    mod._mark_provider_recovery_down("local")
    caplog.set_level(logging.WARNING)

    mod._finish_provider_startup_condition(states["local"], "ready")
    mod._finish_provider_startup_condition(states["local"], "ready")

    callosum.emit.assert_called_once_with("supervisor", "drain")
    assert mod._recovery_state["local"].nudged_generation == 5
    assert "Cannot nudge catchup drain: boom" in caplog.text


def test_nudge_no_targeting():
    callosum = Mock()
    mod._supervisor_callosum = callosum

    mod._nudge_catchup_drain()

    callosum.emit.assert_called_once_with("supervisor", "drain")


def test_remote_mode_inert(monkeypatch, mock_callosum):
    states = _reset_runtime_recovery(monkeypatch)
    callosum = Mock()
    mod._supervisor_callosum = callosum
    monkeypatch.setattr(mod, "_is_remote_mode", True)
    states["local"].generation = 9
    mod._mark_provider_recovery_down("local")

    mod._finish_provider_startup_condition(states["local"], "ready")

    callosum.emit.assert_not_called()
    assert mod._recovery_state["local"].nudged_generation is None


def test_handle_runner_exits_reports_llama_server_to_local_recovery(monkeypatch):
    _setup_runner_exit_test(monkeypatch)
    states = _reset_runtime_recovery(monkeypatch)
    states["local"].generation = 1
    writes = []
    mod._SERVICE_STATE[mod.LOCAL_SERVER_PROCESS_NAME] = {"restart": True}
    managed = _ManagedStub(
        mod.LOCAL_SERVER_PROCESS_NAME,
        ["/tmp/llama-server", "-m", "/tmp/model.gguf"],
    )

    def write_runtime(_state, **kwargs):
        writes.append(kwargs)

    monkeypatch.setattr(mod, "_write_provider_runtime", write_runtime)
    monkeypatch.setattr(
        mod,
        "_launch_process",
        Mock(side_effect=AssertionError("provider exit must not relaunch")),
    )

    procs = [managed]
    asyncio.run(mod.handle_runner_exits(procs))

    assert procs == []
    assert states["local"].generation == 2
    assert states["local"].latest_phase == "stopped"
    assert states["local"].next_truth_at == 0.0
    assert mod._recovery_state["local"].down_generation == 2
    assert writes[0]["phase"] == "stopped"
    assert writes[0]["reason_code"] == "process-exited"


def test_handle_runner_exits_reports_mlx_server_to_local_recovery(monkeypatch):
    _setup_runner_exit_test(monkeypatch)
    states = _reset_runtime_recovery(monkeypatch)
    states["local"].generation = 10
    writes = []
    mod._SERVICE_STATE[mod.MLX_SERVER_PROCESS_NAME] = {"restart": True}
    managed = _ManagedStub(
        mod.MLX_SERVER_PROCESS_NAME,
        ["/tmp/mlx-vlm-server", "--model", "/tmp/model"],
    )

    def write_runtime(_state, **kwargs):
        writes.append(kwargs)

    monkeypatch.setattr(mod, "_write_provider_runtime", write_runtime)
    monkeypatch.setattr(
        mod,
        "_launch_process",
        Mock(side_effect=AssertionError("provider exit must not relaunch")),
    )

    asyncio.run(mod.handle_runner_exits([managed]))

    assert states["local"].generation == 11
    assert mod._recovery_state["local"].down_generation == 11
    assert writes[0]["reason_code"] == "process-exited"


def test_handle_runner_exits_reports_parakeet_without_local_recovery(monkeypatch):
    _setup_runner_exit_test(monkeypatch)
    states = _reset_runtime_recovery(monkeypatch)
    states["parakeet"].generation = 4
    writes = []
    managed = _ManagedStub(
        mod.PARAKEET_SERVER_PROCESS_NAME,
        ["/tmp/parakeet-server"],
    )

    def write_runtime(_state, **kwargs):
        writes.append(kwargs)

    monkeypatch.setattr(mod, "_write_provider_runtime", write_runtime)
    monkeypatch.setattr(
        mod,
        "_launch_process",
        Mock(side_effect=AssertionError("provider exit must not relaunch")),
    )

    asyncio.run(mod.handle_runner_exits([managed]))

    assert states["parakeet"].generation == 5
    assert mod._recovery_state["parakeet"].down_generation == 5
    assert mod._recovery_state["local"].down_generation is None
    assert writes[0]["reason_code"] == "process-exited"


def test_handle_runner_exits_no_flag_for_other_service(monkeypatch):
    _setup_runner_exit_test(monkeypatch)
    mod._SERVICE_STATE["journal:cortex"] = {"restart": True}
    managed = _ManagedStub("journal:cortex", ["journal", "cortex"])
    replacement = _ManagedStub("journal:cortex", managed.cmd)

    def fake_launch(name, cmd, *, restart=False, shutdown_timeout=15, ref=None):
        return replacement

    monkeypatch.setattr(mod, "_launch_process", fake_launch)

    procs = [managed]
    asyncio.run(mod.handle_runner_exits(procs))

    assert procs == [replacement]
    assert mod._recovery_state["local"].down_generation is None
    assert mod._recovery_state["parakeet"].down_generation is None


@pytest.mark.parametrize(
    ("service", "cmd"),
    [
        ("cortex", ["journal", "cortex", "-v"]),
        ("sense", ["journal", "sense", "-v"]),
        ("spl", ["journal", "spl", "-v"]),
        ("convey", ["journal", "convey", "--port", "5015"]),
    ],
)
def test_handle_runner_exits_restarts_non_provider_services_identically(
    monkeypatch,
    service: str,
    cmd: list[str],
) -> None:
    _setup_runner_exit_test(monkeypatch)
    monkeypatch.setattr(mod.time, "time", lambda: 200.0)
    policy = mod.RestartPolicy()
    policy.last_start = 100.0
    mod._RESTART_POLICIES[service] = policy
    mod._SERVICE_STATE[service] = {
        "restart": True,
        "shutdown_timeout": 12,
    }
    managed = _ManagedStub(service, cmd)
    replacement = _ManagedStub(service, cmd)
    launches = []

    def fake_launch(name, cmd_arg, *, restart=False, shutdown_timeout=15, ref=None):
        launches.append((name, cmd_arg, restart, shutdown_timeout, ref))
        return replacement

    monkeypatch.setattr(mod, "_launch_process", fake_launch)

    procs = [managed]
    asyncio.run(mod.handle_runner_exits(procs))

    assert launches == [(service, cmd, True, 12, None)]
    assert procs == [replacement]
    assert mod._recovery_state["local"].down_generation is None
    assert mod._recovery_state["parakeet"].down_generation is None


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
