# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for cadence-scheduled think dispatch."""

from __future__ import annotations

import importlib
import json
import time
from pathlib import Path

import pytest

from solstone.think.pipeline_health import CompletionsSince

DAY = "20990302"
NOW = 1_800_000_000_000


@pytest.fixture
def cadence_runtime(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    (journal / "health").mkdir(parents=True)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    mod = importlib.import_module("solstone.think.thinking")
    monkeypatch.setattr(mod, "emit", lambda *args, **kwargs: None)
    monkeypatch.setattr(mod, "_jsonl_log", lambda *args, **kwargs: None)
    return mod, journal


def _cadence_config(**extra) -> dict:
    config = {
        "type": "generate",
        "priority": 1,
        "output": "md",
        "schedule": "cadence",
    }
    config.update(extra)
    return config


def _window(
    *,
    segments: tuple[dict, ...] = (
        {"stream": "default", "segment": "090000_300", "ts": NOW - 120_000},
    ),
    activities: tuple[dict, ...] = (),
) -> CompletionsSince:
    return CompletionsSince(segments=segments, activities=activities)


def _write_cadence_state(journal: Path, state: dict[str, int]) -> None:
    path = journal / "health" / "cadence.json"
    path.write_text(json.dumps(state), encoding="utf-8")


def test_run_cadence_prompts_zero_talents_exits_without_state_write(
    cadence_runtime, monkeypatch
):
    mod, journal = cadence_runtime
    monkeypatch.setattr(mod, "get_talent_configs", lambda schedule: {})
    monkeypatch.setattr(
        mod,
        "_cortex_request_with_retry",
        lambda **kwargs: pytest.fail("cadence should not dispatch"),
    )

    assert mod.run_cadence_prompts(DAY, refresh=False, verbose=False) == (0, 0, [])
    assert not (journal / "health" / "cadence.json").exists()


def test_run_cadence_prompts_fires_when_window_has_new_work(
    cadence_runtime, monkeypatch
):
    mod, journal = cadence_runtime
    last = NOW - 600_000
    requests: list[dict] = []
    _write_cadence_state(journal, {"talentA": last})
    monkeypatch.setattr(
        mod,
        "get_talent_configs",
        lambda schedule: {"talentA": _cadence_config()},
    )
    monkeypatch.setattr(mod, "now_ms", lambda: NOW)

    def fake_completed_since(day: str, since_ms: int) -> CompletionsSince:
        assert day == DAY
        assert since_ms == last
        return _window()

    def fake_request(**kwargs):
        requests.append(kwargs)
        return "use-1"

    monkeypatch.setattr(mod, "read_completed_since", fake_completed_since)
    monkeypatch.setattr(mod, "_cortex_request_with_retry", fake_request)
    monkeypatch.setattr(
        mod,
        "_drain_priority_batch",
        lambda spawned, *_args: (1, 0, []),
    )

    assert mod.run_cadence_prompts(DAY, refresh=False, verbose=False) == (1, 0, [])

    assert len(requests) == 1
    request_config = requests[0]["config"]
    assert request_config["schedule"] == "cadence"
    assert request_config["cadence_window"]["since_ms"] == last
    assert request_config["cadence_window"]["segments"]
    assert mod.load_cadence_state()["talentA"] == NOW


def test_run_cadence_prompts_noops_without_new_work(cadence_runtime, monkeypatch):
    mod, journal = cadence_runtime
    last = NOW - 600_000
    save_calls: list[dict] = []
    _write_cadence_state(journal, {"talentA": last})
    monkeypatch.setattr(
        mod,
        "get_talent_configs",
        lambda schedule: {"talentA": _cadence_config()},
    )
    monkeypatch.setattr(mod, "now_ms", lambda: NOW)
    monkeypatch.setattr(
        mod,
        "read_completed_since",
        lambda day, since_ms: CompletionsSince((), ()),
    )
    monkeypatch.setattr(
        mod,
        "_cortex_request_with_retry",
        lambda **kwargs: pytest.fail("cadence should not dispatch"),
    )
    monkeypatch.setattr(
        mod, "save_cadence_state", lambda state: save_calls.append(state)
    )

    assert mod.run_cadence_prompts(DAY, refresh=False, verbose=False) == (0, 0, [])
    assert save_calls == []
    assert json.loads((journal / "health" / "cadence.json").read_text()) == {
        "talentA": last
    }


def test_run_cadence_prompts_respects_per_talent_interval(cadence_runtime, monkeypatch):
    mod, journal = cadence_runtime
    _write_cadence_state(journal, {"talentA": NOW - 600_000})
    monkeypatch.setattr(
        mod,
        "get_talent_configs",
        lambda schedule: {"talentA": _cadence_config(cadence_minutes=30)},
    )
    monkeypatch.setattr(mod, "now_ms", lambda: NOW)
    monkeypatch.setattr(
        mod,
        "read_completed_since",
        lambda day, since_ms: pytest.fail("interval gate should run first"),
    )
    monkeypatch.setattr(
        mod,
        "_cortex_request_with_retry",
        lambda **kwargs: pytest.fail("cadence should not dispatch"),
    )

    assert mod.run_cadence_prompts(DAY, refresh=False, verbose=False) == (0, 0, [])


def test_run_cadence_prompts_writes_back_only_on_success(cadence_runtime, monkeypatch):
    mod, journal = cadence_runtime
    last = NOW - 600_000
    save_calls: list[dict] = []
    _write_cadence_state(journal, {"talentA": last})
    monkeypatch.setattr(
        mod,
        "get_talent_configs",
        lambda schedule: {"talentA": _cadence_config()},
    )
    monkeypatch.setattr(mod, "now_ms", lambda: NOW)
    monkeypatch.setattr(mod, "read_completed_since", lambda day, since_ms: _window())
    monkeypatch.setattr(mod, "_cortex_request_with_retry", lambda **kwargs: "use-fail")
    monkeypatch.setattr(
        mod,
        "_drain_priority_batch",
        lambda spawned, *_args: (0, 1, ["talentA (error)"]),
    )
    monkeypatch.setattr(
        mod, "save_cadence_state", lambda state: save_calls.append(state)
    )

    assert mod.run_cadence_prompts(DAY, refresh=False, verbose=False) == (
        0,
        1,
        ["talentA (error)"],
    )
    assert save_calls == []
    assert json.loads((journal / "health" / "cadence.json").read_text()) == {
        "talentA": last
    }


def test_run_cadence_prompts_missing_state_treats_talent_as_never_run(
    cadence_runtime, monkeypatch
):
    mod, journal = cadence_runtime
    requests: list[dict] = []
    monkeypatch.setattr(
        mod,
        "get_talent_configs",
        lambda schedule: {"talentA": _cadence_config()},
    )
    monkeypatch.setattr(mod, "now_ms", lambda: NOW)
    monkeypatch.setattr(mod, "read_completed_since", lambda day, since_ms: _window())
    monkeypatch.setattr(
        mod,
        "_cortex_request_with_retry",
        lambda **kwargs: requests.append(kwargs) or "use-1",
    )
    monkeypatch.setattr(
        mod,
        "_drain_priority_batch",
        lambda spawned, *_args: (1, 0, []),
    )

    assert mod.run_cadence_prompts(DAY, refresh=False, verbose=False) == (1, 0, [])
    assert requests[0]["config"]["cadence_window"]["since_ms"] == 0
    assert mod.load_cadence_state()["talentA"] == NOW
    assert (journal / "health" / "cadence.json").exists()


def test_run_cadence_prompts_writeback_uses_window_read_time(
    cadence_runtime, monkeypatch
):
    mod, _journal = cadence_runtime
    calls = 0
    monkeypatch.setattr(
        mod,
        "get_talent_configs",
        lambda schedule: {"talentA": _cadence_config()},
    )

    def fake_now_ms() -> int:
        nonlocal calls
        calls += 1
        return NOW if calls == 1 else NOW + 999_999

    def fake_drain(spawned, *_args):
        assert mod.now_ms() > NOW
        assert time.time() > 0
        return (1, 0, [])

    monkeypatch.setattr(mod, "now_ms", fake_now_ms)
    monkeypatch.setattr(mod, "read_completed_since", lambda day, since_ms: _window())
    monkeypatch.setattr(mod, "_cortex_request_with_retry", lambda **kwargs: "use-1")
    monkeypatch.setattr(mod, "_drain_priority_batch", fake_drain)

    assert mod.run_cadence_prompts(DAY, refresh=False, verbose=False) == (1, 0, [])
    assert mod.load_cadence_state()["talentA"] == NOW
