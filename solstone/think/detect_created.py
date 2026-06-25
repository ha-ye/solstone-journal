# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Media file metadata detection utilities."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .prompts import load_prompt

DETERMINISTIC_SOURCE_FILENAME = "filename_local"
DETERMINISTIC_SOURCE_FILENAME_UTC = "filename_utc"
DETERMINISTIC_SOURCE_METADATA_LOCAL = "metadata_local"
DETERMINISTIC_SOURCE_METADATA_UTC = "metadata_utc"

_LIMITLESS_FILENAME_RE = re.compile(
    r"^limitless_pendant_(\d{4})-(\d{2})-(\d{2})T"
    r"(\d{2})-(\d{2})-(\d{2})_to_.*$"
)
_LOCAL_FILENAME_RES = (
    re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})_(\d{2})_(\d{2})(?!\d)"),
    re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})(?!\d)"),
    re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})(?!\d)"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})(?!\d)"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?!\d)"),
)
_METADATA_CREATION_FIELDS = (
    "SubSecCreateDate",
    "SubSecDateTimeOriginal",
    "CreateDate",
    "CreationDate",
    "DateTimeOriginal",
    "MediaCreateDate",
    "TrackCreateDate",
    "ContentCreateDate",
)
_OFFSET_RE = re.compile(r"(Z|[+-]\d{2}:\d{2})$")
_SUBSECOND_RE = re.compile(r"(\d{2}:\d{2}:\d{2})\.\d+")

_SCHEMA = json.loads(
    (Path(__file__).parent / "detect_created.schema.json").read_text(encoding="utf-8")
)


def _load_system_prompt() -> str:
    """Load the system prompt from detect_created.txt file."""
    return load_prompt("detect_created", base_dir=Path(__file__).parent).text


