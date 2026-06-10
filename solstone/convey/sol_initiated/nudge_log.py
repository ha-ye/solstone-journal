# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Nudge-log writer for sol-initiated chat attempts."""

from __future__ import annotations

import time


def record_nudge_log(
    kind: str,
    dedupe_key: str,
    category: str,
    outcome: str,
) -> None:
    """Append one sol-initiated row.

    Older rows written by push send accounting do not include ``kind``. This
    writer leaves those rows unchanged.
    """
    # Imported lazily to break a module-load circular import: push.triggers
    # imports sol_initiated.copy (-> sol_initiated.__init__ -> start ->
    # nudge_log), so a top-level `from ...triggers import _append_nudge_log`
    # closes the cycle while triggers is mid-initialization. Deferring to call
    # time (both modules fully loaded) makes the import order-independent.
    from solstone.think.push.triggers import _append_nudge_log

    _append_nudge_log(
        {
            "ts": int(time.time()),
            "kind": kind,
            "dedupe_key": dedupe_key,
            "category": category,
            "outcome": outcome,
        }
    )
