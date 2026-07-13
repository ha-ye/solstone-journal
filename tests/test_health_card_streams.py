# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Health card stream privacy covenant tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from solstone.think import cluster
from solstone.think.importers import health_schema
from solstone.think.pipeline_health import (
    classify_segment_completion,
    read_segment_progress,
)
from solstone.think.utils import day_path
from tests.test_apple_health_importer import FIXTURE_ROOT, _process_with_approval

# Privacy covenant: weakening or removing these invariants requires legal
# review. Derived health cards restate already-indexed health records; if they
# are re-processed, health summaries can be mined into entities, facets, and
# model prompts.


def _registered_card_streams() -> set[str]:
    return {
        stream
        for stream in health_schema.HEALTH_CARD_STREAM_BY_FAMILY.values()
        if stream is not None
    }


def test_registered_health_card_streams_are_cluster_excluded():
    card_streams = _registered_card_streams()

    assert card_streams
    assert card_streams <= cluster.HEALTH_CARD_STREAMS


def test_apple_health_day_summary_writer_resolves_card_stream_at_call_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    canary_stream = "import.health_card_canary"
    registry = dict(health_schema.HEALTH_CARD_STREAM_BY_FAMILY)
    registry[health_schema.SOURCE_APPLE_HEALTH] = canary_stream
    monkeypatch.setattr(health_schema, "HEALTH_CARD_STREAM_BY_FAMILY", registry)

    result = _process_with_approval(
        FIXTURE_ROOT,
        tmp_path,
        import_id="20260103_120000",
        dry_run=False,
        date_from="2026-01-02",
        date_to="2026-01-02",
        with_day_summaries=True,
    )

    summary_path = (
        tmp_path
        / "chronicle"
        / "20260102"
        / canary_stream
        / "000000_300"
        / "day_summary_transcript.md"
    )
    assert result.files_created == [str(summary_path)]
    assert summary_path.exists()
    assert not (tmp_path / "chronicle" / "20260102" / "import.apple_health").exists()


def test_markdown_only_health_card_streams_skip_think(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    card_streams = sorted(_registered_card_streams())
    apple_stream = health_schema.health_card_stream(health_schema.SOURCE_APPLE_HEALTH)
    oura_stream = health_schema.health_card_stream(health_schema.SOURCE_OURA_API)

    assert apple_stream in card_streams
    assert oura_stream in card_streams

    for index, stream in enumerate(card_streams, start=1):
        day = f"2024010{index}"
        segment = day_path(day) / stream / "090000_300"
        segment.mkdir(parents=True)
        (segment / "day_summary_transcript.md").write_text(
            "health summary\n",
            encoding="utf-8",
        )

        audio_ranges, screen_ranges, segments = cluster.scan_day(day)

        assert audio_ranges == []
        assert screen_ranges == []
        assert segments == [
            {
                "key": "090000_300",
                "start": "09:00",
                "end": "09:05",
                "types": ["markdown"],
                "stream": stream,
                "data_state": {"markdown": "analyzed"},
            }
        ]
        completion = classify_segment_completion(segments, read_segment_progress(day))
        assert completion.blockers == []
        assert completion.not_sensed == 0
        assert completion.not_thought == 0
