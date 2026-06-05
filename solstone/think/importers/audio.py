# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import datetime as dt
import logging
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

from solstone.observe.utils import find_available_segment
from solstone.think.importers.shared import find_manifest_by_hash, hash_source
from solstone.think.importers.sync import load_sync_state, save_sync_state
from solstone.think.media import AUDIO_EXTENSIONS

logger = logging.getLogger(__name__)


def slice_audio_segment(
    source_path: str,
    output_path: str,
    start_seconds: float,
    duration_seconds: float,
) -> str:
    """Extract an audio segment from source file, preserving original format.

    Uses stream copy for lossless extraction when possible.

    Args:
        source_path: Path to source audio file
        output_path: Path for output segment file
        start_seconds: Start offset in seconds
        duration_seconds: Duration to extract in seconds

    Returns:
        Output path on success

    Raises:
        subprocess.CalledProcessError: If ffmpeg fails
    """
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-ss",
        str(start_seconds),
        "-i",
        source_path,
        "-t",
        str(duration_seconds),
        "-vn",  # No video
        "-c:a",
        "copy",  # Stream copy for lossless extraction
        "-y",  # Overwrite output
        output_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError:
        # Fallback: re-encode if stream copy fails (some formats don't support it)
        logger.debug(f"Stream copy failed, re-encoding: {output_path}")
        cmd_reencode = [
            "ffmpeg",
            "-nostdin",
            "-ss",
            str(start_seconds),
            "-i",
            source_path,
            "-t",
            str(duration_seconds),
            "-vn",
            "-y",
            output_path,
        ]
        subprocess.run(cmd_reencode, check=True, capture_output=True, text=True)

    logger.info(f"Created audio segment: {output_path}")
    return output_path


def _get_audio_duration(audio_path: str) -> float | None:
    """Get audio duration in seconds using ffprobe.

    Args:
        audio_path: Path to audio file

    Returns:
        Duration in seconds, or None if unable to determine
    """
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError) as e:
        logger.warning(f"Could not determine audio duration: {e}")
        return None


