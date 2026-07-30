# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Utility functions for import operations.

This module contains reusable logic for managing imports in the journal,
extracted from apps/import/routes.py to be usable in CLI tools and other contexts.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solstone.think.importers.shared import (
    PRIVATE_IMPORT_FILE_MODE,
    ensure_private_import_dir,
)
from solstone.think.journal_io import atomic_replace
from solstone.think.utils import resolve_journal_path

IMPORT_TASK_TIMEOUT_SECONDS = 3600


@dataclass(frozen=True)
class ImportStatusResolution:
    status: str
    error: Any | None
    error_stage: Any | None


# ============================================================================
# File Operations
# ============================================================================


def save_import_file(
    journal_root: Path,
    timestamp: str,
    source_path: Path,
    filename: str,
) -> Path:
    """Copy/move file into imports/{timestamp}/ directory.

    Args:
        journal_root: Root journal directory
        timestamp: Import timestamp (YYYYMMDD_HHMMSS format)
        source_path: Path to source file
        filename: Desired filename in import directory

    Returns:
        Final file path where file was saved
    """
    # Create import folder structure: imports/<timestamp>/<filename>
    import_dir = journal_root / "imports" / timestamp
    ensure_private_import_dir(import_dir)

    # Save the file
    file_path = import_dir / filename
    if source_path != file_path:
        # Copy content if different paths
        atomic_replace(
            file_path, source_path.read_bytes(), mode=PRIVATE_IMPORT_FILE_MODE
        )

    return file_path


def save_import_text(
    journal_root: Path,
    timestamp: str,
    content: str,
    filename: str,
) -> Path:
    """Save text content to imports/{timestamp}/ directory.

    Args:
        journal_root: Root journal directory
        timestamp: Import timestamp (YYYYMMDD_HHMMSS format)
        content: Text content to save
        filename: Desired filename in import directory

    Returns:
        Final file path where content was saved
    """
    # Create import folder structure: imports/<timestamp>/<filename>
    import_dir = journal_root / "imports" / timestamp
    ensure_private_import_dir(import_dir)

    # Save the text
    file_path = import_dir / filename
    atomic_replace(file_path, content, mode=PRIVATE_IMPORT_FILE_MODE)

    return file_path


def move_import(
    journal_root: Path,
    old_timestamp: str,
    new_timestamp: str,
) -> Path:
    """Atomically move an import staging directory to a new timestamp.

    Both directories live under ``imports/`` on the same filesystem, so the
    move is a single atomic ``rename``.

    Args:
        journal_root: Root journal directory
        old_timestamp: Current import timestamp directory name
        new_timestamp: Destination import timestamp directory name

    Returns:
        Path to the moved (destination) import directory.

    Raises:
        FileNotFoundError: If the source import directory does not exist.
        FileExistsError: If the destination import directory already exists.
    """
    old_dir = journal_root / "imports" / old_timestamp
    new_dir = journal_root / "imports" / new_timestamp

    if not old_dir.exists():
        raise FileNotFoundError(f"Import directory not found for {old_timestamp}")
    if new_dir.exists():
        raise FileExistsError(f"Import already exists for timestamp {new_timestamp}")

    old_dir.rename(new_dir)
    ensure_private_import_dir(new_dir)
    return new_dir


# ============================================================================
# Metadata Operations
# ============================================================================


def write_import_metadata(
    journal_root: Path,
    timestamp: str,
    metadata: dict,
) -> None:
    """Write import.json with provided metadata dict.

    Args:
        journal_root: Root journal directory
        timestamp: Import timestamp
        metadata: Metadata dictionary to write
    """
    import_dir = journal_root / "imports" / timestamp
    ensure_private_import_dir(import_dir)
    metadata_path = import_dir / "import.json"
    atomic_replace(
        metadata_path,
        json.dumps(metadata, indent=2),
        mode=PRIVATE_IMPORT_FILE_MODE,
    )


def read_import_metadata(
    journal_root: Path,
    timestamp: str,
) -> dict:
    """Read import.json for an import.

    Args:
        journal_root: Root journal directory
        timestamp: Import timestamp

    Returns:
        Metadata dictionary

    Raises:
        FileNotFoundError: If import metadata not found
    """
    import_dir = journal_root / "imports" / timestamp
    metadata_path = import_dir / "import.json"

    if metadata_path.exists():
        return json.loads(metadata_path.read_text(encoding="utf-8"))

    raise FileNotFoundError(f"Import metadata not found for {timestamp}")


