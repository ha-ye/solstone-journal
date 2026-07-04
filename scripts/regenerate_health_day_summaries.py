#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Regenerate Apple Health day-summary cards from normalized import rows.

For the journal root given on the command line (required — this never
defaults to a live journal), finds every
``chronicle/<day>/import.apple_health/<segment>/day_summary_transcript.md``,
rebuilds that day's summary from the normalized rows across all
``imports/*/normalized/<month>.jsonl`` shards (deduplicated by
``dedupe_key``, later import bundles winning), and renders it with the
importer's day-summary renderer.

Dry-run by default: prints, per day, the old and new byte sizes and whether
the content would change. Pass ``--apply`` to rewrite changed files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from solstone.think.importers.apple_health import (  # noqa: E402
    _add_to_day_summary,
    _DaySummary,
    _render_day_summary,
)
from solstone.think.importers.health_schema import (  # noqa: E402
    DEFAULT_HEALTH_IMPORT_STREAM,
    SOURCE_APPLE_HEALTH,
)
from solstone.think.importers.shared import write_markdown_segment_file  # noqa: E402

DAY_SUMMARY_FILENAME = "day_summary_transcript.md"


def load_rows_by_day(journal_root: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Map day -> dedupe_key -> normalized Apple Health row.

    Import bundles are visited in sorted (chronological) order, so when the
    same ``dedupe_key`` appears in several bundles the newest bundle's row
    wins — matching the dedupe database's last-seen semantics.
    """

    rows_by_day: dict[str, dict[str, dict[str, Any]]] = {}
    imports_dir = journal_root / "imports"
    if not imports_dir.is_dir():
        return rows_by_day
    for bundle in sorted(imports_dir.iterdir()):
        normalized_dir = bundle / "normalized"
        if not normalized_dir.is_dir():
            continue
        for shard in sorted(normalized_dir.glob("*.jsonl")):
            for line_number, line in enumerate(
                shard.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Could not parse {shard} line {line_number}: {exc}"
                    ) from exc
                if row.get("source_family") != SOURCE_APPLE_HEALTH:
                    continue
                day = row.get("day")
                dedupe_key = row.get("dedupe_key")
                if not day or not dedupe_key:
                    continue
                rows_by_day.setdefault(day, {})[dedupe_key] = row
    return rows_by_day


def rebuild_day_summary(
    day: str, rows_by_key: dict[str, dict[str, Any]]
) -> tuple[str, str]:
    """Render (markdown, import_id) for one day from deduped normalized rows."""

    ordered = sorted(
        rows_by_key.values(),
        key=lambda row: (
            row.get("start_date") or "",
            row.get("record_type") or "",
            row.get("dedupe_key") or "",
        ),
    )
    summary = _DaySummary(day=day)
    for row in ordered:
        _add_to_day_summary(summary, row)
    import_ids = sorted(
        {str(row["import_id"]) for row in ordered if row.get("import_id")}
    )
    import_id = import_ids[-1] if import_ids else "unknown"
    return _render_day_summary(summary, import_id=import_id), import_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Regenerate Apple Health day-summary cards under a journal root "
            "from its normalized import rows. Dry-run unless --apply is given."
        )
    )
    parser.add_argument(
        "journal_root",
        type=Path,
        help="Journal root to regenerate (required; never defaults to a live journal)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite changed day-summary files (default: dry-run report only)",
    )
    args = parser.parse_args(argv)

    journal_root = args.journal_root.expanduser().resolve()
    if not journal_root.is_dir():
        parser.error(f"journal root is not a directory: {journal_root}")

    summary_paths = sorted(
        journal_root.glob(
            f"chronicle/*/{DEFAULT_HEALTH_IMPORT_STREAM}/*/{DAY_SUMMARY_FILENAME}"
        )
    )
    if not summary_paths:
        print(f"No Apple Health day summaries found under {journal_root}")
        return 0

    rows_by_day = load_rows_by_day(journal_root)

    changed = 0
    unchanged = 0
    skipped = 0
    rewritten = 0
    for md_path in summary_paths:
        segment_key = md_path.parent.name
        day = md_path.parent.parent.parent.name
        day_rows = rows_by_day.get(day)
        if not day_rows:
            skipped += 1
            print(f"{day}: no normalized rows found; leaving file untouched")
            continue
        old_content = md_path.read_text(encoding="utf-8")
        content, _import_id = rebuild_day_summary(day, day_rows)
        new_content = content + "\n"
        old_bytes = len(old_content.encode("utf-8"))
        new_bytes = len(new_content.encode("utf-8"))
        if new_content == old_content:
            unchanged += 1
            print(f"{day}: old {old_bytes} bytes -> new {new_bytes} bytes (unchanged)")
            continue
        changed += 1
        print(f"{day}: old {old_bytes} bytes -> new {new_bytes} bytes (changed)")
        if args.apply:
            write_markdown_segment_file(
                journal_root,
                day,
                DEFAULT_HEALTH_IMPORT_STREAM,
                segment_key,
                DAY_SUMMARY_FILENAME,
                content,
            )
            rewritten += 1

    total = len(summary_paths)
    if args.apply:
        print(
            f"{total} summaries: {rewritten} rewritten, "
            f"{unchanged} unchanged, {skipped} without rows"
        )
    else:
        print(
            f"{total} summaries: {changed} would change, "
            f"{unchanged} unchanged, {skipped} without rows "
            "(dry-run; pass --apply to rewrite)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
