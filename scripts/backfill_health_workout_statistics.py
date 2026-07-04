#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Backfill Apple Health workout distance/energy metadata from retained raws.

The original Apple Health workout dedupe keys are stable identifiers once
assigned. This migration intentionally updates only normalized row metadata
from retained raw exports; it never recalculates or rewrites ``dedupe_key``,
``normalized_ref``, or ``raw_ref``.

Dry-run by default. Pass ``--apply`` to rewrite changed normalized shards.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from solstone.think.importers.apple_health import (  # noqa: E402
    _find_export_xml_in_directory,
    _find_export_xml_in_zip,
    _workout_statistics_by_raw_ref,
)
from solstone.think.importers.health_schema import SOURCE_APPLE_HEALTH  # noqa: E402
from solstone.think.importers.shared import write_jsonl_records  # noqa: E402

_WORKOUT_STATISTIC_KEYS = (
    "totalDistance",
    "totalDistanceUnit",
    "totalDistanceType",
    "totalEnergyBurned",
    "totalEnergyBurnedUnit",
    "totalEnergyBurnedType",
)


def _iter_import_dirs(journal_root: Path) -> Iterable[Path]:
    imports_root = journal_root / "imports"
    if not imports_root.is_dir():
        return ()
    return (
        path
        for path in sorted(imports_root.iterdir())
        if path.is_dir() and not path.name.startswith("_")
    )


def _raw_sources(import_dir: Path) -> list[tuple[Path, str]]:
    raw_dir = import_dir / "raw"
    if not raw_dir.is_dir():
        return []

    sources: list[tuple[Path, str]] = []
    export_xml = _find_export_xml_in_directory(raw_dir)
    if export_xml is not None:
        raw_ref = (
            f"imports/{import_dir.name}/{export_xml.relative_to(import_dir).as_posix()}"
        )
        sources.append((raw_dir, raw_ref))

    for candidate in sorted(raw_dir.rglob("*.zip")):
        with zipfile.ZipFile(candidate) as archive:
            if _find_export_xml_in_zip(archive.namelist()) is None:
                continue
        raw_ref = (
            f"imports/{import_dir.name}/{candidate.relative_to(import_dir).as_posix()}"
        )
        sources.append((candidate, raw_ref))
    return sources


def _load_workout_statistics(import_dir: Path) -> dict[str, dict[str, str]]:
    stats_by_ref: dict[str, dict[str, str]] = {}
    for source_path, raw_ref in _raw_sources(import_dir):
        stats_by_ref.update(_workout_statistics_by_raw_ref(source_path, raw_ref))
    return stats_by_ref


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Could not parse {path} line {line_number}: {exc}"
            ) from exc
    return rows


def _merge_workout_statistics(
    row: dict[str, Any],
    stats_by_ref: dict[str, dict[str, str]],
) -> bool:
    if row.get("source_family") != SOURCE_APPLE_HEALTH:
        return False
    if row.get("kind") != "workout":
        return False
    raw_ref = row.get("raw_ref")
    if not isinstance(raw_ref, str):
        return False
    recovered = stats_by_ref.get(raw_ref)
    if not recovered:
        return False

    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        row["metadata"] = metadata

    changed = False
    for key in _WORKOUT_STATISTIC_KEYS:
        value = recovered.get(key)
        if value is not None and key not in metadata:
            metadata[key] = value
            changed = True
    return changed


def _process_import_dir(
    import_dir: Path,
    *,
    journal_root: Path,
    apply: bool,
) -> tuple[int, int]:
    stats_by_ref = _load_workout_statistics(import_dir)
    if not stats_by_ref:
        return (0, 0)

    normalized_dir = import_dir / "normalized"
    if not normalized_dir.is_dir():
        return (0, 0)

    rows_seen = 0
    rows_changed = 0
    for shard in sorted(normalized_dir.glob("*.jsonl")):
        rows = _read_jsonl(shard)
        shard_changed = 0
        for row in rows:
            if row.get("kind") == "workout":
                rows_seen += 1
            if _merge_workout_statistics(row, stats_by_ref):
                shard_changed += 1
        if shard_changed:
            rows_changed += shard_changed
            rel = shard.relative_to(journal_root)
            action = "updated" if apply else "would update"
            print(f"{rel}: {shard_changed} workout rows {action}")
            if apply:
                write_jsonl_records(shard, rows)
    return (rows_seen, rows_changed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill Apple Health workout distance/energy metadata from retained "
            "raw exports. Dry-run unless --apply is given."
        )
    )
    parser.add_argument(
        "journal_root",
        type=Path,
        help="Journal root to inspect (required; never defaults to a live journal)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite changed normalized shards (default: dry-run report only)",
    )
    args = parser.parse_args(argv)

    journal_root = args.journal_root.expanduser().resolve()
    if not journal_root.is_dir():
        parser.error(f"journal root is not a directory: {journal_root}")

    workout_rows = 0
    changed_rows = 0
    for import_dir in _iter_import_dirs(journal_root):
        seen, changed = _process_import_dir(
            import_dir,
            journal_root=journal_root,
            apply=args.apply,
        )
        workout_rows += seen
        changed_rows += changed

    if args.apply:
        print(f"{workout_rows} workout rows inspected; {changed_rows} rows updated")
    else:
        print(
            f"{workout_rows} workout rows inspected; "
            f"{changed_rows} rows would update "
            "(dry-run; pass --apply to rewrite)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
