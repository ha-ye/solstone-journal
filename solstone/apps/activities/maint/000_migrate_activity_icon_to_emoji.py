# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Migrate legacy custom activity glyphs from icon to emoji."""

from __future__ import annotations

import argparse

from solstone.think.activities import migrate_custom_activity_icons_to_emoji
from solstone.think.utils import setup_cli


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    setup_cli(parser)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report records that would change without writing activity config",
    )
    args = parser.parse_args()

    result = migrate_custom_activity_icons_to_emoji(dry_run=args.dry_run)
    action = "Would migrate" if args.dry_run else "Migrated"
    print(
        f"{action} {result['records_changed']} custom activity record(s) "
        f"across {result['files_changed']} file(s); "
        f"scanned {result['files_scanned']} file(s)."
    )


if __name__ == "__main__":
    main()
