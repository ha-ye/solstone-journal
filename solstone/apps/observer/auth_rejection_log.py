# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""In-memory rate-limited logging for observer auth rejections."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace

from solstone.convey.reasons import Reason
from solstone.think.utils import now_ms

AUTH_REJECTION_WINDOW_SECONDS = 300
AUTH_REJECTION_QUIET_CLOSE_SECONDS = 300
AUTH_REJECTION_TRACKED_PREFIX_CAP = 64
KEYLESS_AUTH_REQUIRED_PREFIX = "keyless_auth_required"

_WINDOW_MS = AUTH_REJECTION_WINDOW_SECONDS * 1000
_QUIET_CLOSE_MS = AUTH_REJECTION_QUIET_CLOSE_SECONDS * 1000

logger = logging.getLogger(__name__)


@dataclass
class _AuthRejectionEntry:
    surface: str
    reason: Reason
    key_prefix: str
    first_ts: int
    latest_ts: int
    active_count: int
    last_warned_ts: int


_LOCK = threading.Lock()
_ENTRIES: dict[str, _AuthRejectionEntry] = {}


def _bucket_key(attempted_prefix: str | None) -> str:
    return attempted_prefix or KEYLESS_AUTH_REQUIRED_PREFIX


def _copy_entry(entry: _AuthRejectionEntry) -> _AuthRejectionEntry:
    return replace(entry)


def _emit_warning(entry: _AuthRejectionEntry) -> None:
    logger.warning(
        "observer_auth_rejection surface=%s reason_code=%s key_prefix=%s "
        "first_ts=%s latest_ts=%s",
        entry.surface,
        entry.reason.code,
        entry.key_prefix,
        entry.first_ts,
        entry.latest_ts,
    )


def _emit_close(entry: _AuthRejectionEntry) -> None:
    logger.error(
        "observer_auth_rejection_burst_closed surface=%s reason_code=%s "
        "key_prefix=%s first_ts=%s latest_ts=%s active_count=%s",
        entry.surface,
        entry.reason.code,
        entry.key_prefix,
        entry.first_ts,
        entry.latest_ts,
        entry.active_count,
    )


def sweep_auth_rejection_bursts() -> None:
    """Close quiet bursts and emit one aggregate error per non-empty burst."""
    current_ts = now_ms()
    expired: list[_AuthRejectionEntry] = []
    with _LOCK:
        for bucket, entry in list(_ENTRIES.items()):
            if current_ts - entry.latest_ts >= _QUIET_CLOSE_MS:
                expired.append(_copy_entry(entry))
                del _ENTRIES[bucket]

    for entry in expired:
        _emit_close(entry)


def record_auth_rejection(
    *,
    surface: str,
    reason: Reason,
    attempted_prefix: str | None,
) -> None:
    """Account for one in-scope observer auth rejection."""
    bucket = _bucket_key(attempted_prefix)
    warning_entry: _AuthRejectionEntry | None = None

    with _LOCK:
        entry = _ENTRIES.get(bucket)
        if entry is None:
            if len(_ENTRIES) >= AUTH_REJECTION_TRACKED_PREFIX_CAP:
                return
            current_ts = now_ms()
            entry = _AuthRejectionEntry(
                surface=surface,
                reason=reason,
                key_prefix=bucket,
                first_ts=current_ts,
                latest_ts=current_ts,
                active_count=1,
                last_warned_ts=current_ts,
            )
            _ENTRIES[bucket] = entry
            warning_entry = _copy_entry(entry)
        else:
            active_count = entry.active_count
            current_ts = now_ms()
            entry.active_count = active_count + 1
            entry.latest_ts = current_ts
            entry.reason = reason
            if current_ts - entry.last_warned_ts >= _WINDOW_MS:
                entry.last_warned_ts = current_ts
                warning_entry = _copy_entry(entry)

    if warning_entry is not None:
        _emit_warning(warning_entry)


def reset_auth_rejection_state_for_tests() -> None:
    """Reset module-level auth rejection accounting state."""
    with _LOCK:
        _ENTRIES.clear()


def tracked_auth_rejection_bucket_count_for_tests() -> int:
    """Return the number of currently tracked auth rejection buckets."""
    with _LOCK:
        return len(_ENTRIES)