def _extract_metadata(path: str) -> str:
    """Return metadata for *path* using exiftool if available."""
    cmd = [
        "exiftool",
        "-all",
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return proc.stdout
    except Exception as exc:  # pragma: no cover - exiftool optional
        return f"Error extracting metadata: {exc}"


def _extract_metadata_json(path: str) -> dict:
    """Return JSON metadata for *path* using exiftool if available."""
    cmd = [
        "exiftool",
        "-json",
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        metadata = json.loads(proc.stdout)
    except Exception:  # pragma: no cover - exiftool optional
        return {}
    if not isinstance(metadata, list) or not metadata:
        return {}
    first = metadata[0]
    return first if isinstance(first, dict) else {}


def _result_from_datetime(
    value: datetime,
    *,
    source: str,
    utc: bool,
) -> dict:
    return {
        "day": value.strftime("%Y%m%d"),
        "time": value.strftime("%H%M%S"),
        "confidence": "high",
        "source": source,
        "utc": utc,
    }


def _parse_datetime_parts(parts: tuple[str, ...]) -> datetime | None:
    try:
        return datetime(*(int(part) for part in parts))
    except ValueError:
        return None


def _filename_stem(path: str, original_filename: Optional[str]) -> str:
    name = original_filename if original_filename else path
    return Path(Path(name).name).stem


def _resolve_limitless_filename(stem: str) -> dict | None:
    match = _LIMITLESS_FILENAME_RE.match(stem)
    if match is None:
        return None
    parsed = _parse_datetime_parts(match.groups())
    if parsed is None:
        return None
    utc_dt = parsed.replace(tzinfo=timezone.utc)
    return _result_from_datetime(
        utc_dt.astimezone(),
        source=DETERMINISTIC_SOURCE_FILENAME_UTC,
        utc=True,
    )


def _resolve_local_filename(stem: str) -> dict | None:
    for pattern in _LOCAL_FILENAME_RES:
        match = pattern.match(stem)
        if match is None:
            continue
        parsed = _parse_datetime_parts(match.groups())
        if parsed is None:
            return None
        return _result_from_datetime(
            parsed,
            source=DETERMINISTIC_SOURCE_FILENAME,
            utc=False,
        )
    return None


def _normalize_metadata_datetime(value: object) -> tuple[str, str, bool] | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or raw.startswith("0000:") or raw.startswith("0000-"):
        return None
    normalized = raw.replace("T", " ")
    normalized = _SUBSECOND_RE.sub(r"\1", normalized)
    if len(normalized) >= 10 and normalized[4] == ":" and normalized[7] == ":":
        normalized = f"{normalized[:4]}-{normalized[5:7]}-{normalized[8:]}"
    normalized = normalized.replace(" ", "T", 1)

    offset_bearing = bool(_OFFSET_RE.search(normalized))
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.year == 0:
        return None
    if offset_bearing:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y%m%d"), parsed.strftime("%H%M%S"), offset_bearing


def _resolve_metadata(path: str) -> dict | None:
    try:
        metadata = _extract_metadata_json(path)
    except Exception:
        return None
    pairs: set[tuple[str, str]] = set()
    saw_offset = False
    for field in _METADATA_CREATION_FIELDS:
        parsed = _normalize_metadata_datetime(metadata.get(field))
        if parsed is None:
            continue
        day, time, offset_bearing = parsed
        pairs.add((day, time))
        saw_offset = saw_offset or offset_bearing
    if len(pairs) != 1:
        return None
    day, time = next(iter(pairs))
    return {
        "day": day,
        "time": time,
        "confidence": "high",
        "source": (
            DETERMINISTIC_SOURCE_METADATA_UTC
            if saw_offset
            else DETERMINISTIC_SOURCE_METADATA_LOCAL
        ),
        "utc": saw_offset,
    }


def resolve_created_deterministic(
    path: str, original_filename: Optional[str] = None
) -> Optional[dict]:
    """Return deterministic creation time information for *path* when unambiguous.

    Direct-source timestamps, such as Plaud recording start times, bypass this resolver
    by passing an explicit timestamp into the importer.
    """
    stem = _filename_stem(path, original_filename)
    limitless_candidate = _resolve_limitless_filename(stem)
    if limitless_candidate is not None:
        return limitless_candidate

    filename_candidate = _resolve_local_filename(stem)
    metadata_candidate = _resolve_metadata(path)
    if filename_candidate is not None and metadata_candidate is not None:
        filename_pair = (filename_candidate["day"], filename_candidate["time"])
        metadata_pair = (metadata_candidate["day"], metadata_candidate["time"])
        if filename_pair != metadata_pair:
            return None
        return filename_candidate
    return filename_candidate or metadata_candidate


def detect_created(
    path: str, original_filename: Optional[str] = None, guidance: Optional[str] = None
) -> Optional[dict]:
    """Return creation time information for *path* using configured provider.

    Parameters
    ----------
    path : str
        Path to the file to analyze
    original_filename : Optional[str]
        Original filename if path is a temporary file
    guidance : Optional[str]
        Optional guidance text from the user to help the LLM interpret ambiguous metadata
    """
    metadata = _extract_metadata(path)

    # Use original filename in header if provided, otherwise use the actual path
    display_path = original_filename if original_filename else path

    lines = [
        f"# exiftool -all output for {display_path}",
        "",
    ]

    # If we have an original filename and it's different from path, add a note
    if original_filename and original_filename != path:
        lines.extend(
            [
                f"Original filename: {original_filename}",
                f"(Analyzing temporary file: {path})",
                "",
            ]
        )

    lines.append(metadata)
    markdown = "\n".join(lines)
    if guidance:
        markdown += f"\n\nImportant guidance from the user: {guidance}"

    from solstone.think.models import generate

    response_text = generate(
        contents=markdown,
        context="detect.created",
        temperature=0.3,
        max_output_tokens=256,
        thinking_budget=4096,
        system_instruction=_load_system_prompt(),
        json_output=True,
        json_schema=_SCHEMA,
    )

    try:
        result = json.loads(response_text)

        # Convert UTC to local time if needed
        if result and result.get("utc") is True:
            day = result.get("day")
            time = result.get("time")

            if day and time:
                # Parse as UTC datetime
                utc_dt = datetime.strptime(f"{day}{time}", "%Y%m%d%H%M%S")
                utc_dt = utc_dt.replace(tzinfo=timezone.utc)

                # Convert to local timezone
                local_dt = utc_dt.astimezone()

                # Update result with local time
                result["day"] = local_dt.strftime("%Y%m%d")
                result["time"] = local_dt.strftime("%H%M%S")

        return result
    except json.JSONDecodeError:
        return None
