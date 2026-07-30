# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Remove the retired sync:granola schedule entry."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

from solstone.think.journal_io.errors import LockTimeout, MalformedDataError
from solstone.think.schedule_config import get_schedules_path, remove_schedule_entry
from solstone.think.utils import setup_cli

MAINT_RETRY_ON_NEXT_START = True

GRANOLA_SCHEDULE_NAME = "sync:granola"
GRANOLA_BACKEND = "granola"
SYNC_EQUALS_FORM = f"--sync={GRANOLA_BACKEND}"
SYNC_SURFACES = {"journal", "sol"}
SYNC_COMMANDS = {"import", "importer"}


@dataclass
class MigrationSummary:
    removed: int = 0
    removed_names: list[str] = field(default_factory=list)
    preserved: int = 0
    preserved_names: list[str] = field(default_factory=list)
    errors: int = 0
    skipped_reason: str | None = None


def _is_retired_granola_sync(value: object) -> bool:
    if not isinstance(value, dict):
        return False

    if "cmd" not in value:
        return True

    cmd = value["cmd"]
    if not isinstance(cmd, list) or not all(isinstance(part, str) for part in cmd):
        return False
    if len(cmd) < 2:
        return False
    if cmd[0] not in SYNC_SURFACES or cmd[1] not in SYNC_COMMANDS:
        return False

    for index, part in enumerate(cmd):
        if part == SYNC_EQUALS_FORM:
            return True
        if (
            part == "--sync"
            and index + 1 < len(cmd)
            and cmd[index + 1] == GRANOLA_BACKEND
        ):
            return True

    return False


def run_migration(*, dry_run: bool) -> MigrationSummary:
    summary = MigrationSummary()
    schedules_path = get_schedules_path()

    # Read explicitly so missing, empty, and unparseable inputs keep distinct
    # skip reasons; write-side MalformedDataError stays confined to the owner
    # mutation helper below.
    if not schedules_path.exists():
        summary.skipped_reason = "no file"
        return summary

    try:
        raw_bytes = schedules_path.read_bytes()
    except OSError as exc:
        summary.errors += 1
        print(f"[ERROR] read failed: {schedules_path}: {exc}")
        return summary

    if not raw_bytes.strip():
        summary.skipped_reason = "empty file"
        return summary

    try:
        raw = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        summary.skipped_reason = "unparseable"
        return summary

    if not isinstance(raw, dict):
        summary.skipped_reason = "unparseable"
        return summary

    name = GRANOLA_SCHEDULE_NAME
    if name not in raw:
        return summary

    if not _is_retired_granola_sync(raw.get(name)):
        summary.preserved += 1
        summary.preserved_names.append(name)
        print(f"WARNING: preserving owner-divergent schedule entry {name}")
        return summary

    print(f"{'[DRY-RUN] ' if dry_run else ''}remove {name}")
    summary.removed += 1
    summary.removed_names.append(name)

    if dry_run:
        return summary

    try:
        remove_schedule_entry(name)
    except (OSError, MalformedDataError, LockTimeout) as exc:
        summary.errors += 1
        print(f"[ERROR] remove failed: {name}: {exc}")

    return summary


def _print_summary(summary: MigrationSummary) -> None:
    print("Summary")
    print(f"  removed:   {summary.removed}")
    print(f"  preserved: {summary.preserved}")
    print(f"  errors:    {summary.errors}")
    if summary.skipped_reason is not None:
        print(f"  skipped:   {summary.skipped_reason}")
    for name in summary.removed_names:
        print(f"  removed_name:   {name}")
    for name in summary.preserved_names:
        print(f"  preserved_name: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove the retired sync:granola schedule entry."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview retired schedule removal without writing files.",
    )
    args = setup_cli(parser)

    summary = run_migration(dry_run=args.dry_run)
    _print_summary(summary)
    if summary.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
