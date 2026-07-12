# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Retired provider-context registration for timeline segment summary."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RegistrationSummary:
    added: int = 0
    preserved: int = 0
    warnings: int = 0
    errors: int = 0


def run_registration(
    journal_path: Path, *, dry_run: bool = False
) -> RegistrationSummary:
    _ = (journal_path, dry_run)
    return RegistrationSummary()


def _print_summary(summary: RegistrationSummary) -> None:
    print("Summary")
    print(f"  added:     {summary.added}")
    print(f"  preserved: {summary.preserved}")
    print(f"  warnings:  {summary.warnings}")
    print(f"  errors:    {summary.errors}")
    print("  retired:   provider context registration is no longer needed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview provider context registration without writing files.",
    )
    args = parser.parse_args()

    summary = run_registration(Path.cwd(), dry_run=args.dry_run)
    _print_summary(summary)
    if summary.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
