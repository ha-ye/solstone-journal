# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from solstone.log_policy import apply_http_logging_policy, snapshot_root_logging
from tests._logging_isolation import preserve_global_logging


def test_apply_sets_httpx_warning_baseline_less():
    logging.getLogger("httpx").setLevel(logging.INFO)

    apply_http_logging_policy()

    assert logging.getLogger("httpx").level == logging.WARNING


def test_apply_reconciles_root_against_snapshot():
    root = logging.getLogger()
    baseline_level, baseline_handlers = snapshot_root_logging()
    added = logging.NullHandler()

    try:
        root.setLevel(logging.INFO)
        root.addHandler(added)

        apply_http_logging_policy((baseline_level, baseline_handlers))

        assert root.level == baseline_level
        assert all(handler is not added for handler in root.handlers)
        for baseline_handler in baseline_handlers:
            assert any(handler is baseline_handler for handler in root.handlers)
    finally:
        if any(handler is added for handler in root.handlers):
            root.removeHandler(added)
        root.setLevel(baseline_level)
        for baseline_handler in baseline_handlers:
            if all(handler is not baseline_handler for handler in root.handlers):
                root.addHandler(baseline_handler)


def test_apply_restores_httpx_after_real_sdk_import(monkeypatch):
    monkeypatch.setenv("OPENHANDS_SUPPRESS_BANNER", "1")

    with preserve_global_logging():
        import openhands.sdk  # noqa: F401

        logging.getLogger("httpx").setLevel(logging.INFO)

        apply_http_logging_policy()

        assert logging.getLogger("httpx").level == logging.WARNING


def test_run_cogitate_invokes_restore(monkeypatch):
    from solstone.think.providers import openhands

    events: list[dict[str, Any]] = []
    calls: list[tuple[int, tuple[logging.Handler, ...]] | None] = []
    real_apply_http_logging_policy = apply_http_logging_policy

    def fail_build_llm(
        provider: str, model: str, *, num_retries: int | None = None
    ) -> Any:
        raise RuntimeError("sentinel")

    def apply_spy(
        root_baseline: tuple[int, tuple[logging.Handler, ...]] | None = None,
    ) -> None:
        calls.append(root_baseline)
        real_apply_http_logging_policy(root_baseline)

    monkeypatch.setenv("OPENHANDS_SUPPRESS_BANNER", "1")
    monkeypatch.setattr(openhands, "_build_llm", fail_build_llm)
    monkeypatch.setattr(openhands, "apply_http_logging_policy", apply_spy)

    with preserve_global_logging():
        logging.getLogger("httpx").setLevel(logging.INFO)

        with pytest.raises(RuntimeError, match="sentinel"):
            asyncio.run(
                openhands.run_cogitate(
                    {"provider": "google", "model": "gemini-test"},
                    on_event=events.append,
                )
            )

        assert calls
        assert any(event.get("event") == "error" for event in events)
        assert logging.getLogger("httpx").level == logging.WARNING
