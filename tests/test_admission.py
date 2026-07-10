# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from solstone.observe.transcribe.resource import stt_local_floor_bytes
from solstone.think import admission

MIB = 1024**2
GIB = 1024**3


@pytest.fixture(autouse=True)
def _reset_admission_state() -> Iterator[None]:
    admission.reset_admission_state()
    yield
    admission.reset_admission_state()


def _memory_config(floor_mib: object) -> dict[str, object]:
    return {"memory": {"floor_mib": floor_mib}}


def test_wait_blocks_until_available_reaches_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    floor = 5 * GIB
    readings = iter([3 * GIB, 4 * GIB, 6 * GIB])
    sleeps: list[float] = []
    starts: list[dict[str, object]] = []
    ends: list[dict[str, object]] = []

    monkeypatch.setattr(admission, "get_config", lambda: _memory_config(5 * 1024))
    monkeypatch.setattr(admission, "read_available_bytes", lambda: next(readings))
    monkeypatch.setattr(admission.time, "sleep", lambda seconds: sleeps.append(seconds))

    waited = admission.wait_for_memory_headroom(
        "describe",
        on_throttle_start=lambda **fields: starts.append(fields),
        on_throttle_end=lambda **fields: ends.append(fields),
    )

    assert waited >= 0.0
    assert sleeps == [1.0, 1.0]
    assert starts == [
        {
            "stage": "describe",
            "available_bytes": 3 * GIB,
            "floor_bytes": floor,
            "available_mib": 3 * 1024,
            "floor_mib": 5 * 1024,
        }
    ]
    assert len(ends) == 1
    assert ends[0]["stage"] == "describe"
    assert isinstance(ends[0]["waited_seconds"], float)
    assert admission.throttle_state() == admission.ThrottleState(
        throttled=False,
        count=0,
        floor_mib=None,
        available_mib=None,
    )


