# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared logging isolation helpers for tests that import chatty SDKs."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from solstone.log_policy import apply_http_logging_policy, snapshot_root_logging


@contextmanager
def preserve_global_logging() -> Iterator[None]:
    root_baseline = snapshot_root_logging()
    named = ("httpx", "solstone.observe.utils")
    saved = {
        name: (logger.level, logger.disabled, logger.propagate)
        for name in named
        for logger in [logging.getLogger(name)]
    }
    try:
        yield
    finally:
        for name, (level, disabled, propagate) in saved.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            logger.disabled = disabled
            logger.propagate = propagate
        apply_http_logging_policy(root_baseline)
