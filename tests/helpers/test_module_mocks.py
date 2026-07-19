# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import threading
import time
from unittest.mock import Mock

from tests.helpers.module_mocks import (
    capturing_thread_constructor,
    inline_thread_constructor,
    module_mock,
)


def test_module_mock_overrides_are_local() -> None:
    real_time = time.time
    clock = Mock(return_value=123.0)

    mocked_time = module_mock(time, time=clock)

    assert mocked_time.time() == 123.0
    assert mocked_time.monotonic is time.monotonic
    assert time.time is real_time


def test_inline_thread_constructor_runs_target_without_real_thread() -> None:
    target = Mock()
    constructor = inline_thread_constructor()
    active_before = threading.active_count()

    thread = constructor(target=target, args=("value",), kwargs={"flag": True})
    thread.start()

    target.assert_called_once_with("value", flag=True)
    assert constructor.call_count == 1
    assert threading.active_count() == active_before


def test_capturing_thread_constructor_records_without_running() -> None:
    target = Mock()
    started: list[tuple[object, ...]] = []
    constructor = capturing_thread_constructor(
        started,
        capture=lambda thread: thread._args,
    )

    thread = constructor(target=target, args=("value",))
    thread.start()

    assert started == [("value",)]
    target.assert_not_called()
