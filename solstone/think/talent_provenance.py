# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Talent output provenance sidecars.

This module is the sole write owner for
``chronicle/<day>/health/talent-provenance/**``.

Growth bounds:
- Output provenance sidecars are 1:1 with talent output files and overwritten in
  place on each real generation, so unchanged reprocessing does not append.
- Segment sidecar orphans are pruned under the reprocessed segment's provenance
  subtree only; there is no global day scan.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from solstone.think.journal_io import (
    MalformedPolicy,
    day_path,
    hold_lock,
    iter_segments,
    read_json,
    write_json,
)
from solstone.think.utils import get_journal

LOG = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_PROVENANCE_DIR = "talent-provenance"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_identity_hash(identity: dict[str, Any]) -> str:
    """Return the SHA-256 hash of a canonicalized identity object."""
    return hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def output_digest(output_path: Path) -> tuple[str, int]:
    """Return ``(sha256, size)`` for an output file."""
    data = output_path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _journal_relative(path: Path) -> Path:
    journal = Path(get_journal()).resolve()
    return path.resolve().relative_to(journal)


def _day_and_logical_output(output_path: Path) -> tuple[str, Path]:
    rel = _journal_relative(output_path)
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == "chronicle":
        return parts[1], Path(*parts[2:])
    if len(parts) >= 6 and parts[0] == "facets" and parts[2] == "activities":
        facet = parts[1]
        day = parts[3]
        return day, Path("facets", facet, "activities", *parts[4:])
    raise ValueError(f"unsupported talent output path for provenance: {output_path}")


def _base_dir(day: str) -> Path:
    return day_path(day, create=False) / "health" / _PROVENANCE_DIR


def provenance_path_for_output(output_path: Path) -> Path:
    """Return the mirrored provenance sidecar path for a talent output."""
    day, logical = _day_and_logical_output(output_path)
    sidecar = _base_dir(day) / logical
    return sidecar.with_name(f"{sidecar.name}.json")


def write_provenance(
    output_path: Path,
    *,
    identity_hash: str,
    output_sha256: str,
    output_size: int,
    provider: str | None,
    model: str | None,
    fallback_from: str | None,
    generation_params: dict[str, Any],
    completed_at_ms: int,
    use_id: str | None,
    identity_fields: dict[str, Any],
) -> None:
    """Atomically overwrite the provenance sidecar for ``output_path``."""
    sidecar_path = provenance_path_for_output(output_path)
    record = {
        "schema_version": SCHEMA_VERSION,
        "identity_hash": identity_hash,
        "output_sha256": output_sha256,
        "output_size": output_size,
        "provider": provider,
        "model": model,
        "fallback_from": fallback_from,
        "generation_params": generation_params,
        "completed_at_ms": completed_at_ms,
        "use_id": use_id,
        "output_path": str(_journal_relative(output_path)),
        "identity": identity_fields,
    }
    with hold_lock(output_path):
        write_json(sidecar_path, record, sort_keys=True)


def read_provenance(output_path: Path) -> dict[str, Any] | None:
    """Read provenance for ``output_path``; malformed data is a cache miss."""
    sidecar_path = provenance_path_for_output(output_path)
    try:
        record = read_json(
            sidecar_path,
            on_error=MalformedPolicy.RAISE,
            default=None,
        )
    except Exception:
        LOG.warning("failed to read talent provenance %s", sidecar_path, exc_info=True)
        return None
    if record is None:
        return None
    if not isinstance(record, dict):
        LOG.warning("invalid talent provenance record in %s", sidecar_path)
        return None
    return record


def _activity_path(day: str, facet: str, activity_id: str) -> Path:
    safe_facet = quote(facet, safe="._-")
    safe_activity = quote(activity_id, safe="._-")
    return _base_dir(day) / "activity-inputs" / safe_facet / f"{safe_activity}.json"


def write_activity_provenance(
    day: str,
    facet: str,
    activity_id: str,
    input_hash: str,
) -> None:
    """Persist the latest activity input hash for parent prompt gating."""
    path = _activity_path(day, facet, activity_id)
    with hold_lock(path):
        write_json(
            path,
            {
                "schema_version": SCHEMA_VERSION,
                "facet": facet,
                "activity_id": activity_id,
                "input_hash": input_hash,
            },
            sort_keys=True,
        )


def read_activity_provenance(day: str, facet: str, activity_id: str) -> str | None:
    """Read the latest activity input hash, or ``None`` on missing/malformed data."""
    path = _activity_path(day, facet, activity_id)
    try:
        record = read_json(path, on_error=MalformedPolicy.RAISE, default=None)
    except Exception:
        LOG.warning("failed to read activity provenance %s", path, exc_info=True)
        return None
    if not isinstance(record, dict):
        return None
    value = record.get("input_hash")
    return value if isinstance(value, str) else None


def compute_activity_input_hash(day: str, activity: dict[str, Any]) -> str:
    """Hash an activity's ordered spans and constituent segment sense content."""
    spans = [str(seg) for seg in activity.get("segments", []) if seg]
    segment_dirs: dict[str, list[Path]] = {}
    for _stream, segment, seg_path in iter_segments(day):
        segment_dirs.setdefault(segment, []).append(seg_path)

    segment_inputs: list[dict[str, Any]] = []
    for segment in spans:
        sense_files = []
        for seg_path in sorted(segment_dirs.get(segment, [])):
            sense_path = seg_path / "talents" / "sense.json"
            if sense_path.exists():
                digest, size = output_digest(sense_path)
                rel_path = str(_journal_relative(sense_path))
                sense_files.append({"path": rel_path, "sha256": digest, "size": size})
            else:
                sense_files.append(
                    {
                        "path": str(
                            _journal_relative(seg_path / "talents" / "sense.json")
                        ),
                        "missing": True,
                    }
                )
        if not sense_files:
            sense_files.append({"segment": segment, "missing": True})
        segment_inputs.append({"segment": segment, "sense": sense_files})

    return compute_identity_hash(
        {
            "activity_id": activity.get("id"),
            "activity": activity.get("activity"),
            "facet": activity.get("facet"),
            "segments": spans,
            "segment_inputs": segment_inputs,
        }
    )


def _segment_subtree(day: str, stream: str | None, segment: str) -> Path:
    base = _base_dir(day)
    if stream:
        return base / stream / segment / "talents"
    return base / segment / "talents"


def prune_orphan_provenance(day: str, stream: str | None, segment: str) -> None:
    """Prune orphaned sidecars for one segment's mirrored output subtree."""
    for subtree in {
        _segment_subtree(day, stream, segment),
        _segment_subtree(day, None, segment),
    }:
        if not subtree.is_dir():
            continue
        for sidecar_path in sorted(subtree.rglob("*.json")):
            try:
                record = read_json(
                    sidecar_path,
                    on_error=MalformedPolicy.WARN_AND_SKIP,
                    default=None,
                )
                if not isinstance(record, dict):
                    continue
                output_rel = record.get("output_path")
                if not isinstance(output_rel, str):
                    continue
                if not (Path(get_journal()) / output_rel).exists():
                    sidecar_path.unlink()
            except OSError:
                LOG.warning(
                    "failed to prune orphan talent provenance %s",
                    sidecar_path,
                    exc_info=True,
                )
