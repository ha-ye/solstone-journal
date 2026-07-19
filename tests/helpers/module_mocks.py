# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Helpers for replacing product-local stdlib module aliases with mocks."""

from __future__ import annotations

from types import ModuleType
from typing import Any, Callable
from unittest.mock import Mock


def module_mock(module: ModuleType, /, **overrides: Any) -> Mock:
    """Create a shallow module mock with real defaults and local overrides."""
    mocked = Mock(spec=module)
    for name in dir(module):
        if not name.startswith("__"):
            setattr(mocked, name, getattr(module, name))
    for name, value in overrides.items():
        setattr(mocked, name, value)
    return mocked


def inline_thread_constructor() -> Mock:
    """Return a mocked Thread constructor whose ``start`` runs inline."""
    constructor = Mock(name="Thread")

    def build(*args: Any, **kwargs: Any) -> Mock:
        target = kwargs.get("target", args[0] if args else None)
        target_args = kwargs.get("args", ())
        target_kwargs = kwargs.get("kwargs", {})
        thread = Mock(name="thread")
        thread._target = target
        thread._args = target_args
        thread._kwargs = target_kwargs
        thread.is_alive.return_value = False
        thread.start.side_effect = lambda: target(*target_args, **target_kwargs)
        return thread

    constructor.side_effect = build
    return constructor


def capturing_thread_constructor(
    started: list[Any],
    *,
    capture: Callable[[Mock], Any] = lambda thread: thread,
) -> Mock:
    """Return a mocked Thread constructor that records starts without running."""
    constructor = Mock(name="Thread")

    def build(*args: Any, **kwargs: Any) -> Mock:
        target = kwargs.get("target", args[0] if args else None)
        thread = Mock(name="thread")
        thread._target = target
        thread._args = kwargs.get("args", ())
        thread._kwargs = kwargs.get("kwargs", {})
        thread.is_alive.return_value = False
        thread.start.side_effect = lambda: started.append(capture(thread))
        return thread

    constructor.side_effect = build
    return constructor
