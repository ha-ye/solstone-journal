# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pure-read SPL proof readiness observer.

The SPL relay proof and the follow-on P4e1 observer/landing lode both need the
same correlated pre-authorization view: one Callosum connection collects
``supervisor/status`` and ``link/health`` in arrival order, keeps that
connection open, and re-verifies freshness, process identity, relay generation,
and secure-listener acceptance immediately before the strict authorization
write. This module starts no services, creates no endpoints, writes no files,
and intentionally ignores Convey's cached HTTP status surface.

The secure-listener acceptance check is a bounded connect-and-close to
127.0.0.1:7657 with no bytes sent. Convey logs the accepted connection and EOF
close as two info records; that is the accepted tradeoff for an external
bound/accepting probe. CLI and HTTP diagnostics remain byte-identical.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from solstone.think.callosum import CallosumConnection
from solstone.think.link.paths import DEFAULT_RELAY_URL

STATUS_MAX_AGE_SECONDS = 5.0
READINESS_WINDOW_SECONDS = 31.0
SECURE_LISTENER_HOST = "127.0.0.1"
SECURE_LISTENER_PORT = 7657
SECURE_LISTENER_TIMEOUT_SECONDS = 0.4


class SplReadinessError(RuntimeError):
    """Stable-code refusal from the SPL readiness observer."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SplReadinessSnapshot:
    supervisor_ref: str
    spl_pid: int
    spl_ref: str
    convey_pid: int
    convey_ref: str
    spl_connection_state: str
    listen_generation: int
    link_health_observed_at_monotonic: float
    observed_relay_origin: str
    secure_listener_bound_accepting: bool
    supervisor_observed_at_monotonic: float


@dataclass(frozen=True, slots=True)
class _ProcessIdentity:
    pid: int
    ref: str
    create_time: float


class SplReadinessObserver:
    """One-window SPL readiness collector held open until authorization."""

    def __init__(
        self,
        journal: Path,
        *,
        window_seconds: float = READINESS_WINDOW_SECONDS,
    ) -> None:
        self._journal = Path(journal)
        self._window_seconds = window_seconds
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._conn: CallosumConnection | None = None
        self._latest_supervisor: tuple[float, dict[str, Any]] | None = None
        self._latest_link: tuple[float, dict[str, Any]] | None = None
        self._processes: dict[str, _ProcessIdentity] = {}

    def __enter__(self) -> SplReadinessObserver:
        sock_path = self._journal / "health" / "callosum.sock"
        if not sock_path.exists():
            raise SplReadinessError("callosum_socket_missing")
        self._conn = CallosumConnection(socket_path=sock_path)
        self._conn.start(callback=self._on_callosum)
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is not None:
            conn.stop()

    def wait_snapshot(self, *, deadline: float | None = None) -> SplReadinessSnapshot:
        stop = min(
            time.monotonic() + self._window_seconds,
            deadline if deadline is not None else float("inf"),
        )
        while True:
            timeout = max(0.0, stop - time.monotonic())
            if timeout <= 0:
                raise SplReadinessError("readiness_window_timeout")
            if self._ready.wait(timeout=timeout):
                break
        snapshot = self._build_snapshot()
        self._processes = {
            "spl": _capture_process(snapshot.spl_pid, snapshot.spl_ref),
            "convey": _capture_process(snapshot.convey_pid, snapshot.convey_ref),
        }
        return snapshot

    def reverify_before_authorization(
        self,
        snapshot: SplReadinessSnapshot,
    ) -> None:
        supervisor_observed, supervisor = self._latest_supervisor_event()
        if time.monotonic() - supervisor_observed > STATUS_MAX_AGE_SECONDS:
            raise SplReadinessError("supervisor_status_stale")
        services = _services_by_name(supervisor)
        crashed = _crashed_names(supervisor)
        if "spl" in crashed or "convey" in crashed:
            raise SplReadinessError("service_crashed")
        supervisor_entry = _required_service(services, "supervisor")
        if supervisor_entry["ref"] != snapshot.supervisor_ref:
            raise SplReadinessError("supervisor_ref_changed")
        spl = _required_service(services, "spl")
        convey = _required_service(services, "convey")
        _require_service_identity(spl, snapshot.spl_pid, snapshot.spl_ref)
        _require_service_identity(convey, snapshot.convey_pid, snapshot.convey_ref)
        _verify_process_identity(self._processes["spl"])
        _verify_process_identity(self._processes["convey"])
        link_observed, link = self._latest_link_event()
        _validate_link_health(link)
        if str(link.get("state")) != snapshot.spl_connection_state:
            raise SplReadinessError("link_state_changed")
        if int(link["listen_generation"]) != snapshot.listen_generation:
            raise SplReadinessError("link_generation_changed")
        if link_observed < snapshot.link_health_observed_at_monotonic:
            raise SplReadinessError("link_health_regressed")
        if not _secure_listener_accepting():
            raise SplReadinessError("secure_listener_unavailable")

    def _on_callosum(self, message: dict[str, Any]) -> None:
        observed_at = time.monotonic()
        tract = message.get("tract")
        event = message.get("event")
        with self._lock:
            if tract == "supervisor" and event == "status":
                self._latest_supervisor = (observed_at, dict(message))
            elif tract == "link" and event == "health":
                self._latest_link = (observed_at, dict(message))
            if self._latest_supervisor is not None and self._latest_link is not None:
                self._ready.set()

    def _build_snapshot(self) -> SplReadinessSnapshot:
        supervisor_observed, supervisor = self._latest_supervisor_event()
        link_observed, link = self._latest_link_event()
        if time.monotonic() - supervisor_observed > STATUS_MAX_AGE_SECONDS:
            raise SplReadinessError("supervisor_status_stale")
        services = _services_by_name(supervisor)
        crashed = _crashed_names(supervisor)
        if "spl" in crashed or "convey" in crashed:
            raise SplReadinessError("service_crashed")
        supervisor_entry = _required_service(services, "supervisor")
        spl = _required_service(services, "spl")
        convey = _required_service(services, "convey")
        _validate_link_health(link)
        relay_origin = observed_relay_origin(self._journal)
        listener_ok = _secure_listener_accepting()
        if not listener_ok:
            raise SplReadinessError("secure_listener_unavailable")
        return SplReadinessSnapshot(
            supervisor_ref=str(supervisor_entry["ref"]),
            spl_pid=int(spl["pid"]),
            spl_ref=str(spl["ref"]),
            convey_pid=int(convey["pid"]),
            convey_ref=str(convey["ref"]),
            spl_connection_state=str(link["state"]),
            listen_generation=int(link["listen_generation"]),
            link_health_observed_at_monotonic=link_observed,
            observed_relay_origin=relay_origin,
            secure_listener_bound_accepting=listener_ok,
            supervisor_observed_at_monotonic=supervisor_observed,
        )

    def _latest_supervisor_event(self) -> tuple[float, dict[str, Any]]:
        with self._lock:
            event = self._latest_supervisor
        if event is None:
            raise SplReadinessError("supervisor_status_missing")
        return event

    def _latest_link_event(self) -> tuple[float, dict[str, Any]]:
        with self._lock:
            event = self._latest_link
        if event is None:
            raise SplReadinessError("link_health_missing")
        return event


def observed_relay_origin(journal: Path) -> str:
    if os.environ.get("SOL_LINK_RELAY_URL", "").strip():
        raise SplReadinessError("relay_env_override")
    config_path = Path(journal) / "config" / "journal.json"
    try:
        raw = json.loads(config_path.read_text("utf-8"))
    except FileNotFoundError:
        raw = {}
    except (json.JSONDecodeError, OSError):
        raise SplReadinessError("relay_config_unreadable") from None
    link_cfg = raw.get("link") if isinstance(raw, dict) else None
    if isinstance(link_cfg, dict) and "relay_url" in link_cfg:
        value = link_cfg.get("relay_url")
        if isinstance(value, str) and value.strip():
            raise SplReadinessError("relay_config_override")
    return DEFAULT_RELAY_URL


def open_spl_readiness_observer(journal: Path) -> SplReadinessObserver:
    return SplReadinessObserver(journal)


def _services_by_name(message: dict[str, Any]) -> dict[str, dict[str, Any]]:
    services = message.get("services")
    if not isinstance(services, list):
        raise SplReadinessError("supervisor_services_invalid")
    out: dict[str, dict[str, Any]] = {}
    for service in services:
        if not isinstance(service, dict):
            continue
        name = service.get("name")
        if isinstance(name, str):
            out[name] = service
    return out


def _crashed_names(message: dict[str, Any]) -> set[str]:
    crashed = message.get("crashed")
    if not isinstance(crashed, list):
        return set()
    names: set[str] = set()
    for service in crashed:
        if isinstance(service, dict) and isinstance(service.get("name"), str):
            names.add(str(service["name"]))
    return names


def _required_service(
    services: dict[str, dict[str, Any]],
    name: str,
) -> dict[str, Any]:
    service = services.get(name)
    if service is None:
        raise SplReadinessError(f"{name}_service_missing")
    if not isinstance(service.get("ref"), str) or not service.get("ref"):
        raise SplReadinessError(f"{name}_service_ref_missing")
    if not isinstance(service.get("pid"), int):
        raise SplReadinessError(f"{name}_service_pid_missing")
    return service


def _require_service_identity(
    service: dict[str, Any],
    expected_pid: int,
    expected_ref: str,
) -> None:
    if service["pid"] != expected_pid or service["ref"] != expected_ref:
        raise SplReadinessError("service_identity_changed")


def _capture_process(pid: int, ref: str) -> _ProcessIdentity:
    identity = _ProcessIdentity(pid=pid, ref=ref, create_time=_process_create_time(pid))
    _verify_process_identity(identity)
    return identity


def _verify_process_identity(identity: _ProcessIdentity) -> None:
    try:
        os.kill(identity.pid, 0)
    except OSError:
        raise SplReadinessError("process_not_live") from None
    if _process_create_time(identity.pid) != identity.create_time:
        raise SplReadinessError("process_replaced")


def _process_create_time(pid: int) -> float:
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        raise SplReadinessError("process_create_time_unavailable") from None


def _validate_link_health(message: dict[str, Any]) -> None:
    state = message.get("state")
    generation = message.get("listen_generation")
    if state != "connected":
        raise SplReadinessError("link_state_not_connected")
    if not isinstance(generation, int):
        raise SplReadinessError("link_generation_missing")


def _secure_listener_accepting() -> bool:
    try:
        sock = socket.create_connection(
            (SECURE_LISTENER_HOST, SECURE_LISTENER_PORT),
            timeout=SECURE_LISTENER_TIMEOUT_SECONDS,
        )
    except OSError:
        return False
    with sock:
        return True


__all__ = [
    "SplReadinessError",
    "SplReadinessObserver",
    "SplReadinessSnapshot",
    "open_spl_readiness_observer",
    "observed_relay_origin",
]
