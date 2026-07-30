# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for think.importers.utils module."""

import json
import tempfile
from pathlib import Path

import pytest

from solstone.think.importers.utils import (
    IMPORT_TASK_TIMEOUT_SECONDS,
    _load_decision_highlights,
    build_import_info,
    calculate_duration_from_files,
    get_import_details,
    list_import_timestamps,
    load_import_segments,
    move_import,
    read_import_metadata,
    read_import_status_info,
    read_imported_results,
    resolve_import_status,
    save_import_file,
    save_import_segments,
    save_import_text,
    update_import_metadata_fields,
    write_import_metadata,
)


@pytest.fixture
def temp_journal():
    """Create a temporary journal directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        journal_path = Path(tmpdir) / "journal"
        journal_path.mkdir()
        yield journal_path


def test_save_import_file(temp_journal):
    """Test saving an import file."""
    # Create a source file
    source = temp_journal / "source.txt"
    source.write_text("test content", encoding="utf-8")

    # Save to import
    result_path = save_import_file(
        journal_root=temp_journal,
        timestamp="20250101_120000",
        source_path=source,
        filename="imported.txt",
    )

    # Verify it was saved correctly
    assert result_path.exists()
    assert result_path.read_text(encoding="utf-8") == "test content"
    assert result_path.parent.name == "20250101_120000"
    assert result_path.name == "imported.txt"


def test_save_import_text(temp_journal):
    """Test saving text content as import."""
    result_path = save_import_text(
        journal_root=temp_journal,
        timestamp="20250101_130000",
        content="Hello world",
        filename="paste.txt",
    )

    assert result_path.exists()
    assert result_path.read_text(encoding="utf-8") == "Hello world"
    assert result_path.parent.name == "20250101_130000"


def test_move_import_success(temp_journal):
    """Test moving an import directory to a new timestamp."""
    old_timestamp = "20250101_120000"
    new_timestamp = "20250101_121500"
    old_dir = temp_journal / "imports" / old_timestamp
    old_dir.mkdir(parents=True)
    (old_dir / "sample.txt").write_text("test content", encoding="utf-8")

    result = move_import(temp_journal, old_timestamp, new_timestamp)

    new_dir = temp_journal / "imports" / new_timestamp
    assert result == new_dir
    assert new_dir.exists()
    assert (new_dir / "sample.txt").read_text(encoding="utf-8") == "test content"
    assert not old_dir.exists()


def test_move_import_missing_source(temp_journal):
    """Test moving a missing import directory raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        move_import(temp_journal, "20250101_120000", "20250101_121500")


def test_move_import_target_exists(temp_journal):
    """Test moving over an existing import directory raises FileExistsError."""
    old_timestamp = "20250101_120000"
    new_timestamp = "20250101_121500"
    (temp_journal / "imports" / old_timestamp).mkdir(parents=True)
    (temp_journal / "imports" / new_timestamp).mkdir(parents=True)

    with pytest.raises(FileExistsError):
        move_import(temp_journal, old_timestamp, new_timestamp)


def test_write_and_read_import_metadata(temp_journal):
    """Test writing and reading import metadata."""
    timestamp = "20250101_140000"
    metadata = {
        "original_filename": "test.txt",
        "upload_timestamp": 1234567890000,
        "file_size": 1024,
        "facet": "work",
    }

    # Write metadata
    write_import_metadata(
        journal_root=temp_journal,
        timestamp=timestamp,
        metadata=metadata,
    )

    # Read it back
    read_metadata = read_import_metadata(
        journal_root=temp_journal,
        timestamp=timestamp,
    )

    assert read_metadata == metadata


def test_read_import_metadata_not_found(temp_journal):
    """Test reading metadata when it doesn't exist."""
    with pytest.raises(FileNotFoundError):
        read_import_metadata(
            journal_root=temp_journal,
            timestamp="20250101_999999",
        )


def test_update_import_metadata_fields(temp_journal):
    """Test updating specific metadata fields."""
    timestamp = "20250101_150000"

    # Create initial metadata
    initial = {"original_filename": "test.txt", "facet": None}
    write_import_metadata(temp_journal, timestamp, initial)

    # Update fields
    updated_metadata, was_modified = update_import_metadata_fields(
        journal_root=temp_journal,
        timestamp=timestamp,
        updates={"facet": "personal", "setting": "home office"},
    )

    assert was_modified is True
    assert updated_metadata["facet"] == "personal"
    assert updated_metadata["setting"] == "home office"
    assert updated_metadata["original_filename"] == "test.txt"

    # Update with same values
    updated_metadata2, was_modified2 = update_import_metadata_fields(
        journal_root=temp_journal,
        timestamp=timestamp,
        updates={"facet": "personal", "setting": "home office"},
    )

    assert was_modified2 is False