def find_staged_by_client_item_id(
    journal_root: Path, client_item_id: str
) -> dict | None:
    """Return staged import metadata with matching client_item_id, else None."""
    for timestamp in list_import_timestamps(journal_root):
        try:
            metadata = read_import_metadata(journal_root, timestamp)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        if metadata.get("client_item_id") == client_item_id:
            return read_import_status_info(journal_root, timestamp, metadata)

    return None


def find_staged_by_source_hash(
    journal_root: Path,
    source_hash: str,
    *,
    exclude_timestamp: str | None = None,
) -> dict | None:
    """Return staged import metadata with matching source_hash, else None."""
    for timestamp in list_import_timestamps(journal_root):
        if timestamp == exclude_timestamp:
            continue
        try:
            metadata = read_import_metadata(journal_root, timestamp)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        if metadata.get("source_hash") == source_hash:
            return read_import_status_info(journal_root, timestamp, metadata)

    return None


def update_import_metadata_fields(
    journal_root: Path,
    timestamp: str,
    updates: dict,
) -> tuple[dict, bool]:
    """Update specific fields in import.json.

    Args:
        journal_root: Root journal directory
        timestamp: Import timestamp
        updates: Dict of fields to update (e.g., {"facet": "foo", "setting": "bar"})

    Returns:
        Tuple of (updated_metadata, was_modified)

    Raises:
        FileNotFoundError: If import metadata not found
    """
    import_dir = journal_root / "imports" / timestamp
    metadata_path = import_dir / "import.json"

    if not metadata_path.exists():
        raise FileNotFoundError(f"Import metadata not found for {timestamp}")

    # Read current metadata
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    # Track if anything changed
    updated = False

    # Update each field
    for key, value in updates.items():
        # Check if field is missing or value changed
        field_missing = key not in metadata
        value_changed = metadata.get(key) != value

        if field_missing or value_changed:
            metadata[key] = value
            updated = True

    # Write back if modified
    if updated:
        atomic_replace(
            metadata_path,
            json.dumps(metadata, indent=2),
            mode=PRIVATE_IMPORT_FILE_MODE,
        )

    return metadata, updated


# ============================================================================
# Reading Processing Results
# ============================================================================


