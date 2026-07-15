# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Non-blocking in-memory admission gates for SPL blob receives."""

from __future__ import annotations

_GLOBAL_ADMISSION_CEILING = 32
_SENDER_ADMISSION_CEILING = 4


class BlobAdmissionGate:
    def __init__(self, *, global_ceiling: int, sender_ceiling: int) -> None:
        self._global_ceiling = global_ceiling
        self._sender_ceiling = sender_ceiling
        self._global_count = 0
        self._sender_counts: dict[str, int] = {}
        self._saturated_count = 0

    @property
    def global_count(self) -> int:
        return self._global_count

    @property
    def saturated_count(self) -> int:
        return self._saturated_count

    def sender_count(self, fp: str) -> int:
        return self._sender_counts.get(fp, 0)

    def active_senders(self) -> int:
        return len(self._sender_counts)

    def try_acquire_global(self) -> bool:
        if self._global_count < self._global_ceiling:
            self._global_count += 1
            return True
        self._saturated_count += 1
        return False

    def release_global(self) -> None:
        self._global_count = max(0, self._global_count - 1)

    def try_acquire_sender(self, fp: str) -> bool:
        count = self._sender_counts.get(fp, 0)
        if count < self._sender_ceiling:
            self._sender_counts[fp] = count + 1
            return True
        self._saturated_count += 1
        return False

    def release_sender(self, fp: str) -> None:
        count = self._sender_counts.get(fp, 0)
        if count <= 1:
            self._sender_counts.pop(fp, None)
            return
        self._sender_counts[fp] = count - 1
