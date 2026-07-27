# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import pytest

from solstone.think.link import client as link_client
from solstone.think.sandbox_profile import probe_contract, spl_readiness
from solstone.think.sandbox_profile import spl_relay_tunnel as probe
from tests.sandbox_profile import RUN_ID, sandbox_journal, write_attempt_dir


def test_snapshot_field_set_is_exact() -> None:
    assert tuple(
        field.name for field in dataclasses.fields(spl_readiness.SplReadinessSnapshot)
    ) == (
        "supervisor_ref",
        "spl_pid",
        "spl_ref",
        "convey_pid",
        "convey_ref",
        "spl_connection_state",
        "listen_generation",
        "link_health_observed_at_monotonic",
        "observed_relay_origin",
        "secure_listener_bound_accepting",
        "supervisor_observed_at_monotonic",
    )


def test_snapshot_and_reverify_use_same_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _journal(tmp_path)
    fake = _install_fake_connection(monkeypatch)
    _install_process_and_socket(monkeypatch)

    with spl_readiness.open_spl_readiness_observer(journal) as observer:
        fake.emit(_supervisor_status())
        fake.emit(_link_health(generation=7))
        snapshot = observer.wait_snapshot(deadline=time.monotonic() + 1)

        assert snapshot.supervisor_ref == "supervisor-ref"
        assert snapshot.spl_pid == 101
        assert snapshot.spl_ref == "spl-ref"
        assert snapshot.convey_pid == 202
        assert snapshot.convey_ref == "convey-ref"
        assert snapshot.spl_connection_state == "connected"
        assert snapshot.listen_generation == 7
        assert snapshot.observed_relay_origin == "https://link.solstone.app"
        assert snapshot.secure_listener_bound_accepting is True

        fake.emit(_supervisor_status())
        observer.reverify_before_authorization(snapshot)

    assert fake.stopped is True


def test_reverify_refuses_generation_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _journal(tmp_path)
    fake = _install_fake_connection(monkeypatch)
    _install_process_and_socket(monkeypatch)

    with spl_readiness.open_spl_readiness_observer(journal) as observer:
        fake.emit(_supervisor_status())
        fake.emit(_link_health(generation=7))
        snapshot = observer.wait_snapshot(deadline=time.monotonic() + 1)
        fake.emit(_link_health(generation=8))

        with pytest.raises(spl_readiness.SplReadinessError) as excinfo:
            observer.reverify_before_authorization(snapshot)

    assert excinfo.value.code == "link_generation_changed"


def test_reverify_refuses_process_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _journal(tmp_path)
    fake = _install_fake_connection(monkeypatch)
    create_times = {101: 10.0, 202: 20.0}
    _install_process_and_socket(monkeypatch, create_times=create_times)

    with spl_readiness.open_spl_readiness_observer(journal) as observer:
        fake.emit(_supervisor_status())
        fake.emit(_link_health(generation=7))
        snapshot = observer.wait_snapshot(deadline=time.monotonic() + 1)
        create_times[101] = 11.0
        fake.emit(_supervisor_status())

        with pytest.raises(spl_readiness.SplReadinessError) as excinfo:
            observer.reverify_before_authorization(snapshot)

    assert excinfo.value.code == "process_replaced"