def read_imported_results(
    journal_root: Path,
    timestamp: str,
) -> dict | None:
    """Read imported.json if exists, else None.

    Args:
        journal_root: Root journal directory
        timestamp: Import timestamp

    Returns:
        Imported results dict or None if not found
    """
    import_dir = journal_root / "imports" / timestamp
    imported_json = import_dir / "imported.json"

    if not imported_json.exists():
        return None

    try:
        with open(imported_json, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ============================================================================
# Scanning and Status Logic
# ============================================================================


def read_import_status_info(
    journal_root: Path,
    timestamp: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict:
    """Return import.json data plus the status fields supplied by imported.json."""
    import_dir = journal_root / "imports" / timestamp
    import_data = dict(
        metadata
        if metadata is not None
        else read_import_metadata(journal_root=journal_root, timestamp=timestamp)
    )
    import_data["timestamp"] = timestamp
    import_data["imported_at"] = import_dir.stat().st_ctime
    if "upload_timestamp" in import_data:
        import_data["imported_at"] = import_data["upload_timestamp"] / 1000

    import_data["processed"] = False
    import_data["error"] = None
    import_data["error_stage"] = None
    imported_meta = read_imported_results(journal_root, timestamp)
    if imported_meta is not None:
        import_data["processed"] = True
        import_data["error"] = imported_meta.get("error")
        import_data["error_stage"] = imported_meta.get("error_stage")
        if "processing_completed" in imported_meta:
            import_data["processing_completed"] = imported_meta.get(
                "processing_completed"
            )

    return import_data


def resolve_import_status(
    import_data: Mapping[str, Any],
    *,
    now: float | None = None,
    timeout_seconds: int = IMPORT_TASK_TIMEOUT_SECONDS,
) -> ImportStatusResolution:
    """Resolve the user-facing import status from merged import metadata."""
    required = ("imported_at", "processed", "error", "error_stage")
    missing = [key for key in required if key not in import_data]
    if missing:
        raise ValueError(
            "resolve_import_status requires merged import info with keys: "
            + ", ".join(missing)
        )

    error = import_data["error"]
    error_stage = import_data["error_stage"]
    if error:
        return ImportStatusResolution("failed", error, error_stage)

    if import_data["processed"] or import_data.get("processing_completed"):
        return ImportStatusResolution("success", error, error_stage)

    task_id = import_data.get("task_id")
    if task_id:
        current_time = time.time() if now is None else now
        import_age_seconds = current_time - float(import_data["imported_at"])
        if import_age_seconds > timeout_seconds:
            return ImportStatusResolution(
                "failed",
                "Import never completed",
                "timeout",
            )
        return ImportStatusResolution("running", error, error_stage)

    return ImportStatusResolution("pending", error, error_stage)


def list_import_timestamps(
    journal_root: Path,
) -> list[str]:
    """Get all valid import timestamps from imports/ directory.

    Args:
        journal_root: Root journal directory

    Returns:
        List of timestamp strings (YYYYMMDD_HHMMSS format)
    """
    imports_dir = journal_root / "imports"

    if not imports_dir.exists():
        return []

    timestamps = []
    for import_folder in imports_dir.iterdir():
        if not import_folder.is_dir():
            continue

        # Skip if it's not a timestamp folder (YYYYMMDD_HHMMSS format)
        if not (import_folder.name.count("_") == 1 and len(import_folder.name) == 15):
            continue

        timestamps.append(import_folder.name)

    return timestamps


def calculate_duration_from_files(
    files: list[str],
) -> int | None:
    """Calculate duration in minutes from imported file timestamps.

    Expects filenames like "120000_imported_audio.jsonl"
    Extracts HHMMSS, calculates start-to-end duration.

    Args:
        files: List of file paths

    Returns:
        Duration in minutes, or None if can't calculate
    """
    if not files:
        return None

    timestamps = []
    for file in files:
        # Extract timestamp from filename like "120000_imported_audio.jsonl"
        basename = Path(file).name
        if basename[:6].isdigit():
            timestamps.append(basename[:6])

    if not timestamps:
        return None

    timestamps.sort()
    start_time = timestamps[0]
    end_time = timestamps[-1]

    # Convert to minutes
    start_h, start_m = int(start_time[:2]), int(start_time[2:4])
    end_h, end_m = int(end_time[:2]), int(end_time[2:4])
    duration_minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)

    if duration_minutes > 0:
        return duration_minutes

    return None


def build_import_info(
    journal_root: Path,
    timestamp: str,
) -> dict:
    """Build complete info dict for one import.

    Args:
        journal_root: Root journal directory
        timestamp: Import timestamp

    Returns:
        Dict with all import information (without status - caller adds that)
    """
    import_dir = journal_root / "imports" / timestamp

    import_data = {
        "timestamp": timestamp,
        "created_at": import_dir.stat().st_ctime,
        "imported_at": import_dir.stat().st_ctime,  # Default, may be overridden
    }

    # Read import.json if it exists
    import_json = import_dir / "import.json"
    task_id = None
    if import_json.exists():
        try:
            with open(import_json, "r", encoding="utf-8") as f:
                import_meta = json.load(f)
                import_data["original_filename"] = import_meta.get(
                    "original_filename", "Unknown"
                )
                import_data["file_size"] = import_meta.get("file_size", 0)
                import_data["mime_type"] = import_meta.get("mime_type", "")
                import_data["facet"] = import_meta.get("facet")
                import_data["setting"] = import_meta.get("setting")
                import_data["user_timestamp"] = import_meta.get("user_timestamp")
                import_data["imported_via"] = import_meta.get("imported_via")
                import_data["link_id"] = import_meta.get("link_id")
                import_data["observer_handle"] = import_meta.get("observer_handle")
                task_id = import_meta.get("task_id")
                import_data["task_id"] = task_id
                # Use upload_timestamp if available for better sorting
                if "upload_timestamp" in import_meta:
                    import_data["imported_at"] = (
                        import_meta["upload_timestamp"] / 1000
                    )  # Convert ms to seconds
        except Exception:
            pass

    # Read imported.json if it exists (processing results)
    import_data["processed"] = False
    import_data["error"] = None
    import_data["error_stage"] = None
    imported_json = import_dir / "imported.json"
    if imported_json.exists():
        try:
            with open(imported_json, "r", encoding="utf-8") as f:
                imported_meta = json.load(f)
                import_data["processed"] = True
                import_data["total_files_created"] = imported_meta.get(
                    "total_files_created", 0
                )
                import_data["target_day"] = imported_meta.get("target_day")
                import_data["source_type"] = imported_meta.get("source_type")
                import_data["source_display"] = imported_meta.get("source_display")
                import_data["entries_written"] = imported_meta.get("entries_written")
                import_data["entities_seeded"] = imported_meta.get("entities_seeded")
                import_data["date_range"] = imported_meta.get("date_range")

                # Check for error state
                if "error" in imported_meta:
                    import_data["error"] = imported_meta.get("error")
                    import_data["error_stage"] = imported_meta.get("error_stage")

                # Calculate duration from imported files
                if imported_meta.get("all_created_files"):
                    duration = calculate_duration_from_files(
                        imported_meta["all_created_files"]
                    )
                    if duration:
                        import_data["duration_minutes"] = duration
        except Exception:
            pass

    return import_data


# ============================================================================
# Detail View
# ============================================================================


def get_import_details(
    journal_root: Path,
    timestamp: str,
) -> dict:
    """Get all metadata files for import detail view.

    Args:
        journal_root: Root journal directory
        timestamp: Import timestamp

    Returns:
        Dict with all detail information

    Raises:
        FileNotFoundError: If import directory doesn't exist
    """
    import_dir = journal_root / "imports" / timestamp
    if not import_dir.exists():
        raise FileNotFoundError(f"Import not found: {timestamp}")

    result = {
        "timestamp": timestamp,
        "import_json": None,
        "imported_json": None,
    }

    # Read import.json
    import_json_path = import_dir / "import.json"
    if import_json_path.exists():
        try:
            with open(import_json_path, "r", encoding="utf-8") as f:
                result["import_json"] = json.load(f)
        except Exception:
            pass

    # Read imported.json
    imported_json_path = import_dir / "imported.json"
    if imported_json_path.exists():
        try:
            with open(imported_json_path, "r", encoding="utf-8") as f:
                result["imported_json"] = json.load(f)
        except Exception:
            pass

    # Read segments.json
    segments_json_path = import_dir / "segments.json"
    if segments_json_path.exists():
        try:
            with open(segments_json_path, "r", encoding="utf-8") as f:
                result["segments_json"] = json.load(f)
        except Exception:
            pass

    imported_json = result.get("imported_json")
    if (
        isinstance(imported_json, dict)
        and imported_json.get("merge_summary") is not None
    ):
        merge_log_path = imported_json.get("merge_log_path")
        merge_staging_path = imported_json.get("merge_staging_path")
        if merge_log_path and merge_staging_path:
            result["merge_artifact_paths"] = {
                "decisions": merge_log_path,
                "staging": merge_staging_path,
            }
            decision_highlights = _load_decision_highlights(Path(merge_log_path))
            if decision_highlights is not None:
                result["decision_highlights"] = decision_highlights

        summary_errors = imported_json.get("summary_errors")
        if isinstance(summary_errors, list) and summary_errors:
            result["summary_errors"] = summary_errors

    return result


def _load_decision_highlights(decisions_path: Path) -> dict | None:
    """Load selected decision-log rows for detail-view highlights."""
    if not decisions_path.exists():
        return None

    staged_entities: list[dict[str, str]] = []
    errored_segments: list[dict[str, str]] = []
    qualifying_rows = 0

    try:
        with open(decisions_path, "r", encoding="utf-8") as handle:
            for line in handle:
                if qualifying_rows >= 50:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue

                action = row.get("action")
                if action == "entity_staged":
                    staged_entities.append(
                        {
                            "source_name": row["source"]["name"],
                            "target_name": row["target"]["name"],
                            "staging_path": row["staging_path"],
                        }
                    )
                    qualifying_rows += 1
                elif action == "segment_errored":
                    errored_segments.append(
                        {
                            "item_id": row["item_id"],
                            "reason": row["reason"],
                        }
                    )
                    qualifying_rows += 1
    except FileNotFoundError:
        return None

    if not staged_entities and not errored_segments:
        return None
    return {
        "staged_entities": staged_entities,
        "errored_segments": errored_segments,
    }


def _backfill_item_type(source_type: str) -> str:
    """Map source_type to manifest item type for backfill."""
    return {
        "ics": "event",
        "kindle": "highlight_group",
        "obsidian": "note",
    }.get(source_type, "conversation")


def generate_content_manifest(journal_root: Path, timestamp: str) -> Path | None:
    """Generate content_manifest.jsonl by backfilling from segment files."""
    import_dir = journal_root / "imports" / timestamp
    imported_path = import_dir / "imported.json"
    if not imported_path.exists():
        return None

    imported = json.loads(imported_path.read_text(encoding="utf-8"))
    source_type = imported.get("source_type", "")
    all_files = imported.get("all_created_files", [])

    entries: list[dict] = []
    entry_idx = 0

    for file_path_str in all_files:
        file_path = Path(file_path_str)
        if not file_path.exists():
            file_path = resolve_journal_path(journal_root, file_path_str)
            if not file_path.exists():
                continue

        parts = file_path.parts
        try:
            seg_key = parts[-2]
            day = parts[-4] if len(parts) >= 4 else ""
            if not (len(day) == 8 and day.isdigit()):
                day = parts[-3] if len(parts) >= 3 else ""
                if not (len(day) == 8 and day.isdigit()):
                    day = ""
        except (IndexError, ValueError):
            seg_key = ""
            day = ""

        segment = {"day": day, "key": seg_key} if day and seg_key else {}

        if file_path.suffix == ".jsonl":
            try:
                lines = file_path.read_text(encoding="utf-8").strip().split("\n")
                if len(lines) < 2:
                    continue
                header = json.loads(lines[0])
                topic = header.get("topics", "")
                messages = []
                for line in lines[1:]:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                if not messages:
                    continue
                preview = ""
                for message in messages:
                    if message.get("speaker") == "Human":
                        preview = message.get("text", "")[:200]
                        break
                entries.append(
                    {
                        "id": f"seg-{entry_idx}",
                        "title": topic or preview[:80] or "Conversation segment",
                        "date": day,
                        "type": "conversation",
                        "preview": preview,
                        "meta": {"message_count": len(messages)},
                        "segments": [segment] if segment else [],
                    }
                )
                entry_idx += 1
            except (OSError, json.JSONDecodeError):
                continue
        elif file_path.suffix == ".md":
            try:
                content = file_path.read_text(encoding="utf-8")
            except OSError:
                continue
            sections = re.split(r"(?m)^## ", content)
            for section in sections:
                section = section.strip()
                if not section:
                    continue
                title_line = section.split("\n", 1)[0].strip()
                body = section.split("\n", 1)[1].strip() if "\n" in section else ""
                entries.append(
                    {
                        "id": f"item-{entry_idx}",
                        "title": title_line,
                        "date": day,
                        "type": _backfill_item_type(source_type),
                        "preview": body[:200],
                        "meta": {},
                        "segments": [segment] if segment else [],
                    }
                )
                entry_idx += 1

    if not entries:
        return None

    manifest_path = import_dir / "content_manifest.jsonl"
    lines = [json.dumps(entry) for entry in entries]
    atomic_replace(
        manifest_path,
        "\n".join(lines) + "\n" if lines else "",
        mode=PRIVATE_IMPORT_FILE_MODE,
    )
    return manifest_path


# ============================================================================
# Segment Tracking
# ============================================================================


def save_import_segments(
    journal_root: Path,
    timestamp: str,
    segments: list[str],
    day: str,
) -> None:
    """Save segment list for an import.

    Args:
        journal_root: Root journal directory
        timestamp: Import timestamp (YYYYMMDD_HHMMSS format)
        segments: List of segment keys (HHMMSS_LEN format)
        day: Day string (YYYYMMDD format)
    """
    import_dir = journal_root / "imports" / timestamp
    ensure_private_import_dir(import_dir)

    segments_path = import_dir / "segments.json"
    data = {
        "segments": segments,
        "day": day,
    }
    atomic_replace(
        segments_path,
        json.dumps(data, indent=2),
        mode=PRIVATE_IMPORT_FILE_MODE,
    )


def load_import_segments(
    journal_root: Path,
    timestamp: str,
) -> tuple[list[str], str] | None:
    """Load segment list for an import.

    Args:
        journal_root: Root journal directory
        timestamp: Import timestamp

    Returns:
        Tuple of (segments_list, day) or None if not found
    """
    import_dir = journal_root / "imports" / timestamp
    segments_path = import_dir / "segments.json"

    if not segments_path.exists():
        return None

    try:
        data = json.loads(segments_path.read_text(encoding="utf-8"))
        return data.get("segments", []), data.get("day", "")
    except Exception:
        return None
