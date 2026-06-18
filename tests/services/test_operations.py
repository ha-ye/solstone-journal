# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import threading
import time

import pytest

from solstone.think.services import operations


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    operations.clear_registry()
    yield
    operations.clear_registry()


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


def test_start_operation_raises_busy_for_same_service() -> None:
    started = threading.Event()
    release = threading.Event()

    def flow():
        started.set()
        release.wait(2)
        return operations.HandoffResult("enabled", None, False)

    operations.start_operation("scout", "enable", "https://portal.test/scout", flow)
    _wait_until(started.is_set)

    with pytest.raises(operations.OperationBusyError):
        operations.start_operation(
            "scout", "refresh", "https://portal.test/scout", flow
        )

    release.set()


def test_different_services_can_run_concurrently() -> None:
    scout_started = threading.Event()
    spl_started = threading.Event()
    release = threading.Event()

    def scout_flow():
        scout_started.set()
        release.wait(2)
        return operations.HandoffResult("enabled", None, False)

    def spl_flow():
        spl_started.set()
        release.wait(2)
        return operations.HandoffResult("enabled", None, False)

    operations.start_operation(
        "scout", "enable", "https://portal.test/scout", scout_flow
    )
    operations.start_operation("spl", "spl_enable", "https://portal.test/spl", spl_flow)

    _wait_until(scout_started.is_set)
    _wait_until(spl_started.is_set)
    release.set()


def test_sweep_drops_completed_entry_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations.start_operation(
        "scout",
        "enable",
        "https://portal.test/scout",
        lambda: operations.HandoffResult("enabled", None, False),
    )
    _wait_until(lambda: operations.operation_for_service("scout")["phase"] == "enabled")

    monkeypatch.setattr(operations, "OPERATION_GRACE_SECONDS", -1.0)

    assert operations.operation_for_service("scout") is None


def test_flow_exception_becomes_retryable_error() -> None:
    def fail():
        raise RuntimeError("boom")

    operations.start_operation("scout", "enable", "https://portal.test/scout", fail)

    def is_error() -> bool:
        operation = operations.operation_for_service("scout")
        return operation is not None and operation["phase"] == "error"

    _wait_until(is_error)
    operation = operations.operation_for_service("scout")
    assert operation["retryable"] is True
    assert operation["portal_url"] == "https://portal.test/scout"


def test_clear_registry_empties_entries() -> None:
    operations.start_operation(
        "scout",
        "enable",
        "https://portal.test/scout",
        lambda: operations.HandoffResult("enabled", None, False),
    )
    _wait_until(lambda: operations.operation_for_service("scout") is not None)

    operations.clear_registry()

    assert operations.operation_for_service("scout") is None
