# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Provider-aware sizing of the think fan-out defaults.

The CPU formulas overcommit the bundled local model server, which runs with one
(floor tier) or two (capable tier) parallel slots. When the work behind a
default can resolve to that server, the default is capped at its slot count.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from solstone.think import thinking as think
from solstone.think.providers import local_server

FLOOR_SLOTS = local_server._FLOOR_TIER.parallel_slots
CAPABLE_SLOTS = local_server._CAPABLE_TIER.parallel_slots


def _forbid_slot_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unreachable() -> int:
        raise AssertionError("slot discovery must not run for non-local defaults")

    monkeypatch.setattr(think, "read_server_parallel_slots", _unreachable)


def _pin_slots(monkeypatch: pytest.MonkeyPatch, slots: int) -> None:
    monkeypatch.setattr(think, "read_server_parallel_slots", lambda: slots)


def _pin_cpu_count(monkeypatch: pytest.MonkeyPatch, cpu_count: int) -> None:
    monkeypatch.setattr(think.os, "cpu_count", lambda: cpu_count)


def _write_journal_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, providers: dict
) -> None:
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True)
    (journal / "config" / "journal.json").write_text(
        json.dumps({"providers": providers})
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))


# --- AC1 -------------------------------------------------------------------


def test_default_segment_workers_local_floor_slots_returns_one(monkeypatch):
    _pin_cpu_count(monkeypatch, 12)
    monkeypatch.setattr(think, "_segment_work_uses_bundled_local", lambda: True)
    _pin_slots(monkeypatch, FLOOR_SLOTS)

    assert think._default_segment_workers() == FLOOR_SLOTS == 1


# --- AC2 -------------------------------------------------------------------


def test_default_segment_workers_local_capable_slots_respects_formula_min(monkeypatch):
    monkeypatch.setattr(think, "_segment_work_uses_bundled_local", lambda: True)
    _pin_slots(monkeypatch, CAPABLE_SLOTS)

    # Formula (8) exceeds slots (2): slots win.
    _pin_cpu_count(monkeypatch, 16)
    assert think._default_segment_workers() == CAPABLE_SLOTS == 2

    # Formula (1) is below slots (2): the formula wins. min() holds both ways.
    _pin_cpu_count(monkeypatch, 2)
    assert think._default_segment_workers() == 1


# --- AC3 -------------------------------------------------------------------


def test_default_segment_workers_nonlocal_cpu_formula(monkeypatch):
    _pin_cpu_count(monkeypatch, 12)
    monkeypatch.setattr(think, "_segment_work_uses_bundled_local", lambda: False)
    _forbid_slot_discovery(monkeypatch)

    # Distinct from the local cases' 1 and 2.
    assert think._default_segment_workers() == 6


# --- AC4 -------------------------------------------------------------------


def test_default_segment_workers_mixed_segment_context_local_caps(
    monkeypatch, tmp_path
):
    """A cloud default plus one segment talent pinned local still caps the fan-out.

    Resolution runs for real here — no monkeypatched predicate. Under
    default-provider-only semantics the default provider is google, so this
    would return the CPU formula (6) instead of the floor tier's 1. That
    difference is the point of the test.
    """
    _write_journal_config(
        monkeypatch,
        tmp_path,
        {
            "generate": {"provider": "google"},
            "contexts": {"talent.system.sense": {"provider": "local"}},
        },
    )
    _pin_cpu_count(monkeypatch, 12)
    _pin_slots(monkeypatch, FLOOR_SLOTS)

    assert think._default_segment_workers() == FLOOR_SLOTS == 1


# --- AC6 -------------------------------------------------------------------


def test_default_segment_workers_cap_logs_once_with_provider_slots_formula_derived(
    monkeypatch, caplog
):
    _pin_cpu_count(monkeypatch, 12)
    monkeypatch.setattr(think, "_segment_work_uses_bundled_local", lambda: True)
    _pin_slots(monkeypatch, FLOOR_SLOTS)

    caplog.set_level(logging.INFO)
    assert think._default_segment_workers() == 1
    assert think._default_segment_workers() == 1

    lines = [r.getMessage() for r in caplog.records if "capped" in r.getMessage()]
    assert lines == [
        "default_segment_workers capped provider=local slots=1 formula=6 derived=1"
    ]


def test_default_segment_workers_does_not_log_when_cap_changes_nothing(
    monkeypatch, caplog
):
    _pin_cpu_count(monkeypatch, 2)  # formula == 1 == slots
    monkeypatch.setattr(think, "_segment_work_uses_bundled_local", lambda: True)
    _pin_slots(monkeypatch, FLOOR_SLOTS)

    caplog.set_level(logging.INFO)
    assert think._default_segment_workers() == 1
    assert not [r for r in caplog.records if "capped" in r.getMessage()]


# --- AC7 -------------------------------------------------------------------


def test_default_describe_jobs_local_caps_and_nonlocal_uses_formula(monkeypatch):
    _pin_cpu_count(monkeypatch, 16)

    monkeypatch.setattr(think, "_describe_uses_bundled_local", lambda: False)
    _forbid_slot_discovery(monkeypatch)
    assert think._default_describe_jobs() == 4

    monkeypatch.setattr(think, "_describe_uses_bundled_local", lambda: True)
    _pin_slots(monkeypatch, FLOOR_SLOTS)
    # Distinct from the non-local 4.
    assert think._default_describe_jobs() == 1


def test_default_describe_jobs_capable_slots_cap(monkeypatch):
    _pin_cpu_count(monkeypatch, 16)
    monkeypatch.setattr(think, "_describe_uses_bundled_local", lambda: True)
    _pin_slots(monkeypatch, CAPABLE_SLOTS)

    assert think._default_describe_jobs() == CAPABLE_SLOTS == 2


def test_default_describe_jobs_cap_logs_provider_slots_formula_derived(
    monkeypatch, caplog
):
    _pin_cpu_count(monkeypatch, 16)
    monkeypatch.setattr(think, "_describe_uses_bundled_local", lambda: True)
    _pin_slots(monkeypatch, FLOOR_SLOTS)

    caplog.set_level(logging.INFO)
    assert think._default_describe_jobs() == 1
    assert think._default_describe_jobs() == 1

    lines = [r.getMessage() for r in caplog.records if "capped" in r.getMessage()]
    assert lines == [
        "default_describe_jobs capped provider=local slots=1 formula=4 derived=1"
    ]


# --- BYO endpoint (design P3, not an AC) -----------------------------------


def test_segment_default_byo_endpoint_uses_cpu_formula_and_does_not_probe(
    monkeypatch, tmp_path
):
    """A third-party OpenAI-compatible endpoint is not the bundled server."""
    _write_journal_config(
        monkeypatch,
        tmp_path,
        {
            "generate": {"provider": "local"},
            "local": {
                "endpoint_url": "https://example.invalid/v1",
                "served_model_id": "some-model",
            },
        },
    )
    _pin_cpu_count(monkeypatch, 12)
    _forbid_slot_discovery(monkeypatch)

    assert think.is_local_provider_needed() is True
    assert think._segment_work_uses_bundled_local() is False
    assert think._default_segment_workers() == 6