def test_read_imported_results(temp_journal):
    """Test reading imported.json results."""
    timestamp = "20250101_160000"
    import_dir = temp_journal / "imports" / timestamp
    import_dir.mkdir(parents=True)

    # Create imported.json
    results = {
        "processed_timestamp": timestamp,
        "total_files_created": 5,
        "target_day": "20250101",
    }
    (import_dir / "imported.json").write_text(json.dumps(results), encoding="utf-8")

    # Read it
    read_results = read_imported_results(temp_journal, timestamp)
    assert read_results == results

    # Test when it doesn't exist
    read_results_none = read_imported_results(temp_journal, "20250101_999999")
    assert read_results_none is None


def test_list_import_timestamps(temp_journal):
    """Test listing all import timestamps."""
    # Create some import folders
    (temp_journal / "imports" / "20250101_120000").mkdir(parents=True)
    (temp_journal / "imports" / "20250101_130000").mkdir(parents=True)
    (temp_journal / "imports" / "20250101_140000").mkdir(parents=True)

    # Create invalid folder (should be ignored)
    (temp_journal / "imports" / "invalid").mkdir(parents=True)

    timestamps = list_import_timestamps(temp_journal)

    assert len(timestamps) == 3
    assert "20250101_120000" in timestamps
    assert "20250101_130000" in timestamps
    assert "20250101_140000" in timestamps
    assert "invalid" not in timestamps


def test_list_import_timestamps_empty(temp_journal):
    """Test listing when no imports exist."""
    timestamps = list_import_timestamps(temp_journal)
    assert timestamps == []

    # Create imports dir but leave it empty
    (temp_journal / "imports").mkdir()
    timestamps = list_import_timestamps(temp_journal)
    assert timestamps == []


def test_calculate_duration_from_files():
    """Test calculating duration from imported file timestamps."""
    files = [
        "/path/to/120000_imported_audio.jsonl",
        "/path/to/120500_imported_audio.jsonl",
        "/path/to/121000_imported_audio.jsonl",
        "/path/to/123000_imported_audio.jsonl",
    ]

    duration = calculate_duration_from_files(files)
    assert duration == 30  # 12:00 to 12:30 = 30 minutes

    # Test with single file
    duration_single = calculate_duration_from_files([files[0]])
    assert duration_single is None

    # Test with empty list
    duration_empty = calculate_duration_from_files([])
    assert duration_empty is None

    # Test with files without timestamps
    duration_no_ts = calculate_duration_from_files(["file.txt", "other.jsonl"])
    assert duration_no_ts is None


def test_build_import_info(temp_journal):
    """Test building complete import info."""
    timestamp = "20250101_190000"
    import_dir = temp_journal / "imports" / timestamp
    import_dir.mkdir(parents=True)

    # Create import.json
    import_metadata = {
        "original_filename": "recording.m4a",
        "file_size": 2048000,
        "mime_type": "audio/m4a",
        "upload_timestamp": 1704124800000,
        "facet": "work",
        "setting": "meeting",
    }
    (import_dir / "import.json").write_text(
        json.dumps(import_metadata), encoding="utf-8"
    )

    # Create imported.json
    imported_results = {
        "total_files_created": 3,
        "target_day": "20250101",
        "all_created_files": [
            "/path/190000_imported_audio.jsonl",
            "/path/190500_imported_audio.jsonl",
            "/path/191000_imported_audio.jsonl",
        ],
    }
    (import_dir / "imported.json").write_text(
        json.dumps(imported_results), encoding="utf-8"
    )

    # Build info
    info = build_import_info(temp_journal, timestamp)

    assert info["timestamp"] == timestamp
    assert info["original_filename"] == "recording.m4a"
    assert info["file_size"] == 2048000
    assert info["mime_type"] == "audio/m4a"
    assert info["facet"] == "work"
    assert info["setting"] == "meeting"
    assert info["processed"] is True
    assert info["total_files_created"] == 3
    assert info["target_day"] == "20250101"
    assert info["duration_minutes"] == 10  # 19:00 to 19:10


