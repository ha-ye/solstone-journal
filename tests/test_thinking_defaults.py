# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Provider-aware sizing of the think fan-out defaults.

The CPU formulas overcommit governed local lanes. When the work behind a
default can resolve to such a lane, the default is capped at its slot count.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from solstone.think import thinking as think
from solstone.think.providers import fanout_policy, local_server

FLOOR_SLOTS = local_server._FLOOR_TIER.parallel_slots
CAPABLE_SLOTS = local_server._CAPABLE_TIER.parallel_slots


def _forbid_slot_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unreachable() -> int:
        raise AssertionError("slot discovery must not run for non-local defaults")

    monkeypatch.setattr(fanout_policy, "read_server_parallel_slots", _unreachable)


def _pin_slots(monkeypatch: pytest.MonkeyPatch, slots: int) -> None:
    monkeypatch.setattr(fanout_policy, "read_server_parallel_slots", lambda: slots)


def _pin_cpu_count(monkeypatch: pytest.MonkeyPatch, cpu_count: int) -> None:
    monkeypatch.setattr(fanout_policy.os, "cpu_count", lambda: cpu_count)


def _write_journal_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    providers: dict,
    *,
    services: dict | None = None,
) -> None:
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True)
    config = {"providers": providers}
    if services is not None:
        config["services"] = services
    (journal / "config" / "journal.json").write_text(json.dumps(config))
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))


# --- AC1 -------------------------------------------------------------------


def test_default_segment_workers_local_floor_slots_returns_one(monkeypatch):
    _pin_cpu_count(monkeypatch, 12)
    monkeypatch.setattr(fanout_policy, "_segment_work_uses_local", lambda: True)
    _pin_slots(monkeypatch, FLOOR_SLOTS)

    assert fanout_policy.default_segment_workers() == FLOOR_SLOTS == 1


# --- AC2 -------------------------------------------------------------------


def test_default_segment_workers_local_capable_slots_respects_formula_min(monkeypatch):
    monkeypatch.setattr(fanout_policy, "_segment_work_uses_local", lambda: True)
    _pin_slots(monkeypatch, CAPABLE_SLOTS)

    # Formula (8) exceeds slots (2): slots win.
    _pin_cpu_count(monkeypatch, 16)
    assert fanout_policy.default_segment_workers() == CAPABLE_SLOTS == 2

    # Formula (1) is below slots (2): the formula wins. min() holds both ways.
    _pin_cpu_count(monkeypatch, 2)
    assert fanout_policy.default_segment_workers() == 1


# --- AC3 -------------------------------------------------------------------


def test_default_segment_workers_nonlocal_cpu_formula(monkeypatch):
    _pin_cpu_count(monkeypatch, 12)
    monkeypatch.setattr(fanout_policy, "_segment_work_uses_local", lambda: False)
    _forbid_slot_discovery(monkeypatch)

    # Distinct from the local cases' 1 and 2.
    assert fanout_policy.default_segment_workers() == 6


# --- AC4 -------------------------------------------------------------------


def test_default_segment_workers_ignores_context_local_pin(monkeypatch, tmp_path):
    """A retired context-local pin does not cap segment fan-out."""
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

    assert fanout_policy.default_segment_workers() == 6


# --- AC6 -------------------------------------------------------------------


def test_default_segment_workers_cap_logs_once_with_provider_slots_formula_derived(
    monkeypatch, caplog
):
    _pin_cpu_count(monkeypatch, 12)
    monkeypatch.setattr(fanout_policy, "_segment_work_uses_local", lambda: True)
    _pin_slots(monkeypatch, FLOOR_SLOTS)

    caplog.set_level(logging.INFO)
    assert fanout_policy.default_segment_workers() == 1
    assert fanout_policy.default_segment_workers() == 1

    lines = [r.getMessage() for r in caplog.records if "capped" in r.getMessage()]
    assert lines == [
        "default_segment_workers capped provider=local slots=1 formula=6 derived=1"
    ]


