# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from solstone.think.call import call_app
from solstone.think.facet_review_candidates import (
    load_candidates,
    record_facet_candidate,
)

runner = CliRunner()


@pytest.fixture
def facet_candidate_journal(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    return Path(tmp_path)


def _seed_candidate(count: int = 3) -> None:
    record_facet_candidate(
        "Home Reno",
        "home reno",
        count,
        14,
        [{"day": "20260602", "stream": "archon", "segment": "090000_300"}],
        "20260602",
    )


def test_list_candidates_text_and_json(facet_candidate_journal):
    _seed_candidate()

    text = runner.invoke(call_app, ["facets", "list-candidates"])
    payload = runner.invoke(call_app, ["facets", "list-candidates", "--json"])

    assert text.exit_code == 0
    assert "Home Reno" in text.output
    assert "count=3" in text.output
    assert payload.exit_code == 0
    assert json.loads(payload.output)[0]["name_key"] == "home reno"


def test_list_candidates_filters_status(facet_candidate_journal):
    _seed_candidate()

    result = runner.invoke(
        call_app,
        ["facets", "list-candidates", "--status", "dismissed", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_accept_candidate_creates_facet_and_marks_accepted(facet_candidate_journal):
    _seed_candidate()

    result = runner.invoke(call_app, ["facets", "accept", "home reno"])

    assert result.exit_code == 0
    assert "Accepted facet candidate" in result.output
    assert (facet_candidate_journal / "facets" / "home-reno" / "facet.json").exists()
    assert load_candidates()[0]["status"] == "accepted"


def test_dismiss_candidate_sets_watermark(facet_candidate_journal):
    _seed_candidate(count=4)

    result = runner.invoke(call_app, ["facets", "dismiss", "home reno"])

    assert result.exit_code == 0
    assert "Dismissed facet candidate" in result.output
    row = load_candidates()[0]
    assert row["status"] == "dismissed"
    assert row["dismissed_count"] == 4


def test_accept_twice_is_idempotent(facet_candidate_journal):
    _seed_candidate()

    first = runner.invoke(call_app, ["facets", "accept", "home reno"])
    second = runner.invoke(call_app, ["facets", "accept", "home reno"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "already accepted" in second.output


def test_accept_missing_candidate_exits_nonzero(facet_candidate_journal):
    result = runner.invoke(call_app, ["facets", "accept", "missing"])

    assert result.exit_code == 1
    assert "candidate not found" in result.output