def test_resolve_import_status_all_states():
    """Resolve every import state from merged metadata."""
    now = 2_000.0
    recent = now - 10.0
    old = now - IMPORT_TASK_TIMEOUT_SECONDS - 1.0

    assert (
        resolve_import_status(
            {
                "imported_at": recent,
                "processed": True,
                "processing_completed": None,
                "error": None,
                "error_stage": None,
            },
            now=now,
        ).status
        == "success"
    )
    assert (
        resolve_import_status(
            {
                "imported_at": recent,
                "processed": False,
                "processing_completed": "2026-01-01T10:00:00",
                "error": None,
                "error_stage": None,
            },
            now=now,
        ).status
        == "success"
    )
    failed = resolve_import_status(
        {
            "imported_at": recent,
            "processed": True,
            "error": "bad import",
            "error_stage": "parse",
        },
        now=now,
    )
    assert failed.status == "failed"
    assert failed.error == "bad import"
    assert failed.error_stage == "parse"

    timed_out = resolve_import_status(
        {
            "imported_at": old,
            "processed": False,
            "error": None,
            "error_stage": None,
            "task_id": "task-old",
        },
        now=now,
    )
    assert timed_out.status == "failed"
    assert timed_out.error == "Import never completed"
    assert timed_out.error_stage == "timeout"

    assert (
        resolve_import_status(
            {
                "imported_at": recent,
                "processed": False,
                "error": None,
                "error_stage": None,
                "task_id": "task-recent",
            },
            now=now,
        ).status
        == "running"
    )
    assert (
        resolve_import_status(
            {
                "imported_at": recent,
                "processed": False,
                "error": None,
                "error_stage": None,
            },
            now=now,
        ).status
        == "pending"
    )


def test_resolve_import_status_error_wins_over_processed():
    error_payload = {"message": "failed after writing partial data"}
    resolution = resolve_import_status(
        {
            "imported_at": 2_000.0,
            "processed": True,
            "error": error_payload,
            "error_stage": "write",
        },
        now=2_100.0,
    )

    assert resolution.status == "failed"
    assert resolution.error == error_payload
    assert resolution.error_stage == "write"


def test_resolve_import_status_raw_import_json_raises():
    raw_import_json = {"task_id": "task-raw"}

    with pytest.raises(ValueError, match="requires merged import info"):
        resolve_import_status(raw_import_json, now=2_000.0)


