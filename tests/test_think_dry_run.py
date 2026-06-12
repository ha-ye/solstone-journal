# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for think --dry-run."""

import importlib

from solstone.think.pipeline_health import CompletionsSince


def test_dry_run_daily(journal_copy, capsys):
    """Dry-run daily mode prints prompts without spawning agents."""
    mod = importlib.import_module("solstone.think.thinking")

    mod.dry_run("20240101")

    out = capsys.readouterr().out
    assert "2024-01-01" in out
    assert "Pre-phase" in out
    assert "Post-phase" in out
    assert "Priority" in out
    assert "Total:" in out


def test_dry_run_segment(journal_copy, capsys):
    """Dry-run segment mode skips pre/post phases."""
    mod = importlib.import_module("solstone.think.thinking")

    mod.dry_run("20240101", segment="120000_300")

    out = capsys.readouterr().out
    assert "segment 120000_300" in out
    assert "Sense orchestrator" in out
    assert "Pre-phase" not in out
    assert "Post-phase" not in out


def test_dry_run_segments_lists_all(journal_copy, capsys):
    """Dry-run --segments lists discovered segments."""
    mod = importlib.import_module("solstone.think.thinking")

    mod.dry_run("20240101", segments=True)

    out = capsys.readouterr().out
    assert "segments" in out.lower()


def test_dry_run_flush(journal_copy, capsys):
    """Dry-run --flush shows flush-eligible agents."""
    mod = importlib.import_module("solstone.think.thinking")

    mod.dry_run("20240101", flush=True, segment="120000_300")

    out = capsys.readouterr().out
    assert "flush" in out.lower()


def test_dry_run_shows_refresh(journal_copy, capsys):
    """Dry-run indicates refresh mode in header."""
    mod = importlib.import_module("solstone.think.thinking")

    mod.dry_run("20240101", refresh=True)

    out = capsys.readouterr().out
    assert "(refresh)" in out


def test_dry_run_no_callosum(journal_copy, monkeypatch, capsys):
    """Dry-run works without callosum connection."""
    mod = importlib.import_module("solstone.think.thinking")

    # Save and clear _callosum to verify dry_run doesn't create one
    prev = mod._callosum
    monkeypatch.setattr(mod, "_callosum", None)
    mod.dry_run("20240101")
    assert mod._callosum is None
    monkeypatch.setattr(mod, "_callosum", prev)


def test_dry_run_cadence_reports_gate_decisions(tmp_path, monkeypatch, capsys):
    """Dry-run cadence mode reports fire/no-op/skip decisions without writes."""
    mod = importlib.import_module("solstone.think.thinking")
    journal = tmp_path / "journal"
    (journal / "health").mkdir(parents=True)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    now = 1_800_000_000_000
    state = {
        "fire": now - 600_000,
        "no_work": now - 700_000,
        "wait": now - 60_000,
    }

    monkeypatch.setattr(
        mod,
        "get_talent_configs",
        lambda schedule: {
            "fire": {"type": "generate", "priority": 1, "schedule": "cadence"},
            "no_work": {"type": "generate", "priority": 2, "schedule": "cadence"},
            "wait": {
                "type": "generate",
                "priority": 3,
                "schedule": "cadence",
                "cadence_minutes": 5,
            },
        },
    )
    monkeypatch.setattr(mod, "load_cadence_state", lambda: state)
    monkeypatch.setattr(mod, "now_ms", lambda: now)

    def fake_completed_since(day: str, since_ms: int) -> CompletionsSince:
        if since_ms == state["fire"]:
            return CompletionsSince(
                segments=(
                    {"stream": "default", "segment": "090000_300", "ts": now - 1},
                ),
                activities=(),
            )
        return CompletionsSince((), ())

    monkeypatch.setattr(mod, "read_completed_since", fake_completed_since)

    mod.dry_run("20990302", cadence=True)

    out = capsys.readouterr().out
    assert "2099-03-02" in out
    assert "cadence agents" in out
    assert "fire  fire" in out
    assert "window: 1 segment(s), 0 activity(ies)" in out
    assert "no-op no_work" in out
    assert "skip  wait" in out
    assert not (journal / "health" / "cadence.json").exists()


def test_dry_run_cadence_zero_talents(tmp_path, monkeypatch, capsys):
    """Dry-run cadence mode reports empty cadence schedules without writes."""
    mod = importlib.import_module("solstone.think.thinking")
    journal = tmp_path / "journal"
    (journal / "health").mkdir(parents=True)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setattr(mod, "get_talent_configs", lambda schedule: {})

    mod.dry_run("20990302", cadence=True)

    out = capsys.readouterr().out
    assert "cadence agents" in out
    assert "No prompts for schedule: cadence" in out
    assert not (journal / "health" / "cadence.json").exists()