def test_reverify_refuses_stale_supervisor_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _journal(tmp_path)
    fake = _install_fake_connection(monkeypatch)
    _install_process_and_socket(monkeypatch)
    now = [100.0]
    monkeypatch.setattr(spl_readiness.time, "monotonic", lambda: now[0])

    with spl_readiness.open_spl_readiness_observer(journal) as observer:
        fake.emit(_supervisor_status())
        fake.emit(_link_health(generation=7))
        snapshot = observer.wait_snapshot(deadline=101.0)
        now[0] = 100.0 + spl_readiness.STATUS_MAX_AGE_SECONDS + 0.01

        with pytest.raises(spl_readiness.SplReadinessError) as excinfo:
            observer.reverify_before_authorization(snapshot)

    assert excinfo.value.code == "supervisor_status_stale"


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("missing_spl", "spl_service_missing"),
        ("missing_convey", "convey_service_missing"),
        ("crashed_spl", "service_crashed"),
        ("missing_link", "link_health_missing"),
        ("link_not_connected", "link_state_not_connected"),
        ("listener_unavailable", "secure_listener_unavailable"),
    ],
)
def test_snapshot_refuses_unready_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    code: str,
) -> None:
    journal = _journal(tmp_path)
    supervisor, link, socket_ok = _snapshot_case(case)
    _install_process_and_socket(monkeypatch, socket_ok=socket_ok)
    observer = spl_readiness.SplReadinessObserver(journal)
    observer._latest_supervisor = (time.monotonic(), supervisor)
    if link is not None:
        observer._latest_link = (time.monotonic(), link)

    with pytest.raises(spl_readiness.SplReadinessError) as excinfo:
        observer._build_snapshot()

    assert excinfo.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "supervisor_status_stale",
        "spl_service_missing",
        "convey_service_missing",
        "service_crashed",
        "link_health_missing",
        "link_state_not_connected",
        "secure_listener_unavailable",
    ],
)
async def test_readiness_refusals_map_capability_not_ready_without_write_or_contact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch, run_id=RUN_ID)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    (journal / "config").mkdir(parents=True, exist_ok=True)
    (journal / "config" / "journal.json").write_text("{}\n", encoding="utf-8")
    attempt = write_attempt_dir(journal)
    calls: list[str] = []

    class RefusingObserver:
        def __enter__(self) -> RefusingObserver:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def wait_snapshot(self, *, deadline: float | None = None) -> object:
            raise spl_readiness.SplReadinessError(code)

    async def enroll(*_args: object, **_kwargs: object) -> object:
        calls.append("enroll")
        raise AssertionError("enrollment must not be reached")

    async def dial(*_args: object, **_kwargs: object) -> object:
        calls.append("dial")
        raise AssertionError("dial must not be reached")

    monkeypatch.setattr(
        probe.spl_readiness,
        "open_spl_readiness_observer",
        lambda _journal: RefusingObserver(),
    )
    monkeypatch.setattr(link_client.Client, "enroll_device_async", enroll)
    monkeypatch.setattr(probe, "_dial_with_deadline", dial)

    lease, outcome = await probe.prove_spl_relay_tunnel(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert lease is None
    assert outcome["reason"] == probe_contract.REASON_CAPABILITY_NOT_READY
    assert outcome["checks"] == ()
    assert calls == []
    assert not (journal / "link" / "authorized_clients.json").exists()


def test_config_relay_url_refuses_even_when_default(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    (journal / "config" / "journal.json").write_text(
        json.dumps({"link": {"relay_url": "https://link.solstone.app"}}, indent=2)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(spl_readiness.SplReadinessError) as excinfo:
        spl_readiness.observed_relay_origin(journal)

    assert excinfo.value.code == "relay_config_override"


def test_env_relay_url_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = _journal(tmp_path)
    monkeypatch.setenv("SOL_LINK_RELAY_URL", "https://elsewhere.test")

    with pytest.raises(spl_readiness.SplReadinessError) as excinfo:
        spl_readiness.observed_relay_origin(journal)

    assert excinfo.value.code == "relay_env_override"


def _journal(tmp_path: Path) -> Path:
    journal = tmp_path / "journal"
    (journal / "health").mkdir(parents=True)
    (journal / "health" / "callosum.sock").write_text("", encoding="utf-8")
    (journal / "config").mkdir()
    (journal / "config" / "journal.json").write_text("{}\n", encoding="utf-8")
    return journal


class _FakeCallosumConnection:
    instance: _FakeCallosumConnection | None = None

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.callback = None
        self.stopped = False
        _FakeCallosumConnection.instance = self

    def start(self, callback) -> None:
        self.callback = callback

    def stop(self) -> None:
        self.stopped = True

    def emit(self, message: dict[str, Any]) -> None:
        assert self.callback is not None
        self.callback(message)


def _install_fake_connection(monkeypatch: pytest.MonkeyPatch) -> _FakeCallosumProxy:
    _FakeCallosumConnection.instance = None
    monkeypatch.setattr(spl_readiness, "CallosumConnection", _FakeCallosumConnection)
    return _FakeCallosumProxy()


class _FakeCallosumProxy:
    @property
    def stopped(self) -> bool:
        assert _FakeCallosumConnection.instance is not None
        return _FakeCallosumConnection.instance.stopped

    def emit(self, message: dict[str, Any]) -> None:
        assert _FakeCallosumConnection.instance is not None
        _FakeCallosumConnection.instance.emit(message)


def _install_process_and_socket(
    monkeypatch: pytest.MonkeyPatch,
    *,
    create_times: dict[int, float] | None = None,
    socket_ok: bool = True,
) -> None:
    create_times = create_times if create_times is not None else {101: 10.0, 202: 20.0}

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def create_time(self) -> float:
            return create_times[self.pid]

    class FakeSocket:
        def __enter__(self) -> FakeSocket:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(spl_readiness.psutil, "Process", FakeProcess)
    monkeypatch.setattr(spl_readiness.os, "kill", lambda _pid, _sig: None)
    if socket_ok:
        monkeypatch.setattr(
            spl_readiness.socket,
            "create_connection",
            lambda _addr, timeout: FakeSocket(),
        )
    else:
        monkeypatch.setattr(
            spl_readiness.socket,
            "create_connection",
            lambda _addr, timeout: (_ for _ in ()).throw(OSError("refused")),
        )


def _snapshot_case(
    case: str,
) -> tuple[dict[str, object], dict[str, object] | None, bool]:
    if case == "missing_spl":
        return _supervisor_status(omit=("spl",)), _link_health(generation=7), True
    if case == "missing_convey":
        return _supervisor_status(omit=("convey",)), _link_health(generation=7), True
    if case == "crashed_spl":
        return (
            _supervisor_status(omit=("spl",), crashed=("spl",)),
            _link_health(generation=7),
            True,
        )
    if case == "missing_link":
        return _supervisor_status(), None, True
    if case == "link_not_connected":
        return (
            _supervisor_status(),
            _link_health(generation=7, state="connecting"),
            True,
        )
    if case == "listener_unavailable":
        return _supervisor_status(), _link_health(generation=7), False
    raise AssertionError(f"unknown case {case}")


def _supervisor_status(
    *,
    omit: tuple[str, ...] = (),
    crashed: tuple[str, ...] = (),
) -> dict[str, object]:
    services = [
        {"name": "supervisor", "ref": "supervisor-ref", "pid": 1},
        {"name": "spl", "ref": "spl-ref", "pid": 101},
        {"name": "convey", "ref": "convey-ref", "pid": 202},
    ]
    return {
        "tract": "supervisor",
        "event": "status",
        "services": [
            service for service in services if str(service["name"]) not in omit
        ],
        "crashed": [{"name": name, "restart_attempts": 1} for name in crashed],
    }


def _link_health(*, generation: int, state: str = "connected") -> dict[str, object]:
    return {
        "tract": "link",
        "event": "health",
        "state": state,
        "listen_generation": generation,
    }