def test_import_list_projection_matches_inline_derivation(temp_journal):
    """List-row status stays compatible with the previous inline derivation."""
    now = 10_000.0
    fixtures = {
        "20250101_190000": {
            "metadata": {
                "original_filename": "success.m4a",
                "file_size": 10,
                "mime_type": "audio/mp4",
                "upload_timestamp": 9_000_000,
                "facet": "work",
                "setting": "office",
                "user_timestamp": "20250101_190000",
                "imported_via": "web_dashboard",
                "link_id": None,
                "observer_handle": None,
            },
            "imported": {
                "total_files_created": 2,
                "target_day": "20250101",
                "source_type": "audio",
                "source_display": "audio",
                "entries_written": 3,
                "entities_seeded": 1,
                "date_range": ["20250101", "20250101"],
            },
            "expected_status": "success",
        },
        "20250101_191000": {
            "metadata": {
                "original_filename": "failed.m4a",
                "file_size": 11,
                "mime_type": "audio/mp4",
                "upload_timestamp": 9_010_000,
                "facet": None,
                "setting": None,
                "user_timestamp": "20250101_191000",
                "imported_via": "web_dashboard",
                "link_id": None,
                "observer_handle": None,
            },
            "imported": {
                "total_files_created": 0,
                "target_day": "20250101",
                "error": "parse failed",
                "error_stage": "parse",
            },
            "expected_status": "failed",
        },
        "20250101_192000": {
            "metadata": {
                "original_filename": "running.m4a",
                "file_size": 12,
                "mime_type": "audio/mp4",
                "upload_timestamp": int((now - 100.0) * 1000),
                "facet": None,
                "setting": None,
                "user_timestamp": "20250101_192000",
                "imported_via": "web_dashboard",
                "link_id": None,
                "observer_handle": None,
                "task_id": "task-recent",
            },
            "expected_status": "running",
        },
        "20250101_193000": {
            "metadata": {
                "original_filename": "timeout.m4a",
                "file_size": 13,
                "mime_type": "audio/mp4",
                "upload_timestamp": int(
                    (now - IMPORT_TASK_TIMEOUT_SECONDS - 10.0) * 1000
                ),
                "facet": None,
                "setting": None,
                "user_timestamp": "20250101_193000",
                "imported_via": "web_dashboard",
                "link_id": None,
                "observer_handle": None,
                "task_id": "task-old",
            },
            "expected_status": "failed",
            "expected_error": "Import never completed",
            "expected_error_stage": "timeout",
        },
        "20250101_194000": {
            "metadata": {
                "original_filename": "pending.m4a",
                "file_size": 14,
                "mime_type": "audio/mp4",
                "upload_timestamp": 9_040_000,
                "facet": None,
                "setting": None,
                "user_timestamp": "20250101_194000",
                "imported_via": "web_dashboard",
                "link_id": None,
                "observer_handle": None,
            },
            "expected_status": "pending",
        },
    }

    for timestamp, fixture in fixtures.items():
        import_dir = temp_journal / "imports" / timestamp
        import_dir.mkdir(parents=True)
        (import_dir / "import.json").write_text(
            json.dumps(fixture["metadata"]),
            encoding="utf-8",
        )
        if fixture.get("imported"):
            (import_dir / "imported.json").write_text(
                json.dumps(fixture["imported"]),
                encoding="utf-8",
            )

    projected = []
    for timestamp in list_import_timestamps(temp_journal):
        import_data = build_import_info(temp_journal, timestamp)
        resolution = resolve_import_status(import_data, now=now)
        import_data["status"] = resolution.status
        import_data["error"] = resolution.error
        import_data["error_stage"] = resolution.error_stage
        projected.append(import_data)

    by_timestamp = {item["timestamp"]: item for item in projected}
    for timestamp, fixture in fixtures.items():
        expected = {
            "timestamp": timestamp,
            "original_filename": fixture["metadata"]["original_filename"],
            "file_size": fixture["metadata"]["file_size"],
            "mime_type": fixture["metadata"]["mime_type"],
            "facet": fixture["metadata"]["facet"],
            "setting": fixture["metadata"]["setting"],
            "user_timestamp": fixture["metadata"]["user_timestamp"],
            "imported_via": fixture["metadata"]["imported_via"],
            "link_id": fixture["metadata"]["link_id"],
            "observer_handle": fixture["metadata"]["observer_handle"],
            "task_id": fixture["metadata"].get("task_id"),
            "processed": bool(fixture.get("imported")),
            "error": fixture.get("expected_error")
            if "expected_error" in fixture
            else (fixture.get("imported") or {}).get("error"),
            "error_stage": fixture.get("expected_error_stage")
            if "expected_error_stage" in fixture
            else (fixture.get("imported") or {}).get("error_stage"),
            "status": fixture["expected_status"],
        }
        if fixture.get("imported"):
            imported = fixture["imported"]
            expected.update(
                {
                    "total_files_created": imported.get("total_files_created", 0),
                    "target_day": imported.get("target_day"),
                    "source_type": imported.get("source_type"),
                    "source_display": imported.get("source_display"),
                    "entries_written": imported.get("entries_written"),
                    "entities_seeded": imported.get("entities_seeded"),
                    "date_range": imported.get("date_range"),
                }
            )

        actual = dict(by_timestamp[timestamp])
        actual.pop("created_at")
        actual.pop("imported_at")
        assert actual == expected


def test_read_import_status_info_merges_imported_json_only_for_matched_record(
    temp_journal,
):
    timestamp = "20250101_195000"
    import_dir = temp_journal / "imports" / timestamp
    import_dir.mkdir(parents=True)
    metadata = {
        "client_item_id": "matched",
        "upload_timestamp": 9_500_000,
        "task_id": "task-merged",
    }
    (import_dir / "import.json").write_text(json.dumps(metadata), encoding="utf-8")
    (import_dir / "imported.json").write_text(
        json.dumps({"processing_completed": "2026-01-01T19:50:00"}),
        encoding="utf-8",
    )

    info = read_import_status_info(temp_journal, timestamp, metadata)

    assert info["timestamp"] == timestamp
    assert info["imported_at"] == 9_500.0
    assert info["processed"] is True
    assert info["processing_completed"] == "2026-01-01T19:50:00"


