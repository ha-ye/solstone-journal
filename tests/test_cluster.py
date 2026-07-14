# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import importlib
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from solstone.observe.processing_record import STATE_EMPTY
from solstone.think.utils import day_path

DAY = "20240101"
SEGMENT = "120000_300"


def _patch_prepare_config_model_deps(monkeypatch) -> None:
    monkeypatch.setattr(
        "solstone.think.models.resolve_provider",
        lambda _type: ("google", "gemini-test"),
    )
    monkeypatch.setattr(
        "solstone.think.models.type_default_is_local", lambda _type: False
    )


def _write_probe_talent(talent_dir: Path, load: dict | str) -> str:
    name = "required_talent_probe"
    metadata = {
        "type": "generate",
        "schedule": "segment",
        "priority": 10,
        "output": "md",
        "load": {
            "transcripts": False,
            "percepts": False,
            "talents": load,
        },
    }
    (talent_dir / f"{name}.md").write_text(
        f"{json.dumps(metadata, indent=2)}\n\nProbe prompt\n",
        encoding="utf-8",
    )
    return name


def _seed_talent_output(
    root: Path,
    name: str,
    content: str = "Agent output with enough substantive content for input gating.",
) -> None:
    segment_dir = root / "chronicle" / DAY / "default" / SEGMENT / "talents"
    segment_dir.mkdir(parents=True, exist_ok=True)
    (segment_dir / f"{name}.md").write_text(content, encoding="utf-8")