def prepare_audio_segments(
    media_path: str,
    day_dir: str,
    base_dt: dt.datetime,
    import_id: str,
    stream: str,
) -> list[tuple[str, Path, list[str]]]:
    """Slice audio into 5-minute segments for observe pipeline.

    Creates segment directories with audio slices, ready for transcription
    via observe.observing events.

    Args:
        media_path: Path to source audio file
        day_dir: Day directory path (YYYYMMDD)
        base_dt: Base datetime for timestamp calculation
        import_id: Import identifier
        stream: Stream name for directory layout (day/stream/segment/)

    Returns:
        List of (segment_key, segment_dir, files_list) tuples
        where files_list contains the audio filename(s) created
    """
    media = Path(media_path)
    source_ext = media.suffix.lower()
    stream_dir = Path(day_dir) / stream

    # Get audio duration to calculate number of segments
    duration = _get_audio_duration(media_path)
    if duration is None:
        raise RuntimeError(f"Could not determine duration of {media_path}")

    # Calculate number of 5-minute segments (ceiling division)
    segment_duration = 300  # 5 minutes
    num_segments = int((duration + segment_duration - 1) // segment_duration)
    if num_segments == 0:
        num_segments = 1  # At least one segment for very short audio

    segments: list[tuple[str, Path, list[str]]] = []

    for chunk_index in range(num_segments):
        # Calculate timestamp for this segment
        ts = base_dt + timedelta(minutes=chunk_index * 5)
        time_part = ts.strftime("%H%M%S")

        # Create segment key with 5-minute duration
        segment_key_candidate = f"{time_part}_{segment_duration}"

        # Check for collision and deconflict if needed
        available_key = find_available_segment(stream_dir, segment_key_candidate)
        if available_key is None:
            logger.warning(
                f"Could not find available segment key near {segment_key_candidate}"
            )
            continue

        if available_key != segment_key_candidate:
            logger.info(
                f"Segment collision: {segment_key_candidate} -> {available_key}"
            )

        # Create segment directory under stream
        segment_dir = stream_dir / available_key
        segment_dir.mkdir(parents=True, exist_ok=True)

        # Slice audio for this segment
        audio_filename = f"imported_audio{source_ext}"
        audio_path = segment_dir / audio_filename
        start_seconds = chunk_index * segment_duration

        # For the last segment, use remaining duration
        if chunk_index == num_segments - 1:
            chunk_duration = duration - start_seconds
        else:
            chunk_duration = segment_duration

        try:
            slice_audio_segment(
                media_path,
                str(audio_path),
                start_seconds,
                chunk_duration,
            )
            segments.append((available_key, segment_dir, [audio_filename]))
            logger.info(f"Created segment: {available_key} with {audio_filename}")
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to slice segment {available_key}: {e}")
            # Clean up empty directory
            if segment_dir.exists() and not any(segment_dir.iterdir()):
                segment_dir.rmdir()

    return segments


class AudioFolderBackend:
    """Syncable backend for importing local folders of audio files."""

    name: str = "audio"

    def sync(
        self,
        journal_root: Path,
        *,
        dry_run: bool = True,
        source_path: Path | None = None,
        force: bool = False,
        auto: bool | str = True,
    ) -> dict[str, Any]:
        """Catalog or import audio files from a local folder."""
        if shutil.which("ffmpeg") is None:
            raise ValueError("ffmpeg is required for audio sync imports")
        if shutil.which("ffprobe") is None:
            raise ValueError("ffprobe is required for audio sync imports")
        if source_path is None:
            raise ValueError("Audio sync requires --path pointing to an audio folder")

        source_root = source_path.expanduser().resolve()
        if not source_root.exists() or not source_root.is_dir():
            raise ValueError(f"Audio sync path is not a directory: {source_path}")

        state = load_sync_state(journal_root, "audio") or {
            "backend": "audio",
            "source_path": str(source_root),
            "files": {},
        }
        if force:
            state["files"] = {}
        known_files: dict[str, dict[str, Any]] = state.get("files", {})

        for entry in known_files.values():
            if entry.get("status") != "available":
                continue
            source_hash = entry.get("hash")
            if not source_hash:
                continue
            if find_manifest_by_hash(journal_root, source_hash):
                entry["status"] = "imported"
                entry["imported_at"] = dt.datetime.now().isoformat()
                entry.pop("last_error", None)
                entry.pop("skip_reason", None)

        imports_root = (journal_root / "imports").resolve()
        current_rel_paths: set[str] = set()
        to_import: list[tuple[str, Path]] = []
        audio_count = 0
        errors: list[str] = []
        downloaded = 0

        for path in sorted(source_root.rglob("*")):
            resolved_path = path.resolve()
            if resolved_path.is_relative_to(imports_root):
                continue
            if not path.is_file():
                continue
            if path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue

            audio_count += 1
            rel_path = path.relative_to(source_root).as_posix()
            current_rel_paths.add(rel_path)

            source_hash = hash_source(resolved_path)
            filesize = resolved_path.stat().st_size
            base_entry = {
                **(known_files.get(rel_path) or {}),
                "filename": resolved_path.name,
                "filesize": filesize,
                "hash": source_hash,
            }

            if find_manifest_by_hash(journal_root, source_hash):
                entry = {
                    **base_entry,
                    "status": "imported",
                    "imported_at": base_entry.get(
                        "imported_at", dt.datetime.now().isoformat()
                    ),
                }
                entry.pop("last_error", None)
                entry.pop("skip_reason", None)
                known_files[rel_path] = entry
                continue

            duration = _get_audio_duration(str(resolved_path))
            if duration is None:
                entry = {**base_entry, "status": "unreadable"}
                entry.pop("imported_at", None)
                entry.pop("skip_reason", None)
                entry.pop("last_error", None)
                entry.pop("duration", None)
                known_files[rel_path] = entry
                errors.append(f"{rel_path}: could not read audio (probe failed)")
                continue
            if duration < 30:
                entry = {
                    **base_entry,
                    "status": "skipped",
                    "skip_reason": "too_short",
                    "duration": duration,
                }
                entry.pop("imported_at", None)
                entry.pop("last_error", None)
                known_files[rel_path] = entry
                continue

            entry = {
                **base_entry,
                "status": "available",
                "duration": duration,
            }
            entry.pop("imported_at", None)
            entry.pop("skip_reason", None)
            known_files[rel_path] = entry
            to_import.append((rel_path, resolved_path))

        if audio_count == 0:
            raise ValueError("Audio sync path contains no audio files")

        for rel_path, info in known_files.items():
            if rel_path not in current_rel_paths and info.get("status") != "removed":
                info["status"] = "removed"

        def summarize() -> dict[str, Any]:
            return {
                "total": len(known_files),
                "imported": sum(
                    1 for f in known_files.values() if f.get("status") == "imported"
                ),
                "available": sum(
                    1 for f in known_files.values() if f.get("status") == "available"
                ),
                "skipped": sum(
                    1 for f in known_files.values() if f.get("status") == "skipped"
                ),
                "downloaded": downloaded,
                "errors": errors,
            }

        def save_state() -> None:
            state["files"] = known_files
            state["source_path"] = str(source_root)
            state["last_sync"] = dt.datetime.now().isoformat()
            save_sync_state(journal_root, "audio", state)

        if not dry_run:
            from solstone.think.importers.cli import import_one

            for rel_path, path in to_import:
                info = known_files[rel_path]
                try:
                    result = import_one(
                        path,
                        source="audio",
                        auto=auto,
                        wait_for_processing=False,
                    )
                except Exception as exc:
                    message = str(exc) or exc.__class__.__name__
                    info["status"] = "available"
                    info["last_error"] = message
                    errors.append(f"{rel_path}: {message}")
                    logger.warning("Audio import failed for %s: %s", rel_path, message)
                else:
                    if (
                        isinstance(result, dict)
                        and "segments" in result
                        and "skipped" not in result
                    ):
                        info["status"] = "imported"
                        info["imported_at"] = dt.datetime.now().isoformat()
                        info.pop("last_error", None)
                        info.pop("skip_reason", None)
                        downloaded += 1
                    elif isinstance(result, dict) and result.get("skipped") is True:
                        reason = str(result.get("reason", "skipped"))
                        message = f"import_one skipped: {reason}"
                        info["status"] = "available"
                        info["last_error"] = message
                        errors.append(f"{rel_path}: {message}")
                        logger.warning(
                            "Audio import skipped for %s: %s", rel_path, reason
                        )
                    elif result is None:
                        message = "import_one returned no result"
                        info["status"] = "available"
                        info["last_error"] = message
                        errors.append(f"{rel_path}: {message}")
                        logger.warning(
                            "Audio import returned no result for %s", rel_path
                        )
                    else:
                        message = "import_one returned unrecognized result"
                        info["status"] = "available"
                        info["last_error"] = message
                        errors.append(f"{rel_path}: {message}")
                        logger.warning(
                            "Audio import returned unrecognized result for %s", rel_path
                        )
                save_state()

        result = summarize()
        save_state()
        return result


backend = AudioFolderBackend()