def test_get_import_details(temp_journal):
    """Test getting all details for an import."""
    timestamp = "20250101_200000"
    import_dir = temp_journal / "imports" / timestamp
    import_dir.mkdir(parents=True)

    # Create all metadata files
    (import_dir / "import.json").write_text('{"file": "test.m4a"}', encoding="utf-8")
    (import_dir / "imported.json").write_text(
        '{"total_files_created": 2}', encoding="utf-8"
    )
    details = get_import_details(temp_journal, timestamp)

    assert details["timestamp"] == timestamp
    assert details["import_json"]["file"] == "test.m4a"
    assert details["imported_json"]["total_files_created"] == 2


def test_get_import_details_not_found(temp_journal):
    """Test getting details for non-existent import."""
    with pytest.raises(FileNotFoundError):
        get_import_details(temp_journal, "20250101_999999")


def test_save_and_load_import_segments(temp_journal):
    """Test saving and loading segment list for an import."""
    timestamp = "20250101_210000"
    segments = ["120000_300", "120500_300", "121000_300"]
    day = "20250101"

    # Save segments
    save_import_segments(temp_journal, timestamp, segments, day)

    # Load them back
    result = load_import_segments(temp_journal, timestamp)
    assert result is not None
    loaded_segments, loaded_day = result
    assert loaded_segments == segments
    assert loaded_day == day


def test_load_import_segments_not_found(temp_journal):
    """Test loading segments when file doesn't exist."""
    result = load_import_segments(temp_journal, "20250101_999999")
    assert result is None


def test_get_import_details_includes_segments(temp_journal):
    """Test that get_import_details includes segments.json."""
    timestamp = "20250101_220000"
    import_dir = temp_journal / "imports" / timestamp
    import_dir.mkdir(parents=True)

    # Create segments.json
    segments_data = {
        "segments": ["120000_300", "120500_300"],
        "day": "20250101",
    }
    (import_dir / "segments.json").write_text(
        json.dumps(segments_data), encoding="utf-8"
    )

    details = get_import_details(temp_journal, timestamp)
    assert details["segments_json"] == segments_data


def test_load_decision_highlights_filters_and_caps(temp_journal):
    decision_log = temp_journal / "journal.merge" / "run" / "decisions.jsonl"
    decision_log.parent.mkdir(parents=True)

    rows = [
        '{"action":"ignored","item_id":"skip-me"}',
        "not json at all",
    ]
    for idx in range(30):
        rows.append(
            json.dumps(
                {
                    "action": "entity_staged",
                    "item_id": f"entity-{idx}",
                    "source": {"name": f"Source {idx}"},
                    "target": {"name": f"Target {idx}"},
                    "staging_path": f"/tmp/staging/entity-{idx}/entity.json",
                }
            )
        )
    for idx in range(30):
        rows.append(
            json.dumps(
                {
                    "action": "segment_errored",
                    "item_id": f"20260101/default/{idx:06d}_300",
                    "reason": f"segment failure {idx}",
                }
            )
        )
    decision_log.write_text("\n".join(rows) + "\n", encoding="utf-8")

    highlights = _load_decision_highlights(decision_log)

    assert highlights is not None
    assert len(highlights["staged_entities"]) == 30
    assert len(highlights["errored_segments"]) == 20
    assert highlights["staged_entities"][0] == {
        "source_name": "Source 0",
        "target_name": "Target 0",
        "staging_path": "/tmp/staging/entity-0/entity.json",
    }
    assert highlights["errored_segments"][0] == {
        "item_id": "20260101/default/000000_300",
        "reason": "segment failure 0",
    }


def test_load_decision_highlights_returns_none_without_qualifying_rows(temp_journal):
    decision_log = temp_journal / "journal.merge" / "run" / "decisions.jsonl"
    decision_log.parent.mkdir(parents=True)
    decision_log.write_text(
        "\n".join(
            [
                '{"action":"segment_copied","item_id":"20260101/default/090000_300"}',
                "not json at all",
                '{"action":"entity_created","item_id":"source_person"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert _load_decision_highlights(decision_log) is None


def test_load_decision_highlights_propagates_non_missing_io_errors(
    temp_journal, monkeypatch
):
    decision_log = temp_journal / "journal.merge" / "run" / "decisions.jsonl"
    decision_log.parent.mkdir(parents=True)
    decision_log.write_text(
        json.dumps(
            {
                "action": "segment_errored",
                "item_id": "20260101/default/090000_300",
                "reason": "segment copy failed",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    real_open = open

    def broken_open(path, *args, **kwargs):
        if Path(path) == decision_log:
            raise PermissionError("permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", broken_open)

    with pytest.raises(PermissionError, match="permission denied"):
        _load_decision_highlights(decision_log)
