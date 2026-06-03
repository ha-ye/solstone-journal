# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from solstone.apps.activities.talent.activities_review import (
    assemble_activity_evidence,
    pre_process,
)
from solstone.think.talent import get_talent


def test_assemble_activity_evidence_includes_records_and_narratives(monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", "tests/fixtures/journal")

    evidence = assemble_activity_evidence("montague", "20260306")

    assert "# Activity evidence for montague on 20260306" in evidence
    assert "## Existing records" in evidence
    assert "## Per-span narratives" in evidence
    assert "engineering_143000_300" in evidence
    assert "Verona Platform" in evidence
    assert "Engineering Session Review" in evidence
    assert "schema translation" in evidence


def test_pre_process_returns_activity_evidence_template_vars(monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", "tests/fixtures/journal")

    result = pre_process({"facet": "montague", "day": "20260306"})

    assert isinstance(result, dict)
    assert result["template_vars"]["activity_evidence"]


def test_pre_process_missing_facet_returns_none():
    assert pre_process({"day": "20260306"}) is None


def test_pre_process_missing_day_returns_none():
    assert pre_process({"facet": "montague"}) is None


def test_activities_review_talent_config(monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", "tests/fixtures/journal")

    config = get_talent("activities:activities_review")

    assert config["type"] == "cogitate"
    assert config["schedule"] == "daily"
    assert config["multi_facet"] is True
    assert config["priority"] == 30
    assert config["hook"]["pre"] == "activities:activities_review"
    assert "$activity_evidence" in config["user_instruction"]
    assert "sol call activities list" not in config["user_instruction"]
    assert "$activity_md_dir" not in config["user_instruction"]
