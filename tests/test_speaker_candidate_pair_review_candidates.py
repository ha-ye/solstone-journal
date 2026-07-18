# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

import pytest

import solstone.think.speaker_candidate_pair_review_candidates as mod
from solstone.think.speaker_candidate_pair_review_candidates import (
    candidate_key,
    dismiss_candidate,
    find_candidate,
    is_dismissed_pair_suppressed,
    load_candidates,
    record_candidate_pair_candidate,
    review_candidates_path,
)


@pytest.fixture
def candidate_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None
    return Path(tmp_path)


def test_candidate_pair_path_and_key_are_order_independent(candidate_journal):
    anchor_a = '["20260101","090000_300","test","mic_audio",1]'
    anchor_b = '["20260102","090000_300","test","mic_audio",1]'

    assert (
        review_candidates_path()
        == candidate_journal / "speakers" / "candidate-pair-review-candidates.jsonl"
    )
    assert candidate_key(anchor_a, anchor_b) == candidate_key(anchor_b, anchor_a)


def test_record_candidate_pair_candidate_creates_evidence_row(
    candidate_journal, monkeypatch
):
    monkeypatch.setattr(mod, "utc_now_iso", lambda: "2026-06-03T17:30:00Z")
    anchor_a = '["20260101","090000_300","test","mic_audio",1]'
    anchor_b = '["20260102","090000_300","test","mic_audio",2]'
    sample = {
        "day": "20260101",
        "stream": "test",
        "segment_key": "090000_300",
        "source": "mic_audio",
        "cluster_label": 1,
        "audio_url": "/app/speakers/api/serve_audio/20260101/test/090000_300/mic_audio.flac",
    }

    row, created, suppressed = record_candidate_pair_candidate(
        source_anchor=anchor_b,
        target_anchor=anchor_a,
        source_anchors={anchor_b},
        target_anchors={anchor_a},
        similarity=0.62,
        source_intervals=31,
        target_intervals=35,
        source_samples=[sample],
        target_samples=[],
    )

    assert created is True
    assert suppressed is False
    assert row == {
        "key": candidate_key(anchor_a, anchor_b),
        "anchor_a": anchor_a,
        "anchor_b": anchor_b,
        "status": "open",
        "similarity": 0.62,
        "evidence": {
            "basis": "speaker-candidate-pair",
            "similarity": 0.62,
            "source_intervals": 31,
            "target_intervals": 35,
            "source_samples": [sample],
            "target_samples": [],
        },
        "first_surfaced": "2026-06-03T17:30:00Z",
        "last_surfaced": "2026-06-03T17:30:00Z",
        "created_at": "2026-06-03T17:30:00Z",
        "updated_at": "2026-06-03T17:30:00Z",
    }
    assert load_candidates() == [row]
    assert find_candidate(load_candidates(), anchor_b, anchor_a) == row


def test_dismissal_suppression_survives_id_churn_membership(
    candidate_journal, monkeypatch
):
    times = iter(["2026-06-03T17:30:00Z", "2026-06-04T17:30:00Z"])
    monkeypatch.setattr(mod, "utc_now_iso", lambda: next(times))
    anchor_a = '["20260101","090000_300","test","mic_audio",1]'
    anchor_b = '["20260102","090000_300","test","mic_audio",1]'
    anchor_c = '["20260103","090000_300","test","mic_audio",1]'

    record_candidate_pair_candidate(
        source_anchor=anchor_a,
        target_anchor=anchor_b,
        source_anchors={anchor_a},
        target_anchors={anchor_b},
        similarity=0.61,
        source_intervals=31,
        target_intervals=32,
        source_samples=[],
        target_samples=[],
    )
    dismissed = dismiss_candidate(anchor_a, anchor_b)
    assert dismissed is not None

    rows = load_candidates()
    assert is_dismissed_pair_suppressed(rows, {anchor_a, anchor_c}, {anchor_b})
    assert is_dismissed_pair_suppressed(rows, {anchor_b}, {anchor_a, anchor_c})
    row, created, suppressed = record_candidate_pair_candidate(
        source_anchor=anchor_c,
        target_anchor=anchor_b,
        source_anchors={anchor_a, anchor_c},
        target_anchors={anchor_b},
        similarity=0.63,
        source_intervals=60,
        target_intervals=32,
        source_samples=[],
        target_samples=[],
    )

    assert row is None
    assert created is False
    assert suppressed is True
    assert len(load_candidates()) == 1
    assert load_candidates()[0]["status"] == "dismissed"


def test_dismissed_rows_do_not_reopen(candidate_journal):
    anchor_a = '["20260101","090000_300","test","mic_audio",1]'
    anchor_b = '["20260102","090000_300","test","mic_audio",1]'
    record_candidate_pair_candidate(
        source_anchor=anchor_a,
        target_anchor=anchor_b,
        source_anchors={anchor_a},
        target_anchors={anchor_b},
        similarity=0.61,
        source_intervals=31,
        target_intervals=32,
        source_samples=[],
        target_samples=[],
    )
    dismiss_candidate(anchor_a, anchor_b)

    row, created, suppressed = record_candidate_pair_candidate(
        source_anchor=anchor_a,
        target_anchor=anchor_b,
        source_anchors={anchor_a},
        target_anchors={anchor_b},
        similarity=0.64,
        source_intervals=31,
        target_intervals=32,
        source_samples=[],
        target_samples=[],
    )

    assert row is None
    assert created is False
    assert suppressed is True
    assert load_candidates()[0]["status"] == "dismissed"