def test_unreliable_memory_warns_once_and_admits(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(admission, "get_config", lambda: _memory_config(5 * 1024))
    monkeypatch.setattr(admission, "read_available_bytes", lambda: None)
    caplog.set_level(logging.WARNING, logger=admission.LOG.name)

    assert admission.wait_for_memory_headroom("describe") == 0.0
    assert admission.wait_for_memory_headroom("transcribe") == 0.0

    warnings = [
        record
        for record in caplog.records
        if "Memory reading unreliable" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert admission.throttle_state().count == 0


def test_floor_zero_auto_discrete_short_circuits_without_memory_reads(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    counts = {"config": 0, "detect": 0, "available": 0}

    def fake_config() -> dict[str, object]:
        counts["config"] += 1
        return {}

    def fake_detect() -> bool:
        counts["detect"] += 1
        return False

    def fake_available() -> int:
        counts["available"] += 1
        return 1 * GIB

    monkeypatch.setattr(admission, "get_config", fake_config)
    monkeypatch.setattr(admission.platform, "system", lambda: "Linux")
    monkeypatch.setattr(admission.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        admission.local_cuda, "detect_nvidia_unified_memory", fake_detect
    )
    monkeypatch.setattr(admission, "read_available_bytes", fake_available)
    caplog.set_level(logging.INFO, logger=admission.LOG.name)

    waits = [admission.wait_for_memory_headroom("describe") for _ in range(5)]

    assert waits == [0.0, 0.0, 0.0, 0.0, 0.0]
    assert counts == {"config": 1, "detect": 1, "available": 0}
    assert admission.throttle_state().count == 0
    assert not [
        record
        for record in caplog.records
        if "processing throttled" in record.getMessage()
    ]


def test_configured_zero_floor_skips_detection_and_memory_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts = {"config": 0, "detect": 0, "available": 0}

    def fake_config() -> dict[str, object]:
        counts["config"] += 1
        return _memory_config(0)

    def fake_detect() -> bool:
        counts["detect"] += 1
        return True

    def fake_available() -> int:
        counts["available"] += 1
        return 1 * GIB

    monkeypatch.setattr(admission, "get_config", fake_config)
    monkeypatch.setattr(
        admission.local_cuda, "detect_nvidia_unified_memory", fake_detect
    )
    monkeypatch.setattr(admission, "read_available_bytes", fake_available)

    waits = [admission.wait_for_memory_headroom("describe") for _ in range(5)]

    assert waits == [0.0, 0.0, 0.0, 0.0, 0.0]
    assert counts == {"config": 1, "detect": 0, "available": 0}
    assert admission.throttle_state().count == 0


def test_configured_floor_never_calls_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admission, "get_config", lambda: _memory_config(1234))

    def fail_detect() -> bool:
        raise AssertionError("explicit floor must not probe hardware")

    monkeypatch.setattr(
        admission.local_cuda,
        "detect_nvidia_unified_memory",
        fail_detect,
    )

    assert admission.resolve_memory_floor_bytes() == 1234 * MIB


def test_concurrent_first_admission_probes_hardware_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workers = 6
    barrier = threading.Barrier(workers)
    counts = {"config": 0, "detect": 0}
    counts_lock = threading.Lock()
    results: list[int | None] = [None] * workers
    errors: list[BaseException] = []

    def fake_config() -> dict[str, object]:
        with counts_lock:
            counts["config"] += 1
        return {}

    def fake_detect() -> bool:
        with counts_lock:
            counts["detect"] += 1
        time.sleep(0.05)
        return False

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=2)
            results[index] = admission.resolve_memory_floor_bytes()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(admission, "get_config", fake_config)
    monkeypatch.setattr(admission.platform, "system", lambda: "Linux")
    monkeypatch.setattr(admission.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        admission.local_cuda, "detect_nvidia_unified_memory", fake_detect
    )

    threads = [
        threading.Thread(target=worker, args=(index,), name=f"resolver-{index}")
        for index in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert counts == {"config": 1, "detect": 1}
    assert results == [0] * workers


@pytest.mark.parametrize("raw", ["4096", True, -1])
def test_invalid_floor_warns_and_falls_back_to_auto(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    raw: object,
) -> None:
    monkeypatch.setattr(admission, "get_config", lambda: _memory_config(raw))
    monkeypatch.setattr(admission.platform, "system", lambda: "Linux")
    monkeypatch.setattr(admission.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        admission.local_cuda,
        "detect_nvidia_unified_memory",
        lambda: True,
    )
    monkeypatch.setattr(admission, "stt_local_floor_bytes", lambda: 4 * GIB)
    monkeypatch.setattr(admission, "read_total_bytes", lambda: 16 * GIB)
    caplog.set_level(logging.WARNING, logger=admission.LOG.name)

    assert admission.resolve_memory_floor_bytes() == 5 * GIB
    assert any(
        "Invalid memory.floor_mib in journal config" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("system", "machine", "cuda_detected", "expected"),
    [
        ("Darwin", "arm64", None, True),
        ("Linux", "x86_64", True, True),
        ("Linux", "aarch64", False, False),
    ],
)
def test_detect_unified_memory_memoizes_platform_result(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    cuda_detected: bool | None,
    expected: bool,
) -> None:
    calls = {"cuda": 0}

    def fake_cuda_detect() -> bool:
        calls["cuda"] += 1
        assert cuda_detected is not None
        return cuda_detected

    monkeypatch.setattr(admission.platform, "system", lambda: system)
    monkeypatch.setattr(admission.platform, "machine", lambda: machine)
    monkeypatch.setattr(
        admission.local_cuda, "detect_nvidia_unified_memory", fake_cuda_detect
    )

    assert admission.detect_unified_memory() is expected
    assert admission.detect_unified_memory() is expected
    assert calls["cuda"] == (0 if cuda_detected is None else 1)


def test_default_unified_floor_at_or_above_stt_local_floor() -> None:
    floor = admission._default_floor_bytes()
    stt_floor = stt_local_floor_bytes()

    assert stt_floor is None or floor >= stt_floor


def test_stop_during_wait_decrements_and_fires_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = threading.Event()
    starts: list[dict[str, object]] = []
    ends: list[dict[str, object]] = []

    monkeypatch.setattr(admission, "get_config", lambda: _memory_config(5 * 1024))
    monkeypatch.setattr(admission, "read_available_bytes", lambda: 2 * GIB)

    def fake_sleep(_seconds: float) -> None:
        stopped.set()

    monkeypatch.setattr(admission.time, "sleep", fake_sleep)

    waited = admission.wait_for_memory_headroom(
        "describe",
        should_stop=stopped.is_set,
        on_throttle_start=lambda **fields: starts.append(fields),
        on_throttle_end=lambda **fields: ends.append(fields),
    )

    assert waited >= 0.0
    assert len(starts) == 1
    assert len(ends) == 1
    assert ends[0]["stage"] == "describe"
    assert admission.throttle_state().count == 0


def test_two_waiters_keep_throttle_state_until_both_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_first = threading.Event()
    release_second = threading.Event()
    starts: list[str] = []
    ends: list[str] = []
    events_lock = threading.Lock()

    monkeypatch.setattr(admission, "get_config", lambda: _memory_config(5 * 1024))
    monkeypatch.setattr(admission, "_POLL_INTERVAL_SECONDS", 0.01)

    def fake_available() -> int:
        name = threading.current_thread().name
        if name == "admission-waiter-1" and release_first.is_set():
            return 6 * GIB
        if name == "admission-waiter-2" and release_second.is_set():
            return 6 * GIB
        return 2 * GIB

    def on_start(**fields: object) -> None:
        with events_lock:
            starts.append(str(fields["stage"]))

    def on_end(**fields: object) -> None:
        with events_lock:
            ends.append(str(fields["stage"]))

    monkeypatch.setattr(admission, "read_available_bytes", fake_available)

    threads = [
        threading.Thread(
            target=admission.wait_for_memory_headroom,
            args=("describe",),
            kwargs={
                "on_throttle_start": on_start,
                "on_throttle_end": on_end,
            },
            name="admission-waiter-1",
        ),
        threading.Thread(
            target=admission.wait_for_memory_headroom,
            args=("transcribe",),
            kwargs={
                "on_throttle_start": on_start,
                "on_throttle_end": on_end,
            },
            name="admission-waiter-2",
        ),
    ]

    for thread in threads:
        thread.start()
    _wait_until(lambda: admission.throttle_state().count == 2)

    state = admission.throttle_state()
    assert state.throttled is True
    assert state.count == 2
    assert state.floor_mib == 5 * 1024
    assert state.available_mib == 2 * 1024

    release_first.set()
    _wait_until(lambda: not threads[0].is_alive())

    state = admission.throttle_state()
    assert state.throttled is True
    assert state.count == 1

    release_second.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert admission.throttle_state() == admission.ThrottleState(
        throttled=False,
        count=0,
        floor_mib=None,
        available_mib=None,
    )
    assert sorted(starts) == ["describe", "transcribe"]
    assert sorted(ends) == ["describe", "transcribe"]


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")
