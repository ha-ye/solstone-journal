# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.think.spl.admission import BlobAdmissionGate


def test_global_ceiling_rejects_without_waiting_and_releases_capacity() -> None:
    gate = BlobAdmissionGate(global_ceiling=2, sender_ceiling=4)

    assert gate.try_acquire_global() is True
    assert gate.try_acquire_global() is True
    assert gate.try_acquire_global() is False
    assert gate.global_count == 2
    assert gate.saturated_count == 1

    gate.release_global()

    assert gate.global_count == 1
    assert gate.try_acquire_global() is True
    assert gate.global_count == 2
    assert gate.saturated_count == 1


def test_per_sender_ceiling_is_keyed_by_fingerprint() -> None:
    gate = BlobAdmissionGate(global_ceiling=10, sender_ceiling=1)

    assert gate.try_acquire_sender("sender-a") is True
    assert gate.try_acquire_sender("sender-a") is False
    assert gate.try_acquire_sender("sender-b") is True
    assert gate.sender_count("sender-a") == 1
    assert gate.sender_count("sender-b") == 1
    assert gate.saturated_count == 1

    gate.release_sender("sender-a")

    assert gate.try_acquire_sender("sender-a") is True
    assert gate.sender_count("sender-a") == 1


def test_sender_keys_are_pruned_at_zero() -> None:
    gate = BlobAdmissionGate(global_ceiling=10, sender_ceiling=2)

    assert gate.try_acquire_sender("sender-a") is True
    assert gate.try_acquire_sender("sender-a") is True
    assert gate.active_senders() == 1

    gate.release_sender("sender-a")
    assert gate.sender_count("sender-a") == 1
    assert gate.active_senders() == 1

    gate.release_sender("sender-a")
    assert gate.sender_count("sender-a") == 0
    assert gate.active_senders() == 0


def test_saturated_count_is_monotonic_across_global_and_sender_rejections() -> None:
    gate = BlobAdmissionGate(global_ceiling=0, sender_ceiling=0)

    assert gate.try_acquire_global() is False
    assert gate.try_acquire_sender("sender-a") is False
    assert gate.try_acquire_global() is False

    assert gate.saturated_count == 3
