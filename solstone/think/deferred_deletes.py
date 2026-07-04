# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Process-local timer registry for deferred destructive actions.

Deferred deletes are best-effort and process-lifetime only. The commit runs on
a daemon ``threading.Timer`` after a TTL cancel window; if the process exits
within that window, the commit is silently dropped even though the HTTP response
already told the client the delete succeeded by returning
``pending``/``commit_at_ms``/``ttl_seconds``.

This mechanism is for pure deletes only. It must never finalize a replacement or
anything whose dropped commit is not data-safe. A dropped pure delete just means
the delete did not happen; a dropped replacement finalization could corrupt or
lose data.

Dropped commits are noticeable after the fact only as a forensic signature in
the action log at ``config/actions/<day>.jsonl``. The caller writes a
``phase:"pending"`` record with ``pending_id`` immediately; ``_commit`` writes
``phase:"committed"`` only when it runs; cancel writes ``phase:"cancelled"``.
A dropped commit leaves a pending-phase record whose ``pending_id`` has no later
committed or cancelled record.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable

logger = logging.getLogger(__name__)

_TIMERS: dict[str, threading.Timer] = {}
_LOCK = threading.Lock()


def schedule_with_id(
    pending_id: str,
    commit_fn: Callable[[], None],
    ttl_seconds: float = 10.0,
) -> str:
    """Schedule ``commit_fn`` to run after ``ttl_seconds`` using ``pending_id``."""

    def _fire(fire_pending_id: str) -> None:
        with _LOCK:
            timer = _TIMERS.pop(fire_pending_id, None)
        if timer is None:
            return

        try:
            commit_fn()
        except Exception:
            logger.exception("Deferred delete commit failed for %s", fire_pending_id)

    timer = threading.Timer(ttl_seconds, _fire, args=(pending_id,))
    timer.daemon = True
    with _LOCK:
        _TIMERS[pending_id] = timer
    timer.start()
    return pending_id


def schedule(commit_fn: Callable[[], None], ttl_seconds: float = 10.0) -> str:
    """Schedule ``commit_fn`` using a generated pending id."""

    return schedule_with_id(uuid.uuid4().hex, commit_fn, ttl_seconds=ttl_seconds)


def cancel(pending_id: str) -> bool:
    """Cancel a pending deferred delete if it still exists."""

    with _LOCK:
        timer = _TIMERS.pop(pending_id, None)
    if timer is None:
        return False

    timer.cancel()
    return True
