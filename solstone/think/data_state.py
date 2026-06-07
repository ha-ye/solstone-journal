# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared per-modality data-state vocabulary."""

import json
import os
import time
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

ANALYZING_STALE_SECONDS = 1800


class DataState(StrEnum):
    """Read-only visibility state for modality data."""

    ANALYZED = "analyzed"
    PENDING = "pending"
    ANALYZING = "analyzing"
    FAILED = "failed"
    PURGED = "purged"
    ABSENT = "absent"


def _iso_z_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _analyzing_path(seg_path: Path, modality: str) -> Path:
    return seg_path / f".analyzing_{modality}"


def _failed_path(seg_path: Path, modality: str) -> Path:
    return seg_path / f".analyze_failed_{modality}"


def _write_failed_marker(
    marker_path: Path,
    failed_path: Path,
    modality: str,
    reason: str,
    detail: str,
    payload: dict | None = None,
) -> None:
    failed_payload = {
        "started_at": (payload or {}).get("started_at", _iso_z_now()),
        "modality": modality,
        "reason": reason,
        "failed_at": _iso_z_now(),
        "detail": detail,
    }
    marker_path.write_text(json.dumps(failed_payload, sort_keys=True) + "\n")
    marker_path.replace(failed_path)


def create_analyzing_marker(seg_path: Path, modality: str) -> Path:
    """Atomically create a per-modality analyzing marker."""
    path = _analyzing_path(seg_path, modality)
    payload = {
        "started_at": _iso_z_now(),
        "modality": modality,
    }
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        os.write(fd, json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n")
    finally:
        os.close(fd)
    return path


def _classify_marker(
    marker_path: Path, *, has_chunks: bool
) -> tuple[str, dict | None, str]:
    if has_chunks:
        return "chunks_win", None, ""
    if not marker_path.is_file():
        return "none", None, ""

    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("marker JSON must be an object")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return "corrupt", None, str(exc)

    try:
        marker_age = time.time() - marker_path.stat().st_mtime
    except OSError:
        marker_age = 0
    if marker_age > ANALYZING_STALE_SECONDS:
        return (
            "stale",
            payload,
            f"analyzing marker older than {ANALYZING_STALE_SECONDS} seconds",
        )
    return "active", payload, ""


def derive_modality_state(
    seg_path: Path,
    modality: str,
    *,
    has_chunks: bool,
    has_jsonl: bool,
    has_raw: bool,
) -> str:
    """Purely resolve per-modality data state without writing sidecar markers.

    Marker repair is intentionally separated into repair_modality_markers().
    """
    marker_path = _analyzing_path(seg_path, modality)
    failed_path = _failed_path(seg_path, modality)

    verdict, _payload, _detail = _classify_marker(marker_path, has_chunks=has_chunks)
    if verdict == "chunks_win":
        return DataState.ANALYZED.value
    if verdict in {"corrupt", "stale"}:
        return DataState.FAILED.value
    if verdict == "active":
        return DataState.ANALYZING.value

    if failed_path.is_file():
        return DataState.FAILED.value
    if has_jsonl or has_raw:
        return DataState.PENDING.value
    return DataState.ABSENT.value


def repair_modality_markers(
    seg_path: Path,
    modality: str,
    *,
    has_chunks: bool,
    has_jsonl: bool,
    has_raw: bool,
) -> None:
    """Repair analyzing sidecars using the former derive_modality_state writes."""
    marker_path = _analyzing_path(seg_path, modality)
    failed_path = _failed_path(seg_path, modality)
    verdict, payload, detail = _classify_marker(marker_path, has_chunks=has_chunks)

    if verdict == "chunks_win":
        marker_path.unlink(missing_ok=True)
    elif verdict == "corrupt":
        _write_failed_marker(
            marker_path,
            failed_path,
            modality,
            "marker_corrupt",
            detail,
        )
    elif verdict == "stale":
        _write_failed_marker(
            marker_path,
            failed_path,
            modality,
            "stale",
            detail,
            payload,
        )
