# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for retired timeline segment-summary provider context registration."""

from __future__ import annotations

import importlib
import sys

from solstone.apps.timeline.tests.conftest import write_json

mod = importlib.import_module(
    "solstone.apps.timeline.maint.002_register_segment_summary_model"
)


def _journal_config_path(journal):
    return journal / "config" / "journal.json"


def test_registration_is_retired_noop(timeline_journal):
    path = _journal_config_path(timeline_journal)
    write_json(
        path,
        {
            "providers": {
                "contexts": {
                    "talent.timeline.segment_summary": {
                        "provider": "google",
                        "model": "legacy-model",
                    }
                }
            }
        },
    )
    before = path.read_bytes()

    summary = mod.run_registration(timeline_journal)

    assert summary.added == 0
    assert summary.preserved == 0
    assert summary.warnings == 0
    assert summary.errors == 0
    assert path.read_bytes() == before


def test_registration_main_succeeds_without_reading_malformed_config(
    timeline_journal, monkeypatch
):
    path = _journal_config_path(timeline_journal)
    path.write_text("{bad", encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["register-segment-summary-model"])

    mod.main()

    assert path.read_bytes() == before
