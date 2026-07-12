# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Retired provider-context rename for the chat refactor."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class MigrationSummary:
    renamed: int = 0
    removed: int = 0
    preserved: int = 0
    errors: int = 0
    skipped_reason: str | None = None


def run_migration(journal_path: Path, *, dry_run: bool) -> MigrationSummary:
    _ = (journal_path, dry_run)
    return MigrationSummary(skipped_reason="retired")


def _print_summary(summary: MigrationSummary) -> None:
    logger.info("Summary")
    logger.info("  renamed:  %d", summary.renamed)
    logger.info("  removed:  %d", summary.removed)
    logger.info("  preserved:%d", summary.preserved)
    logger.info("  errors:   %d", summary.errors)
    if summary.skipped_reason is not None:
        logger.info("  skipped:  %s", summary.skipped_reason)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the provider-context rename without writing files.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    journal_path = Path.cwd()
    summary = run_migration(journal_path, dry_run=args.dry_run)

    _print_summary(summary)
    if summary.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
