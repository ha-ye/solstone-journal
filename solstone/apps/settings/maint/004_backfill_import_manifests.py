# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Backfill byte-hash dedup manifests for pre-dedup audio/text imports."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from solstone.think.importers.shared import hash_source, write_manifest
from solstone.think.importers.utils import list_import_timestamps
from solstone.think.utils import get_journal, setup_cli

logger = logging.getLogger(__name__)


def _source_type_from_ext(path: Path) -> str:
    """Infer generic import source type from the retained original extension."""
    ext = path.suffix.lower()
    if ext == ".m4a":
        return "apple"
    if ext in {".txt", ".md", ".pdf"}:
        return "text"
    return "audio"


def _days_affected(imported_meta: dict) -> list[str]:
    """Derive affected days without relying on path parsing defaults."""
    target_day = imported_meta.get("target_day")
    if target_day:
        return [target_day]
    date_range = imported_meta.get("date_range")
    if date_range:
        return sorted(set(date_range))
    return []


def backfill(journal_root: Path) -> dict:
    """Backfill missing import dedup manifests."""
    counts = {
        "scanned": 0,
        "backfilled": 0,
        "skipped_already_has_manifest": 0,
        "skipped_no_retained_original": 0,
    }
    for ts in list_import_timestamps(journal_root):
        counts["scanned"] += 1
        import_dir = journal_root / "imports" / ts

        if (import_dir / "manifest.json").exists():
            counts["skipped_already_has_manifest"] += 1
            continue

        import_json_path = import_dir / "import.json"
        if not import_json_path.is_file():
            counts["skipped_no_retained_original"] += 1
            continue
        with open(import_json_path, "r", encoding="utf-8") as f:
            import_meta = json.load(f)
        file_path = import_meta.get("file_path")
        if not file_path:
            counts["skipped_no_retained_original"] += 1
            continue

        retained = import_dir / os.path.basename(file_path)
        if not retained.is_file():
            counts["skipped_no_retained_original"] += 1
            continue

        source_hash = hash_source(retained)
        imported_path = import_dir / "imported.json"
        imported_meta = {}
        if imported_path.is_file():
            with open(imported_path, "r", encoding="utf-8") as f:
                imported_meta = json.load(f)
        source_type = imported_meta.get("source_type") or _source_type_from_ext(
            retained
        )
        files_created = imported_meta.get("all_created_files", [])
        entry_count = len(files_created)
        days_affected = _days_affected(imported_meta)
        write_manifest(
            journal_root,
            import_id=ts,
            source_type=source_type,
            source_hash=source_hash,
            entry_count=entry_count,
            files_created=files_created,
            days_affected=days_affected,
        )
        counts["backfilled"] += 1
        logger.info(
            "Backfilled manifest for import %s (hash %s...)", ts, source_hash[:12]
        )
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    setup_cli(parser)
    journal_root = Path(get_journal())
    counts = backfill(journal_root)
    print(f"Scanned {counts['scanned']} import(s)")
    print(f"Backfilled {counts['backfilled']} manifest(s)")
    print(f"Skipped {counts['skipped_already_has_manifest']} already having a manifest")
    print(f"Skipped {counts['skipped_no_retained_original']} with no retained original")


if __name__ == "__main__":
    main()
