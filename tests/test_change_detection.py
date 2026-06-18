# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for segment change detection."""

import json
from pathlib import Path

from solstone.think.change_detection import (
    assemble_sensor_state,
    classify,
    compare_screen,
    compare_transcript,
    detect_segment_change,
    resolve_predecessor,
)


def _screen_state(
    *,
    key: str = "center:DP-3",
    first_hash: str | None = "0000000000000000",
    last_hash: str | None = "0000000000000000",
    qualified_count: int = 1,
) -> dict:
    return {
        "monitors": {
            key: {
                "first_hash": first_hash,
                "last_hash": last_hash,
                "qualified_count": qualified_count,
            }
        }
    }


def _transcript_state(
    *,
    present: bool = True,
    word_count: int = 10,
    content_hash: str | None = "sha256:aaa",
) -> dict:
    return {
        "present": present,
        "word_count": word_count,
        "content_hash": content_hash,
    }


def _make_segment(journal: Path, day: str, stream: str, segment: str) -> Path:
    seg_dir = journal / "chronicle" / day / stream / segment
    seg_dir.mkdir(parents=True)
    return seg_dir


def _write_screen(seg_dir: Path, *, first: str, last: str, count: int = 1) -> None:
    (seg_dir / "center_DP-3_screen.jsonl").write_text(
        json.dumps(
            {
                "raw": "center_DP-3_screen.webm",
                "first_hash": first,
                "last_hash": last,
                "qualified_count": count,
            }
        )
        + "\n",
        encoding="utf-8",
    )


class TestCompareScreen:
    def test_hamming_below_threshold_is_unchanged(self):
        result = compare_screen(
            _screen_state(last_hash="0000000000000000"),
            _screen_state(first_hash="0000000000000001"),
        )

        assert result == {"present": True, "changed": False}

    def test_hamming_at_threshold_is_changed(self):
        result = compare_screen(
            _screen_state(last_hash="0000000000000000"),
            _screen_state(first_hash="00000000000000ff"),
        )

        assert result == {"present": True, "changed": True}

    def test_monitor_present_on_one_side_is_changed(self):
        result = compare_screen({"monitors": {}}, _screen_state())

        assert result == {"present": True, "changed": True}

    def test_missing_hash_is_changed(self):
        result = compare_screen(
            _screen_state(last_hash=None),
            _screen_state(first_hash="0000000000000000"),
        )

        assert result == {"present": True, "changed": True}

    def test_single_frame_monitor_uses_same_boundary_hash(self):
        result = compare_screen(
            _screen_state(
                first_hash="0000000000000001",
                last_hash="0000000000000001",
            ),
            _screen_state(
                first_hash="0000000000000001",
                last_hash="0000000000000001",
            ),
        )

        assert result == {"present": True, "changed": False}


class TestCompareTranscript:
    def test_identical_hash_is_unchanged(self):
        result = compare_transcript(
            _transcript_state(word_count=12, content_hash="sha256:same"),
            _transcript_state(word_count=18, content_hash="sha256:same"),
        )

        assert result == {"present": True, "changed": False}

    def test_differing_hash_with_word_delta_above_floor_is_changed(self):
        result = compare_transcript(
            _transcript_state(word_count=10, content_hash="sha256:prev"),
            _transcript_state(word_count=16, content_hash="sha256:curr"),
        )

        assert result == {"present": True, "changed": True}

    def test_differing_hash_with_word_delta_at_floor_is_unchanged(self):
        result = compare_transcript(
            _transcript_state(word_count=10, content_hash="sha256:prev"),
            _transcript_state(word_count=15, content_hash="sha256:curr"),
        )

        assert result == {"present": True, "changed": False}

    def test_absent_on_either_side_is_not_present(self):
        result = compare_transcript(
            _transcript_state(present=False, word_count=0, content_hash=None),
            _transcript_state(word_count=20, content_hash="sha256:curr"),
        )

        assert result == {"present": False, "changed": False}