def test_default_segment_workers_does_not_log_when_cap_changes_nothing(
    monkeypatch, caplog
):
    _pin_cpu_count(monkeypatch, 2)  # formula == 1 == slots
    monkeypatch.setattr(fanout_policy, "_segment_work_uses_local", lambda: True)
    _pin_slots(monkeypatch, FLOOR_SLOTS)

    caplog.set_level(logging.INFO)
    assert fanout_policy.default_segment_workers() == 1
    assert not [r for r in caplog.records if "capped" in r.getMessage()]


# --- AC7 -------------------------------------------------------------------


def test_default_describe_jobs_local_caps_and_nonlocal_uses_formula(monkeypatch):
    _pin_cpu_count(monkeypatch, 16)

    monkeypatch.setattr(fanout_policy, "_describe_uses_local", lambda: False)
    _forbid_slot_discovery(monkeypatch)
    assert fanout_policy.default_describe_jobs() == 4

    monkeypatch.setattr(fanout_policy, "_describe_uses_local", lambda: True)
    _pin_slots(monkeypatch, FLOOR_SLOTS)
    # Distinct from the non-local 4.
    assert fanout_policy.default_describe_jobs() == 1


def test_default_describe_jobs_capable_slots_cap(monkeypatch):
    _pin_cpu_count(monkeypatch, 16)
    monkeypatch.setattr(fanout_policy, "_describe_uses_local", lambda: True)
    _pin_slots(monkeypatch, CAPABLE_SLOTS)

    assert fanout_policy.default_describe_jobs() == CAPABLE_SLOTS == 2


def test_default_describe_jobs_cap_logs_provider_slots_formula_derived(
    monkeypatch, caplog
):
    _pin_cpu_count(monkeypatch, 16)
    monkeypatch.setattr(fanout_policy, "_describe_uses_local", lambda: True)
    _pin_slots(monkeypatch, FLOOR_SLOTS)

    caplog.set_level(logging.INFO)
    assert fanout_policy.default_describe_jobs() == 1
    assert fanout_policy.default_describe_jobs() == 1

    lines = [r.getMessage() for r in caplog.records if "capped" in r.getMessage()]
    assert lines == [
        "default_describe_jobs capped provider=local slots=1 formula=4 derived=1"
    ]


# --- BYO endpoint (design P3, not an AC) -----------------------------------


def test_segment_default_byo_endpoint_uses_configured_slot_cap_and_does_not_probe(
    monkeypatch, tmp_path
):
    """A governed third-party endpoint caps without bundled slot discovery."""
    _write_journal_config(
        monkeypatch,
        tmp_path,
        {
            "active": {"provider": "local", "model": "local/qwen3.5-4b"},
            "local": {
                "endpoint_url": "https://example.invalid/v1",
                "served_model_id": "some-model",
                "parallel_slots": 3,
            },
        },
    )
    _pin_cpu_count(monkeypatch, 12)
    _forbid_slot_discovery(monkeypatch)

    assert think.is_local_provider_needed() is True
    assert fanout_policy._segment_work_uses_local() is True
    assert fanout_policy.default_segment_workers() == 3


def test_segment_default_confidential_endpoint_uses_cpu_formula_and_does_not_probe(
    monkeypatch, tmp_path
):
    """Confidential BYO endpoints are ungoverned even with a stray slot key."""
    _write_journal_config(
        monkeypatch,
        tmp_path,
        {
            "active": {"provider": "local", "model": "local/qwen3.5-4b"},
            "local": {
                "endpoint_url": "https://example.invalid/v1",
                "served_model_id": "some-model",
                "parallel_slots": 1,
            },
        },
        services={"confidential": {"account_id": "acct"}},
    )
    _pin_cpu_count(monkeypatch, 12)
    _forbid_slot_discovery(monkeypatch)

    assert think.is_local_provider_needed() is True
    assert fanout_policy.default_segment_workers() == 6


