# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Remove legacy timeline rollup schedule entries superseded by journal maintenance routines."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field

from solstone.think.journal_io.errors import LockTimeout, MalformedDataError
from solstone.think.schedule_config import read_schedules, remove_schedule_entry
from solstone.think.utils import setup_cli

logger = logging.getLogger(__name__)

LEGACY_ENTRIES = {
    "timeline-rollup-day": {
        "cmd": ["sol", "call", "timeline", "rollup-day"],
        "every": "daily",
        "max_runtime": "30m",
    },
    "timeline-rollup-master": {
        "cmd": ["sol", "call", "timeline", "rollup-master"],
        "every": "daily",
        "max_runtime": "30m",
    },
}


@dataclass
class MigrationSummary:
    removed: int = 0
    preserved: int = 0
    absent: int = 0
    preserved_names: list[str] = field(default_factory=list)


def run_migration(*, dry_run: bool = False) -> MigrationSummary:
    summary = MigrationSummary()
    raw = read_schedules()

    for name, expected in LEGACY_ENTRIES.items():
        existing = raw.get(name)
        if existing is None:
            summary.absent += 1
            continue

        if existing == expected:
            if not dry_run:
                remove_schedule_entry(name)
            summary.removed += 1
            continue

        logger.warning("Preserving owner-divergent schedule entry %s", name)
        summary.preserved += 1
        summary.preserved_names.append(name)

    return summary


def _print_summary(summary: MigrationSummary) -> None:
    print("Summary")
    print(f"  removed:   {summary.removed}")
    print(f"  preserved: {summary.preserved}")
    print(f"  absent:    {summary.absent}")
    for name in summary.preserved_names:
        print(f"WARNING: preserved owner-divergent schedule entry {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview legacy schedule removal without writing files.",
    )
    args = setup_cli(parser)

    try:
        summary = run_migration(dry_run=args.dry_run)
    except (OSError, MalformedDataError, LockTimeout) as exc:
        logger.warning("Failed to migrate timeline rollup schedules: %s", exc)
        sys.exit(1)

    _print_summary(summary)


if __name__ == "__main__":
    main()