class TestClassify:
    def test_all_absent_is_idle(self):
        assert classify(
            {
                "screen": {"present": False, "changed": False},
                "transcript": {"present": False, "changed": False},
            }
        ) == ("idle", [])

    def test_present_and_none_changed_is_redundant(self):
        assert classify(
            {
                "screen": {"present": True, "changed": False},
                "transcript": {"present": False, "changed": False},
            }
        ) == ("redundant", [])

    def test_any_changed_is_active(self):
        assert classify(
            {
                "transcript": {"present": True, "changed": True},
                "screen": {"present": True, "changed": True},
            }
        ) == ("active", ["screen", "transcript"])


class TestResolvePredecessor:
    def test_resolution_uses_chronological_prior_even_before_state_exists(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
        day = "20260102"
        _make_segment(tmp_path, day, "A", "101000_300")
        _make_segment(tmp_path, day, "A", "100000_300")

        assert resolve_predecessor(day, "A", "101000_300") == {
            "day": day,
            "stream": "A",
            "segment": "100000_300",
        }

    def test_interleaved_streams_are_filtered(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
        day = "20260102"
        _make_segment(tmp_path, day, "A", "100000_300")
        _make_segment(tmp_path, day, "B", "100500_300")
        _make_segment(tmp_path, day, "A", "101000_300")

        assert resolve_predecessor(day, "A", "101000_300") == {
            "day": day,
            "stream": "A",
            "segment": "100000_300",
        }

    def test_first_of_stream_has_no_predecessor(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
        day = "20260102"
        _make_segment(tmp_path, day, "A", "100000_300")

        assert resolve_predecessor(day, "A", "100000_300") is None

    def test_gap_above_threshold_has_no_predecessor(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
        day = "20260102"
        _make_segment(tmp_path, day, "A", "100000_300")
        _make_segment(tmp_path, day, "A", "102000_300")

        assert resolve_predecessor(day, "A", "102000_300") is None


class TestDetectSegmentChange:
    def test_no_predecessor_with_present_sensor_is_active(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
        day = "20260102"
        seg_dir = _make_segment(tmp_path, day, "A", "100000_300")
        _write_screen(
            seg_dir,
            first="0000000000000000",
            last="0000000000000000",
        )

        result = detect_segment_change(
            day,
            "A",
            "100000_300",
            seg_dir,
            predecessor=None,
            timestamp="2026-01-02T10:00:00+00:00",
        )

        assert result["change_class"] == "active"
        assert result["changed_sensors"] == ["screen"]

    def test_missing_predecessor_state_with_present_sensor_is_active(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
        day = "20260102"
        _make_segment(tmp_path, day, "A", "100000_300")
        seg_dir = _make_segment(tmp_path, day, "A", "101000_300")
        _write_screen(
            seg_dir,
            first="0000000000000000",
            last="0000000000000000",
        )
        predecessor = {"day": day, "stream": "A", "segment": "100000_300"}

        result = detect_segment_change(
            day,
            "A",
            "101000_300",
            seg_dir,
            predecessor=predecessor,
            timestamp="2026-01-02T10:10:00+00:00",
        )

        assert result["predecessor"] == predecessor
        assert result["change_class"] == "active"
        assert result["changed_sensors"] == ["screen"]

    def test_empty_segment_without_predecessor_is_idle(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
        day = "20260102"
        seg_dir = _make_segment(tmp_path, day, "A", "100000_300")

        result = detect_segment_change(
            day,
            "A",
            "100000_300",
            seg_dir,
            predecessor=None,
            timestamp="2026-01-02T10:00:00+00:00",
        )

        assert result["change_class"] == "idle"
        assert result["changed_sensors"] == []


def test_assemble_transcript_excludes_document_extraction_jsonl(tmp_path):
    seg_dir = tmp_path / "segment"
    seg_dir.mkdir()
    (seg_dir / "audio.jsonl").write_text(
        json.dumps({"raw": "audio.flac"})
        + "\n"
        + json.dumps({"start": "00:00:01", "text": "spoken words"})
        + "\n",
        encoding="utf-8",
    )
    (seg_dir / "document.jsonl").write_text(
        json.dumps({"kind": "document"})
        + "\n"
        + json.dumps({"start": "00:00:01", "text": "document words " * 20})
        + "\n",
        encoding="utf-8",
    )

    state = assemble_sensor_state(seg_dir)

    assert state["transcript"]["present"] is True
    assert state["transcript"]["word_count"] < 20