def _seed_screen_json(segment_dir: Path) -> None:
    talents_dir = segment_dir / "talents"
    talents_dir.mkdir(parents=True, exist_ok=True)
    (talents_dir / "screen.json").write_text(
        json.dumps(
            {
                "narrative": "09:00 Alice Smith reviewed the launch board.",
                "entities": [
                    {
                        "type": "Person",
                        "name": "Alice Smith",
                        "role": "attendee",
                        "context": "Visible in the meeting participant tile.",
                    },
                    {
                        "type": "Project",
                        "name": "launch board",
                        "role": "mentioned",
                        "context": "Reviewed on screen.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_cluster(tmp_path, monkeypatch):
    """Test cluster() uses transcripts and agent output summaries (*.md files)."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")
    # Write JSONL format: metadata first, then entry in segment directory
    (day_dir / "default" / "120000_300").mkdir(parents=True)
    (day_dir / "default" / "120000_300" / "audio.jsonl").write_text(
        '{}\n{"text": "hi"}\n'
    )
    (day_dir / "default" / "120500_300").mkdir(parents=True)
    (day_dir / "default" / "120500_300" / "talents").mkdir()
    (day_dir / "default" / "120500_300" / "talents" / "screen.md").write_text(
        "screen summary"
    )
    result, counts = mod.cluster(
        "20240101", sources={"transcripts": True, "percepts": False, "agents": True}
    )
    assert counts["transcripts"] == 1
    assert counts["talents"] == 1
    assert "### Transcript" in result
    # Now uses insight rendering: "### {stem} summary"
    assert "screen summary" in result


def test_process_segment_ignores_document_jsonl_but_keeps_image_jsonl(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")
    segment = day_dir / "default" / "123456_300"
    segment.mkdir(parents=True)
    (segment / "report.jsonl").write_text(
        json.dumps({"raw": "report.pdf", "kind": "document"})
        + "\n"
        + json.dumps({"start": "00:00:00", "text": "hello doc"})
        + "\n",
        encoding="utf-8",
    )
    (segment / "image.jsonl").write_text(
        json.dumps({"raw": "image.png", "kind": "image"})
        + "\n"
        + json.dumps({"start": "00:00:00", "text": "hello image"})
        + "\n",
        encoding="utf-8",
    )

    mod = importlib.import_module("solstone.think.cluster")

    entries = mod._process_segment(
        segment, "20240101", transcripts=True, percepts=False, agents=False
    )

    assert len(entries) == 1
    assert entries[0]["prefix"] == "transcript"
    assert entries[0]["name"] == "123456_300/image.jsonl"
    assert "hello image" in entries[0]["content"]
    assert "hello doc" not in entries[0]["content"]


def test_process_segment_reads_document_transcript_md_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")
    segment = day_dir / "import.document" / "123456_0"
    segment.mkdir(parents=True)
    (segment / "document_transcript.md").write_text("document body", encoding="utf-8")
    (segment / "report.jsonl").write_text(
        json.dumps({"raw": "report.pdf", "kind": "document"})
        + "\n"
        + json.dumps({"start": "00:00:00", "text": "legacy duplicate"})
        + "\n",
        encoding="utf-8",
    )

    mod = importlib.import_module("solstone.think.cluster")

    entries = mod._process_segment(
        segment, "20240101", transcripts=True, percepts=False, agents=False
    )

    assert len(entries) == 1
    assert entries[0]["name"] == "123456_0/document_transcript.md"
    assert entries[0]["content"] == "document body"


def test_cluster_range(tmp_path, monkeypatch):
    """Test cluster_range with transcripts and agents sources."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")
    # Write JSONL format: metadata first, then entry with proper start time and source in segment directory
    (day_dir / "default" / "120000_300").mkdir(parents=True)
    (day_dir / "default" / "120000_300" / "audio.jsonl").write_text(
        '{"raw": "raw.flac", "model": "whisper-1"}\n'
        '{"start": "00:00:01", "source": "mic", "text": "hi from audio"}\n'
    )
    (day_dir / "default" / "120000_300" / "talents").mkdir()
    (day_dir / "default" / "120000_300" / "talents" / "screen.md").write_text(
        "screen summary content"
    )
    # Test with agents=True to include *.md files
    md = mod.cluster_range(
        "20240101",
        "120000",
        "120100",
        sources={"transcripts": True, "percepts": False, "agents": True},
    )
    # Check that the function works and includes expected sections
    assert "### Transcript" in md
    # Now uses insight rendering: "### {stem} summary"
    assert "screen summary" in md
    assert "screen summary content" in md


def test_cluster_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")
    # Audio transcripts at 09:01, 09:05, 09:20 and 11:00 (JSONL format with empty metadata)
    (day_dir / "default" / "090101_300").mkdir(parents=True)
    (day_dir / "default" / "090101_300" / "audio.jsonl").write_text("{}\n")
    (day_dir / "default" / "090500_300").mkdir(parents=True)
    (day_dir / "default" / "090500_300" / "audio.jsonl").write_text("{}\n")
    (day_dir / "default" / "092000_300").mkdir(parents=True)
    (day_dir / "default" / "092000_300" / "audio.jsonl").write_text("{}\n")
    (day_dir / "default" / "110000_300").mkdir(parents=True)
    (day_dir / "default" / "110000_300" / "audio.jsonl").write_text("{}\n")
    # Screen transcripts at 10:01, 10:05, 10:20 and 12:00
    (day_dir / "default" / "100101_300").mkdir(parents=True)
    (day_dir / "default" / "100101_300" / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n'
    )
    (day_dir / "default" / "100500_300").mkdir(parents=True)
    (day_dir / "default" / "100500_300" / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n'
    )
    (day_dir / "default" / "102000_300").mkdir(parents=True)
    (day_dir / "default" / "102000_300" / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n'
    )
    (day_dir / "default" / "120000_300").mkdir(parents=True)
    (day_dir / "default" / "120000_300" / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n'
    )
    audio_ranges, screen_ranges = mod.cluster_scan("20240101")
    # Expected ranges: 15-minute slot grouping (segments 09:01-09:05-09:20 group together)
    # Slots: 09:00, 09:00, 09:15 -> ranges: 09:00-09:30; 11:00 -> 11:00-11:15
    assert audio_ranges == [("09:00", "09:30"), ("11:00", "11:15")]
    assert screen_ranges == [("10:00", "10:30"), ("12:00", "12:15")]


def test_cluster_segments(tmp_path, monkeypatch):
    """Test cluster_segments returns individual segments with their types."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    # Create segment with duration: 090000_300 (09:00:00 for 5 minutes)
    (day_dir / "default" / "090000_300").mkdir(parents=True)
    (day_dir / "default" / "090000_300" / "audio.jsonl").write_text("{}\n")

    # Create segment with both audio and screen
    (day_dir / "default" / "100000_600").mkdir(parents=True)
    (day_dir / "default" / "100000_600" / "audio.jsonl").write_text("{}\n")
    (day_dir / "default" / "100000_600" / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n'
    )

    # Create segment with only screen
    (day_dir / "default" / "110000_300").mkdir(parents=True)
    (day_dir / "default" / "110000_300" / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n'
    )

    segments = mod.cluster_segments("20240101")

    assert len(segments) == 3

    # Check first segment (audio only)
    assert segments[0]["key"] == "090000_300"
    assert segments[0]["start"] == "09:00"
    assert segments[0]["end"] == "09:05"
    assert segments[0]["types"] == ["audio"]

    # Check second segment (both transcripts and screen)
    assert segments[1]["key"] == "100000_600"
    assert segments[1]["start"] == "10:00"
    assert segments[1]["end"] == "10:10"
    assert "audio" in segments[1]["types"]
    assert "screen" in segments[1]["types"]

    # Check third segment (screen only)
    assert segments[2]["key"] == "110000_300"
    assert segments[2]["start"] == "11:00"
    assert segments[2]["end"] == "11:05"
    assert segments[2]["types"] == ["screen"]


def test_cluster_period_uses_raw_screen(tmp_path, monkeypatch):
    """Test cluster_period uses raw screen.jsonl, not insight *.md files."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    # Create segment with both audio and raw screen data
    segment = day_dir / "default" / "100000_300"
    segment.mkdir(parents=True)
    (segment / "audio.jsonl").write_text(
        '{"raw": "audio.flac"}\n{"start": "00:00:01", "text": "hello"}\n'
    )
    # Raw screen.jsonl with frame analysis (what cluster_period should use)
    (segment / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n'
        '{"timestamp": 10, "analysis": {"primary": "code_editor", '
        '"visual_description": "VS Code with Python file"}}\n'
    )
    # Also create screen.md (insight) to verify it's NOT used by cluster_period
    (segment / "talents").mkdir()
    (segment / "talents" / "screen.md").write_text("This insight should NOT appear")

    result, counts = mod.cluster_period(
        "20240101",
        "100000_300",
        sources={"transcripts": True, "percepts": True, "agents": False},
    )

    # Should have both transcript and screen entries
    assert counts["transcripts"] == 1
    assert counts["percepts"] == 1
    assert "### Transcript" in result
    # Should use raw screen format header
    assert "Screen Activity" in result
    # Raw screen content should be present
    assert "VS Code with Python file" in result
    # Insight content should NOT be present (agents=False for cluster_period)
    assert "This insight should NOT appear" not in result


def test_load_entries_from_toplevel_segment(tmp_path, monkeypatch):
    """_load_entries_from_segment resolves the day for top-level segment dirs."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")
    segment = day_dir / "100000_300"
    segment.mkdir()

    mod = importlib.import_module("solstone.think.cluster")

    entries = mod._load_entries_from_segment(
        str(segment),
        transcripts=True,
        percepts=False,
        agents=False,
    )

    assert entries == []


def test_cluster_range_with_agents(tmp_path, monkeypatch):
    """Test cluster_range with agents source loads all *.md files."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    # Create segment with multiple insight files
    segment = day_dir / "default" / "100000_300"
    segment.mkdir(parents=True)
    (segment / "talents").mkdir()
    (segment / "audio.jsonl").write_text(
        '{"raw": "audio.flac"}\n{"start": "00:00:01", "text": "hello"}\n'
    )
    (segment / "talents" / "screen.md").write_text("Screen activity summary")
    (segment / "talents" / "activity.md").write_text("Activity insight content")
    # Also create screen.jsonl to verify it's NOT used when agents=True, screen=False
    (segment / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n'
        '{"timestamp": 10, "analysis": {"primary": "code_editor"}}\n'
    )

    # Test agents=True returns *.md summaries, not raw screen data
    result = mod.cluster_range(
        "20240101",
        "100000",
        "100500",
        sources={"transcripts": True, "percepts": False, "agents": True},
    )

    assert "### Transcript" in result
    # Should include both .md files as agent outputs
    assert "### screen summary" in result
    assert "Screen activity summary" in result
    assert "### activity summary" in result
    assert "Activity insight content" in result
    # Should NOT include raw screen data
    assert "code_editor" not in result


def test_cluster_range_with_screen(tmp_path, monkeypatch):
    """Test cluster_range with screen source loads raw screen.jsonl data."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    # Create segment with raw screen data and insight file
    segment = day_dir / "default" / "100000_300"
    segment.mkdir(parents=True)
    (segment / "talents").mkdir()
    (segment / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n'
        '{"timestamp": 10, "analysis": {"primary": "code_editor"}}\n'
    )
    (segment / "talents" / "screen.md").write_text("Screen summary insight")

    # Test screen=True returns raw screen data, not agent outputs
    result = mod.cluster_range(
        "20240101",
        "100000",
        "100500",
        sources={"transcripts": False, "percepts": True, "agents": False},
    )

    assert "Screen Activity" in result
    assert "code_editor" in result
    # Should NOT include insight content
    assert "Screen summary insight" not in result
    assert "### screen summary" not in result


def test_cluster_range_with_multiple_screen_files(tmp_path, monkeypatch):
    """Test cluster_range loads multiple *_screen.jsonl files per segment."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    # Create segment with multiple screen files (like multi-monitor setup)
    segment = day_dir / "default" / "100000_300"
    segment.mkdir(parents=True)
    (segment / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n'
        '{"timestamp": 10, "analysis": {"primary": "code_editor", '
        '"visual_description": "Primary monitor with VS Code"}}\n'
    )
    (segment / "monitor_2_screen.jsonl").write_text(
        '{"raw": "monitor_2.webm"}\n'
        '{"timestamp": 10, "analysis": {"primary": "browser", '
        '"visual_description": "Secondary monitor with documentation"}}\n'
    )

    # Test screen=True returns data from both screen files
    result = mod.cluster_range(
        "20240101",
        "100000",
        "100500",
        sources={"transcripts": False, "percepts": True, "agents": False},
    )

    # Should include content from both screen files
    assert "Primary monitor with VS Code" in result
    assert "Secondary monitor with documentation" in result


def test_cluster_scan_with_split_screen(tmp_path, monkeypatch):
    """Test cluster_scan detects *_screen.jsonl files."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    # Create segment with only *_screen.jsonl (no screen.jsonl)
    (day_dir / "default" / "100000_300").mkdir(parents=True)
    (day_dir / "default" / "100000_300" / "monitor_1_screen.jsonl").write_text(
        '{"raw": "m1.webm"}\n'
    )

    audio_ranges, screen_ranges = mod.cluster_scan("20240101")

    # Should detect the segment as having screen content (15-minute slot grouping)
    assert screen_ranges == [("10:00", "10:15")]


def test_cluster_segments_with_split_screen(tmp_path, monkeypatch):
    """Test cluster_segments detects *_screen.jsonl files."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    # Create segment with only *_screen.jsonl (no screen.jsonl)
    (day_dir / "default" / "100000_300").mkdir(parents=True)
    (day_dir / "default" / "100000_300" / "wayland_screen.jsonl").write_text(
        '{"raw": "w.webm"}\n'
    )

    segments = mod.cluster_segments("20240101")

    assert len(segments) == 1
    assert segments[0]["key"] == "100000_300"
    assert "screen" in segments[0]["types"]


def test_cluster_span(tmp_path, monkeypatch):
    """Test cluster_span processes a span of segments."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    # Create three segments with different content
    (day_dir / "default" / "090000_300").mkdir(parents=True)
    (day_dir / "default" / "090000_300" / "audio.jsonl").write_text(
        '{"raw": "audio.flac"}\n{"start": "00:00:01", "text": "morning segment"}\n'
    )

    (day_dir / "default" / "100000_300").mkdir(parents=True)
    (day_dir / "default" / "100000_300" / "audio.jsonl").write_text(
        '{"raw": "audio.flac"}\n{"start": "00:00:01", "text": "mid-morning segment"}\n'
    )
    (day_dir / "default" / "100000_300" / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n'
        '{"timestamp": 10, "analysis": {"primary": "code_editor"}}\n'
    )

    (day_dir / "default" / "110000_300").mkdir(parents=True)
    (day_dir / "default" / "110000_300" / "audio.jsonl").write_text(
        '{"raw": "audio.flac"}\n{"start": "00:00:01", "text": "late morning segment"}\n'
    )

    # Process only first and third segments as a span (audio only, no screen)
    result, counts = mod.cluster_span(
        "20240101",
        ["090000_300", "110000_300"],
        sources={"transcripts": True, "percepts": False, "agents": False},
    )

    # Should have 2 transcript entries (one per segment)
    assert counts["transcripts"] == 2
    assert counts["percepts"] == 0
    assert "morning segment" in result
    assert "late morning segment" in result
    # Should NOT include the skipped segment
    assert "mid-morning segment" not in result
    assert "code_editor" not in result


def test_cluster_span_missing_segment(tmp_path, monkeypatch):
    """Test cluster_span fails fast when segment is missing."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    # Create only one segment
    (day_dir / "default" / "090000_300").mkdir(parents=True)
    (day_dir / "default" / "090000_300" / "audio.jsonl").write_text(
        '{"raw": "audio.flac"}\n'
    )

    # Try to process existing and non-existing segments
    with pytest.raises(ValueError) as exc_info:
        mod.cluster_span(
            "20240101",
            ["090000_300", "100000_300"],
            sources={"transcripts": True, "percepts": False, "agents": False},
        )

    assert "100000_300" in str(exc_info.value)
    assert "not found" in str(exc_info.value)


def test_cluster_with_agent_filter_dict(tmp_path, monkeypatch):
    """Test cluster() with dict-valued agents source for selective filtering."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    # Create segment with multiple agent output files
    segment = day_dir / "default" / "120000_300"
    segment.mkdir(parents=True)
    (segment / "talents").mkdir()
    (segment / "audio.jsonl").write_text('{}\n{"text": "hello"}\n')
    (segment / "talents" / "entities.md").write_text("Entity extraction results")
    (segment / "talents" / "meetings.md").write_text("Meeting summary results")
    (segment / "talents" / "flow.md").write_text("Flow analysis results")

    # Test filtering to only include entities
    result, counts = mod.cluster(
        "20240101",
        sources={"transcripts": True, "percepts": False, "agents": {"entities": True}},
    )

    assert counts["transcripts"] == 1
    assert counts["talents"] == 1  # Only entities should be counted
    assert "Entity extraction results" in result
    assert "Meeting summary results" not in result
    assert "Flow analysis results" not in result


def test_cluster_with_agent_filter_multiple(tmp_path, monkeypatch):
    """Test cluster() with dict selecting multiple agents."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    # Create segment with multiple agent output files
    segment = day_dir / "default" / "120000_300"
    segment.mkdir(parents=True)
    (segment / "talents").mkdir()
    (segment / "audio.jsonl").write_text('{}\n{"text": "hello"}\n')
    (segment / "talents" / "entities.md").write_text("Entity extraction results")
    (segment / "talents" / "meetings.md").write_text("Meeting summary results")
    (segment / "talents" / "flow.md").write_text("Flow analysis results")

    # Test filtering to include entities and meetings but not flow
    result, counts = mod.cluster(
        "20240101",
        sources={
            "transcripts": True,
            "percepts": False,
            "agents": {"entities": True, "meetings": "required", "flow": False},
        },
    )

    assert counts["transcripts"] == 1
    assert counts["talents"] == 2  # entities + meetings
    assert "Entity extraction results" in result
    assert "Meeting summary results" in result
    assert "Flow analysis results" not in result


def test_cluster_with_agent_filter_app_namespaced(tmp_path, monkeypatch):
    """Test cluster() with dict filtering app-namespaced agent outputs."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    # Create segment with app-namespaced agent output files
    # App agent output naming: "app:agent" -> "_app_agent.md"
    segment = day_dir / "default" / "120000_300"
    segment.mkdir(parents=True)
    (segment / "talents").mkdir()
    (segment / "audio.jsonl").write_text('{}\n{"text": "hello"}\n')
    (segment / "talents" / "entities.md").write_text("System entity results")
    (segment / "talents" / "_app_name.md").write_text("App agent results")

    # Test filtering to include app-namespaced agent
    result, counts = mod.cluster(
        "20240101",
        sources={
            "transcripts": True,
            "percepts": False,
            "agents": {"entities": False, "app:name": True},
        },
    )

    assert counts["transcripts"] == 1
    assert counts["talents"] == 1  # Only app:name
    assert "System entity results" not in result
    assert "App agent results" in result


def test_schedule_screen_talent_filter_loads_screen_json_only(tmp_path, monkeypatch):
    """Schedule's screen filter includes formatted screen.json without screen.md."""
    from solstone.think.talent import get_talent

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")
    segment = day_dir / "default" / "120000_300"
    segment.mkdir(parents=True)
    _seed_screen_json(segment)

    mod = importlib.import_module("solstone.think.cluster")
    agents = get_talent("schedule")["sources"]["talents"]
    result, counts = mod.cluster(
        "20240101",
        sources={"transcripts": False, "percepts": False, "agents": agents},
    )

    assert agents == {"screen": True}
    assert counts["talents"] == 1
    assert "### screen summary" in result
    assert "09:00 Alice Smith reviewed the launch board." in result
    assert "Project: launch board (mentioned)" in result


def test_speaker_attribution_screen_filter_loads_screen_json_only(
    tmp_path, monkeypatch
):
    """Speaker attribution's screen filter includes formatted screen.json only."""
    from solstone.think.talent import get_talent

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")
    segment = day_dir / "default" / "120000_300"
    segment.mkdir(parents=True)
    _seed_screen_json(segment)

    mod = importlib.import_module("solstone.think.cluster")
    agents = get_talent("speaker_attribution")["sources"]["talents"]
    result, counts = mod.cluster(
        "20240101",
        sources={"transcripts": False, "percepts": False, "agents": agents},
    )

    assert agents == {"screen": True}
    assert counts["talents"] == 1
    assert "### screen summary" in result
    assert "Alice Smith" in result
    assert "Visible in the meeting participant tile." in result


def test_participation_sense_filter_uses_single_formatted_sense_json_projection(
    tmp_path, monkeypatch
):
    """Participation's sense filter gets formatted sense.json, not stale sense.md."""
    from solstone.think.talent import get_talent

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")
    segment = day_dir / "default" / "120000_300"
    talents_dir = segment / "talents"
    talents_dir.mkdir(parents=True)
    (talents_dir / "sense.json").write_text(
        json.dumps(
            {
                "density": "active",
                "content_type": "meeting",
                "activity_summary": "Discussed the launch timeline.",
                "entities": [
                    {
                        "type": "Person",
                        "name": "Alice Smith",
                        "role": "attendee",
                        "source": "voice",
                        "context": "Owned the timeline follow-up.",
                        "level": "high",
                    }
                ],
                "facets": [
                    {
                        "facet": "work",
                        "activity": "launch planning",
                        "level": "high",
                    }
                ],
                "meeting_detected": True,
                "speakers": ["Alice Smith", "Bob Chen"],
            }
        ),
        encoding="utf-8",
    )
    (talents_dir / "sense.md").write_text(
        "# Sense Entities\n\n- STALE SENSE MD BULLET",
        encoding="utf-8",
    )

    mod = importlib.import_module("solstone.think.cluster")
    agents = get_talent("participation")["sources"]["talents"]
    result, counts = mod.cluster(
        "20240101",
        sources={"transcripts": False, "percepts": False, "agents": agents},
    )

    assert agents == {"sense": True}
    assert counts["talents"] == 1
    assert result.count("### sense summary") == 1
    assert "Discussed the launch timeline." in result
    assert "work: launch planning (high)" in result
    assert "**Speakers:** Alice Smith, Bob Chen" in result
    assert "Person: Alice Smith" in result
    assert "STALE SENSE MD BULLET" not in result


def test_cluster_with_empty_agent_filter(tmp_path, monkeypatch):
    """Test cluster() with empty dict means no agents."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    segment = day_dir / "default" / "120000_300"
    segment.mkdir(parents=True)
    (segment / "talents").mkdir()
    (segment / "audio.jsonl").write_text('{}\n{"text": "hello"}\n')
    (segment / "talents" / "entities.md").write_text("Entity extraction results")

    # Empty dict should mean no agents
    result, counts = mod.cluster(
        "20240101",
        sources={"transcripts": True, "percepts": False, "agents": {}},
    )

    assert counts["transcripts"] == 1
    assert counts["talents"] == 0
    assert "Entity extraction results" not in result


def test_cluster_count_keys_match_for_empty_and_populated_results(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    empty_day = day_path("20240102")
    empty_day.mkdir(parents=True, exist_ok=True)

    mod = importlib.import_module("solstone.think.cluster")
    _empty_result, empty_counts = mod.cluster(
        "20240102",
        sources={"transcripts": True, "percepts": True, "agents": True},
    )

    populated_day = day_path("20240103")
    segment = populated_day / "default" / "120000_300"
    (segment / "talents").mkdir(parents=True)
    (segment / "audio.jsonl").write_text(
        '{"raw": "audio.flac"}\n{"start": "00:00:01", "text": "hello"}\n',
        encoding="utf-8",
    )
    (segment / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n'
        '{"timestamp": 10, "analysis": {"primary": "code_editor"}}\n',
        encoding="utf-8",
    )
    (segment / "talents" / "sense.md").write_text(
        "Sense markdown with entity candidates.",
        encoding="utf-8",
    )
    _populated_result, populated_counts = mod.cluster(
        "20240103",
        sources={"transcripts": True, "percepts": True, "agents": True},
    )

    assert (
        set(empty_counts)
        == set(populated_counts)
        == {
            "transcripts",
            "percepts",
            "talents",
        }
    )


@pytest.mark.parametrize(
    ("load", "agent_name", "expected_skip"),
    [
        ("required", "sense", None),
        ({"sense": "required"}, "sense", None),
        ("required", None, "missing_required_talents"),
    ],
)
def test_prepare_config_required_talent_source_uses_talent_count_key(
    tmp_path,
    monkeypatch,
    load,
    agent_name,
    expected_skip,
):
    from solstone.think import talent as talent_module
    from solstone.think import talents

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(talent_module, "TALENT_DIR", tmp_path / "talents")
    talent_module.TALENT_DIR.mkdir()
    _patch_prepare_config_model_deps(monkeypatch)
    prompt_name = _write_probe_talent(talent_module.TALENT_DIR, load)
    if agent_name is not None:
        _seed_talent_output(tmp_path, agent_name)

    config = talents.prepare_config(
        {
            "name": prompt_name,
            "day": DAY,
            "segment": SEGMENT,
            "output": "md",
        }
    )

    assert config.get("skip_reason") == expected_skip


def test_filename_to_agent_key():
    """Test _filename_to_agent_key conversion."""
    from solstone.think.cluster import _filename_to_agent_key

    # System agents
    assert _filename_to_agent_key("entities") == "entities"
    assert _filename_to_agent_key("flow") == "flow"

    # App-namespaced agents
    assert _filename_to_agent_key("_app_name") == "app:name"
    assert _filename_to_agent_key("_entities_observer") == "entities:observer"

    # Edge case: single underscore component
    assert _filename_to_agent_key("_app") == "_app"  # No second part, returns as-is


def test_agent_matches_filter():
    """Test _agent_matches_filter logic."""
    from solstone.think.cluster import _agent_matches_filter

    # None filter means all agents
    assert _agent_matches_filter("entities", None) is True
    assert _agent_matches_filter("_app_name", None) is True

    # Empty dict means no agents
    assert _agent_matches_filter("entities", {}) is False
    assert _agent_matches_filter("_app_name", {}) is False

    # Specific filtering
    filter_dict = {"entities": True, "meetings": False, "app:name": "required"}
    assert _agent_matches_filter("entities", filter_dict) is True
    assert _agent_matches_filter("meetings", filter_dict) is False
    assert _agent_matches_filter("_app_name", filter_dict) is True
    assert _agent_matches_filter("flow", filter_dict) is False  # Not in filter


def test_scan_day_combined(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    first = day_dir / "default" / "090000_300"
    first.mkdir(parents=True)
    (first / "audio.jsonl").write_text("{}\n")
    (first / "screen.jsonl").write_text('{"raw": "screen.webm"}\n')

    second = day_dir / "default" / "093000_300"
    second.mkdir(parents=True)
    (second / "audio.jsonl").write_text("{}\n")

    audio_ranges, screen_ranges, segments = mod.scan_day("20240101")
    expected_ranges = mod.cluster_scan("20240101")
    expected_segments = mod.cluster_segments("20240101")

    assert audio_ranges == [("09:00", "09:15"), ("09:30", "09:45")]
    assert screen_ranges == [("09:00", "09:15")]
    assert segments == [
        {
            "key": "090000_300",
            "start": "09:00",
            "end": "09:05",
            "types": ["audio", "screen"],
            "stream": "default",
            "data_state": {"audio": "pending", "screen": "pending"},
        },
        {
            "key": "093000_300",
            "start": "09:30",
            "end": "09:35",
            "types": ["audio"],
            "stream": "default",
            "data_state": {"audio": "pending"},
        },
    ]
    assert (audio_ranges, screen_ranges) == expected_ranges
    assert segments == expected_segments


def test_scan_day_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    mod = importlib.import_module("solstone.think.cluster")

    assert mod.scan_day("20250101") == ([], [], [])


def test_scan_day_marks_stub_screen_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    segment = day_dir / "default" / "090000_300"
    segment.mkdir(parents=True)
    (segment / "screen.jsonl").write_text('{"raw": "screen.webm"}\n')

    audio_ranges, screen_ranges, segments = mod.scan_day("20240101")

    assert audio_ranges == []
    assert screen_ranges == [("09:00", "09:15")]
    assert segments == [
        {
            "key": "090000_300",
            "start": "09:00",
            "end": "09:05",
            "types": ["screen"],
            "stream": "default",
            "data_state": {"screen": "pending"},
        }
    ]


def test_scan_day_marks_headerless_screen_frame_analyzed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    segment = day_dir / "default" / "090000_300"
    segment.mkdir(parents=True)
    frame = {
        "frame_id": 1,
        "timestamp": 1,
        "analysis": {
            "primary": "work",
            "visual_description": "fedora tmux session",
        },
        "content": {},
    }
    (segment / "fedora_tmux_screen.jsonl").write_text(json.dumps(frame) + "\n")

    _, screen_ranges, segments = mod.scan_day("20240101")

    assert screen_ranges == [("09:00", "09:15")]
    assert segments[0]["data_state"] == {"screen": "analyzed"}


def test_scan_day_marks_analyzed_screen_analyzed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    segment = day_dir / "default" / "090000_300"
    segment.mkdir(parents=True)
    (segment / "screen.jsonl").write_text(
        '{"raw": "screen.webm"}\n{"timestamp": 1, "analysis": {"primary": "work"}}\n'
    )

    _, screen_ranges, segments = mod.scan_day("20240101")

    assert screen_ranges == [("09:00", "09:15")]
    assert segments[0]["data_state"] == {"screen": "analyzed"}


def test_scan_day_keeps_screen_raw_substring_collision_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    segment = day_dir / "default" / "090000_300"
    segment.mkdir(parents=True)
    (segment / "screen.jsonl").write_text('{"raw": "clip_timestamp.webm"}\n')

    _, screen_ranges, segments = mod.scan_day("20240101")

    assert screen_ranges == [("09:00", "09:15")]
    assert segments[0]["data_state"] == {"screen": "pending"}


def test_scan_day_marks_whitespace_only_screen_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    segment = day_dir / "default" / "090000_300"
    segment.mkdir(parents=True)
    (segment / "screen.jsonl").write_text("\n  \n\t\n")

    _, screen_ranges, segments = mod.scan_day("20240101")

    assert screen_ranges == [("09:00", "09:15")]
    assert segments[0]["data_state"] == {"screen": "pending"}


@pytest.mark.parametrize("raw_name", ["audio.flac", "audio.m4a"])
def test_scan_day_marks_raw_audio_without_jsonl_pending(
    tmp_path, monkeypatch, raw_name
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    segment = day_dir / "default" / "090000_300"
    segment.mkdir(parents=True)
    (segment / raw_name).write_bytes(b"audio")

    audio_ranges, screen_ranges, segments = mod.scan_day("20240101")

    assert audio_ranges == [("09:00", "09:15")]
    assert screen_ranges == []
    assert segments[0]["types"] == ["audio"]
    assert segments[0]["data_state"] == {"audio": "pending"}


def test_scan_day_marks_header_only_audio_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    segment = day_dir / "default" / "090000_300"
    segment.mkdir(parents=True)
    (segment / "audio.jsonl").write_text('{"raw": "audio.flac"}\n')

    audio_ranges, _, segments = mod.scan_day("20240101")

    assert audio_ranges == [("09:00", "09:15")]
    assert segments[0]["data_state"] == {"audio": "pending"}


def test_detect_data_state_reads_empty_processing_record(tmp_path):
    mod = importlib.import_module("solstone.think.cluster")
    plain_segment = tmp_path / "plain" / "090000_300"
    empty_record_segment = tmp_path / "empty_record" / "090000_300"
    plain_segment.mkdir(parents=True)
    empty_record_segment.mkdir(parents=True)
    (plain_segment / "screen.jsonl").write_text(
        json.dumps({"raw": "screen.webm"}) + "\n",
        encoding="utf-8",
    )
    (empty_record_segment / "screen.jsonl").write_text(
        json.dumps(
            {
                "raw": "screen.webm",
                "_solstone_processing": {"state": STATE_EMPTY},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    plain_state = mod._detect_data_state(plain_segment)
    empty_record_state = mod._detect_data_state(empty_record_segment)

    assert plain_state == {"screen": "pending"}
    assert empty_record_state == {"screen": "empty"}


def test_derive_modality_state_chunks_win_is_pure(tmp_path):
    from solstone.think.data_state import derive_modality_state

    segment = tmp_path / "090000_300"
    segment.mkdir()
    marker = segment / ".analyzing_audio"
    marker.write_text('{"started_at": "2026-05-20T09:00:00Z", "modality": "audio"}\n')

    state = derive_modality_state(
        segment,
        "audio",
        has_chunks=True,
        has_jsonl=True,
        has_raw=True,
    )

    assert state == "analyzed"
    assert marker.exists()


def test_repair_modality_markers_chunks_win_rescue(tmp_path):
    from solstone.think.data_state import repair_modality_markers

    segment = tmp_path / "090000_300"
    segment.mkdir()
    marker = segment / ".analyzing_audio"
    marker.write_text('{"started_at": "2026-05-20T09:00:00Z", "modality": "audio"}\n')

    repair_modality_markers(
        segment,
        "audio",
        has_chunks=True,
        has_jsonl=True,
        has_raw=True,
    )

    assert not marker.exists()


def test_derive_modality_state_stale_marker_is_pure(tmp_path):
    from solstone.think.data_state import derive_modality_state

    segment = tmp_path / "090000_300"
    segment.mkdir()
    marker = segment / ".analyzing_screen"
    failed = segment / ".analyze_failed_screen"
    marker.write_text('{"started_at": "2026-05-20T09:00:00Z", "modality": "screen"}\n')
    old_time = time.time() - 2000
    os.utime(marker, (old_time, old_time))

    state = derive_modality_state(
        segment,
        "screen",
        has_chunks=False,
        has_jsonl=True,
        has_raw=True,
    )

    assert state == "failed"
    assert marker.exists()
    assert not failed.exists()


def test_repair_modality_markers_stale_marker_renames_failed(tmp_path):
    from solstone.think.data_state import repair_modality_markers

    segment = tmp_path / "090000_300"
    segment.mkdir()
    marker = segment / ".analyzing_screen"
    failed = segment / ".analyze_failed_screen"
    marker.write_text('{"started_at": "2026-05-20T09:00:00Z", "modality": "screen"}\n')
    old_time = time.time() - 2000
    os.utime(marker, (old_time, old_time))

    repair_modality_markers(
        segment,
        "screen",
        has_chunks=False,
        has_jsonl=True,
        has_raw=True,
    )

    assert not marker.exists()
    payload = json.loads(failed.read_text())
    assert payload["reason"] == "stale"
    assert payload["modality"] == "screen"


def test_derive_modality_state_corrupt_marker_is_pure(tmp_path):
    from solstone.think.data_state import derive_modality_state

    segment = tmp_path / "090000_300"
    segment.mkdir()
    marker = segment / ".analyzing_screen"
    failed = segment / ".analyze_failed_screen"
    marker.write_text("{not json")

    state = derive_modality_state(
        segment,
        "screen",
        has_chunks=False,
        has_jsonl=False,
        has_raw=True,
    )

    assert state == "failed"
    assert marker.exists()
    assert not failed.exists()


def test_repair_modality_markers_corrupt_marker_renames_failed(tmp_path):
    from solstone.think.data_state import repair_modality_markers

    segment = tmp_path / "090000_300"
    segment.mkdir()
    marker = segment / ".analyzing_screen"
    failed = segment / ".analyze_failed_screen"
    marker.write_text("{not json")

    repair_modality_markers(
        segment,
        "screen",
        has_chunks=False,
        has_jsonl=False,
        has_raw=True,
    )

    assert not marker.exists()
    payload = json.loads(failed.read_text())
    assert payload["reason"] == "marker_corrupt"
    assert payload["modality"] == "screen"


def test_derive_modality_state_does_not_probe_processes(tmp_path, monkeypatch):
    from solstone.think.data_state import derive_modality_state

    def fail_os_kill(pid, sig):  # pragma: no cover - fails if called
        raise AssertionError("os.kill should not be used for analyzing state")

    monkeypatch.setattr(os, "kill", fail_os_kill)
    segment = tmp_path / "090000_300"
    segment.mkdir()
    (segment / ".analyzing_screen").write_text(
        '{"started_at": "2026-05-20T09:00:00Z", "modality": "screen"}\n'
    )

    assert (
        derive_modality_state(
            segment,
            "screen",
            has_chunks=False,
            has_jsonl=True,
            has_raw=True,
        )
        == "analyzing"
    )


@pytest.mark.parametrize(
    ("case", "expected_state"),
    [
        ("chunks_win", "analyzed"),
        ("stale", "failed"),
        ("corrupt", "failed"),
    ],
)
def test_detect_data_state_does_not_repair_markers(
    tmp_path, monkeypatch, case, expected_state
):
    from solstone.think import cluster, data_state

    def fail_write(*_args, **_kwargs):
        raise AssertionError("_write_failed_marker should not be called by reads")

    monkeypatch.setattr(data_state, "_write_failed_marker", fail_write)
    segment = tmp_path / "090000_300"
    segment.mkdir()
    marker = segment / ".analyzing_screen"
    failed = segment / ".analyze_failed_screen"

    if case == "chunks_win":
        (segment / "screen.jsonl").write_text(
            '{"raw": "screen.webm"}\n{"timestamp": 1, "content": {}}\n',
            encoding="utf-8",
        )
        marker.write_text(
            '{"started_at": "2026-05-20T09:00:00Z", "modality": "screen"}\n',
            encoding="utf-8",
        )
    elif case == "stale":
        (segment / "screen.webm").write_bytes(b"raw")
        marker.write_text(
            '{"started_at": "2026-05-20T09:00:00Z", "modality": "screen"}\n',
            encoding="utf-8",
        )
        old_time = time.time() - 2000
        os.utime(marker, (old_time, old_time))
    else:
        marker.write_text("{not json", encoding="utf-8")

    before = marker.read_bytes()

    state = cluster._detect_data_state(segment)

    assert state["screen"] == expected_state
    assert marker.exists()
    assert marker.read_bytes() == before
    assert not failed.exists()


def test_scan_day_detects_analyzing_markers_from_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    source = Path("tests/fixtures/journal/chronicle/20260520")
    dest = day_path("20260520")
    shutil.copytree(source, dest, dirs_exist_ok=True)
    # Normalize all analyzing markers to "now" so wall-clock elapsed since the
    # fixture was checked out doesn't push fresh markers over the staleness
    # threshold; then explicitly stale only the one this test exercises.
    now = time.time()
    for marker in dest.rglob(".analyzing_*"):
        os.utime(marker, (now, now))
    stale_marker = dest / "default" / "093000_300" / ".analyzing_screen"
    old_time = now - 2000
    os.utime(stale_marker, (old_time, old_time))

    mod = importlib.import_module("solstone.think.cluster")

    _audio_ranges, _screen_ranges, segments = mod.scan_day("20260520")
    by_key = {segment["key"]: segment for segment in segments}

    assert by_key["090000_300"]["data_state"]["screen"] == "analyzing"
    assert by_key["091000_300"]["data_state"]["screen"] == "failed"
    assert by_key["092000_300"]["data_state"]["screen"] == "analyzed"
    assert (dest / "default" / "092000_300" / ".analyzing_screen").exists()
    assert by_key["093000_300"]["data_state"]["screen"] == "failed"
    assert (dest / "default" / "093000_300" / ".analyzing_screen").exists()
    assert not (dest / "default" / "093000_300" / ".analyze_failed_screen").exists()
    assert by_key["094000_300"]["data_state"] == {
        "audio": "pending",
        "screen": "pending",
    }


def test_scan_day_marks_analyzed_audio_analyzed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    segment = day_dir / "default" / "090000_300"
    segment.mkdir(parents=True)
    (segment / "audio.jsonl").write_text(
        '{"raw": "audio.flac"}\n'
        '{"start": "00:00:01", "source": "mic", "text": "audio line"}\n'
    )

    audio_ranges, _, segments = mod.scan_day("20240101")

    assert audio_ranges == [("09:00", "09:15")]
    assert segments[0]["data_state"] == {"audio": "analyzed"}


def test_scan_day_keeps_audio_raw_substring_collision_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    segment = day_dir / "default" / "090000_300"
    segment.mkdir(parents=True)
    (segment / "audio.jsonl").write_text('{"raw": "startup_audio.flac"}\n')

    audio_ranges, _, segments = mod.scan_day("20240101")

    assert audio_ranges == [("09:00", "09:15")]
    assert segments[0]["data_state"] == {"audio": "pending"}


def test_scan_day_omits_absent_modalities(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    segment = day_dir / "default" / "090000_300"
    segment.mkdir(parents=True)

    assert mod.scan_day("20240101") == ([], [], [])


@pytest.mark.parametrize("filename", ["imported.md", "call_transcript.md"])
def test_scan_day_marks_text_transcript_audio_analyzed(tmp_path, monkeypatch, filename):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    segment = day_dir / "default" / "090000_300"
    segment.mkdir(parents=True)
    (segment / filename).write_text("transcript text\n")

    audio_ranges, _, segments = mod.scan_day("20240101")

    assert audio_ranges == [("09:00", "09:15")]
    assert segments[0]["data_state"] == {"audio": "analyzed"}


def test_scan_day_marks_markdown_only_health_segment_as_markdown(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    segment = day_dir / "import.apple_health" / "090000_300"
    segment.mkdir(parents=True)
    (segment / "day_summary_transcript.md").write_text("health summary\n")

    audio_ranges, screen_ranges, segments = mod.scan_day("20240101")

    assert audio_ranges == []
    assert screen_ranges == []
    assert segments == [
        {
            "key": "090000_300",
            "start": "09:00",
            "end": "09:05",
            "types": ["markdown"],
            "stream": "import.apple_health",
            "data_state": {"markdown": "analyzed"},
        }
    ]
    assert mod.read_segment_data_state(
        "20240101", "090000_300", "import.apple_health"
    ) == {"markdown": "analyzed"}


@pytest.mark.parametrize(
    "stream",
    ["import.kindle", "import.ics", "import.obsidian", "import.document"],
)
def test_scan_day_marks_ordinary_markdown_import_as_audio(
    tmp_path, monkeypatch, stream
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    segment = day_dir / stream / "090000_300"
    segment.mkdir(parents=True)
    (segment / "note_transcript.md").write_text("owner note\n")

    audio_ranges, screen_ranges, segments = mod.scan_day("20240101")

    # Health day cards are derived summaries; ordinary markdown imports are
    # source content that must still produce sense/entities/facets.
    assert audio_ranges == [("09:00", "09:15")]
    assert screen_ranges == []
    assert segments == [
        {
            "key": "090000_300",
            "start": "09:00",
            "end": "09:05",
            "types": ["audio"],
            "stream": stream,
            "data_state": {"audio": "analyzed"},
        }
    ]
    assert mod.read_segment_data_state("20240101", "090000_300", stream) == {
        "audio": "analyzed"
    }


def test_scan_day_does_not_exclude_unregistered_health_like_stream(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day_dir = day_path("20240101")

    mod = importlib.import_module("solstone.think.cluster")

    stream = "import.health_summary"
    segment = day_dir / stream / "090000_300"
    segment.mkdir(parents=True)
    (segment / "day_summary_transcript.md").write_text("health-looking summary\n")

    assert not mod._is_markdown_only_health_segment(stream, segment)
    audio_ranges, screen_ranges, segments = mod.scan_day("20240101")

    assert audio_ranges == [("09:00", "09:15")]
    assert screen_ranges == []
    assert segments[0]["stream"] == stream
    assert segments[0]["data_state"] == {"audio": "analyzed"}


def test_day_path_create_false(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    missing = day_path("29990101", create=False)
    assert not missing.exists()

    created = day_path("29990101")
    assert created.exists()


def test_find_segment_dir_missing_streamed_segment_does_not_create_directory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    mod = importlib.import_module("solstone.think.cluster")
    result = mod._find_segment_dir("29990101", "090000_300", "default")

    assert result is None
    assert not (tmp_path / "chronicle" / "29990101").exists()


def test_scan_day_keeps_document_import_segment_as_media(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day = "20990415"
    seg_dir = tmp_path / "chronicle" / day / "import.document" / "120000_300"
    seg_dir.mkdir(parents=True)
    (seg_dir / "document_transcript.md").write_text(
        "# Imported Document\n\nSome extracted text.\n", encoding="utf-8"
    )
    (seg_dir / "original.pdf").write_bytes(b"%PDF-1.4 synthetic")

    from solstone.think.cluster import scan_day

    _, _, segments = scan_day(day)

    assert len(segments) == 1
    assert segments[0]["types"] != ["markdown"]
    assert "markdown" not in segments[0]["data_state"]