def test_describe_default_byo_endpoint_uses_configured_slot_cap_and_does_not_probe(
    monkeypatch, tmp_path
):
    _write_journal_config(
        monkeypatch,
        tmp_path,
        {
            "active": {"provider": "local", "model": "local/qwen3.5-4b"},
            "local": {
                "endpoint_url": "https://example.invalid/v1",
                "served_model_id": "some-model",
                "parallel_slots": 2,
            },
        },
    )
    _pin_cpu_count(monkeypatch, 16)
    _forbid_slot_discovery(monkeypatch)

    assert fanout_policy._describe_uses_local() is True
    assert fanout_policy.default_describe_jobs() == 2


def test_describe_default_confidential_endpoint_uses_cpu_formula_and_does_not_probe(
    monkeypatch, tmp_path
):
    _write_journal_config(
        monkeypatch,
        tmp_path,
        {
            "active": {"provider": "local", "model": "local/qwen3.5-4b"},
            "local": {
                "endpoint_url": "https://example.invalid/v1",
                "served_model_id": "some-model",
                "parallel_slots": 1,
            },
        },
        services={"confidential": {"account_id": "acct"}},
    )
    _pin_cpu_count(monkeypatch, 16)
    _forbid_slot_discovery(monkeypatch)

    assert fanout_policy._describe_uses_local() is True
    assert fanout_policy.default_describe_jobs() == 4


@pytest.mark.parametrize(
    ("mode", "configured", "args_jobs", "effective_procs"),
    [
        ("watch", 1, None, 1),
        ("watch", 4, None, 4),
        ("day", 1, 1, 1),
        ("day", 1, 2, 2),
        ("day", 4, 1, 4),
        ("day", 4, 2, 4),
    ],
)
@pytest.mark.parametrize("slots", [1, 2, 4])
def test_describe_per_proc_product_matrix_and_residual_log_once(
    monkeypatch, caplog, mode, configured, args_jobs, effective_procs, slots
):
    del mode, configured, args_jobs
    monkeypatch.setattr(fanout_policy, "_describe_uses_local", lambda: True)
    _pin_slots(monkeypatch, slots)
    caplog.set_level(logging.INFO)

    per_proc = fanout_policy.describe_per_proc_jobs(effective_procs)
    repeat = fanout_policy.describe_per_proc_jobs(effective_procs)

    assert repeat == per_proc
    product = effective_procs * per_proc
    assert product <= max(2 * slots, effective_procs)

    residual_lines = [
        record.getMessage()
        for record in caplog.records
        if "describe_per_proc_jobs residual" in record.getMessage()
    ]
    if effective_procs == 4 and slots == 1:
        assert residual_lines == [
            "describe_per_proc_jobs residual provider=local slots=1 "
            "effective_procs=4 per_proc=1 product=4 limit=2"
        ]
    else:
        assert residual_lines == []


def test_describe_per_proc_bundled_floor_slots_direct_call_returns_two(monkeypatch):
    monkeypatch.setattr(fanout_policy, "_describe_uses_local", lambda: True)
    _pin_slots(monkeypatch, FLOOR_SLOTS)

    assert fanout_policy.describe_per_proc_jobs(1) == 2


def test_describe_per_proc_nonlocal_and_confidential_byo_keep_historical_default(
    monkeypatch,
):
    monkeypatch.setattr(fanout_policy, "_describe_uses_local", lambda: False)
    _forbid_slot_discovery(monkeypatch)
    assert fanout_policy.describe_per_proc_jobs(1) == 10

    monkeypatch.setattr(fanout_policy, "_describe_uses_local", lambda: True)
    monkeypatch.setattr(fanout_policy, "_local_fanout_slots", lambda: None)
    assert fanout_policy.describe_per_proc_jobs(4) == 10
