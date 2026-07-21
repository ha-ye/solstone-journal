# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Migrate legacy provider-check schedules to active-brain refresh."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from solstone.think.journal_io.errors import LockTimeout, MalformedDataError
from solstone.think.schedule_config import (
    get_schedules_path,
    remove_schedule_entry,
    set_schedule_entries,
)
from solstone.think.utils import get_journal, setup_cli

MAINT_RETRY_ON_NEXT_START = True

JOURNAL_CMD = "jour" + "nal"
SOL_CMD = "s" + "ol"
PROVIDERS_CMD = "providers"
CHECK_CMD = "check"
BRAIN_CMD = [JOURNAL_CMD, "brain", "refresh"]
PROVIDER_CHECK_PREFIXES = (
    [JOURNAL_CMD, PROVIDERS_CMD, CHECK_CMD],
    [SOL_CMD, PROVIDERS_CMD, CHECK_CMD],
)
TALENTS_HEALTH_FILENAME = "talents" + ".json"


@dataclass
class MigrationSummary:
    matched: int = 0
    installed_brain: bool = False
    preserved_brain: bool = False
    removed: int = 0
    cleanup_deleted: int = 0
    errors: int = 0
    cleanup_errors: list[str] = field(default_factory=list)
    skipped_reason: str | None = None


def _cmd_tokens(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _invokes_provider_check(name: str, entry: Any) -> bool:
    if name in {"providers", "providers-check"}:
        return True
    if not isinstance(entry, dict):
        return False
    cmd = _cmd_tokens(entry.get("cmd"))
    if cmd is None or len(cmd) < 3:
        return False
    return cmd[:3] in PROVIDER_CHECK_PREFIXES


def _invokes_brain_refresh(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    cmd = _cmd_tokens(entry.get("cmd"))
    return cmd == BRAIN_CMD


def _source_name(matches: dict[str, Any]) -> str | None:
    for preferred in ("providers", "providers-check"):
        if preferred in matches:
            return preferred
    return sorted(matches)[0] if matches else None


def _new_brain_entry(matches: dict[str, Any]) -> dict[str, Any]:
    source_name = _source_name(matches)
    source = matches.get(source_name or "", {})
    if not isinstance(source, dict):
        source = {}
    cadence = source.get("every") if isinstance(source.get("every"), str) else "daily"
    enabled = True
    if any(
        isinstance(entry, dict) and entry.get("enabled") is False
        for entry in matches.values()
    ):
        enabled = False
    elif isinstance(source.get("enabled"), bool):
        enabled = source["enabled"]
    return {
        "cmd": BRAIN_CMD[:],
        "every": cadence,
        "enabled": enabled,
        "max_runtime": "5m",
    }


def _cleanup_paths(journal: Path) -> tuple[list[Path], list[Path]]:
    health = journal / "health"
    fixed = [
        health / TALENTS_HEALTH_FILENAME,
        health / f"{TALENTS_HEALTH_FILENAME}.lock",
        health / "recheck.lock",
        health / "providers.log",
    ]
    return fixed, list(health.glob(f".{TALENTS_HEALTH_FILENAME}.*.tmp"))


def _unlink_if_present(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    path.unlink()
    return True


def _run_cleanup(summary: MigrationSummary, *, dry_run: bool) -> None:
    journal = Path(get_journal())
    fixed, globs = _cleanup_paths(journal)
    for path in [*fixed, *globs]:
        try:
            if dry_run:
                if path.exists() or path.is_symlink():
                    print(f"[DRY-RUN] delete {path}")
                continue
            if _unlink_if_present(path):
                summary.cleanup_deleted += 1
                print(f"delete {path}")
        except OSError as exc:
            summary.errors += 1
            message = f"[ERROR] delete failed: {path}: {exc}"
            summary.cleanup_errors.append(message)
            print(message)


def run_migration(*, dry_run: bool) -> MigrationSummary:
    summary = MigrationSummary()
    schedules_path = get_schedules_path()
    raw: dict[str, Any] = {}

    if schedules_path.exists():
        try:
            raw_bytes = schedules_path.read_bytes()
            raw_obj = json.loads(raw_bytes) if raw_bytes.strip() else {}
        except (OSError, json.JSONDecodeError) as exc:
            summary.errors += 1
            print(f"[ERROR] read failed: {schedules_path}: {exc}")
            return summary
        if not isinstance(raw_obj, dict):
            summary.errors += 1
            print(f"[ERROR] malformed schedules: {schedules_path}")
            return summary
        raw = raw_obj
    else:
        summary.skipped_reason = "no schedules file"

    matches = {
        name: value
        for name, value in raw.items()
        if _invokes_provider_check(name, value)
    }
    summary.matched = len(matches)

    if _invokes_brain_refresh(raw.get("brain")):
        summary.preserved_brain = True
    else:
        brain_entry = _new_brain_entry(matches)
        summary.installed_brain = True
        print(f"{'[DRY-RUN] ' if dry_run else ''}set brain: {brain_entry!r}")
        if not dry_run:
            try:
                set_schedule_entries({"brain": brain_entry})
            except (OSError, MalformedDataError, LockTimeout) as exc:
                summary.errors += 1
                print(f"[ERROR] write failed: {schedules_path}: {exc}")
                return summary

    for name in sorted(matches):
        if name == "brain":
            continue
        print(f"{'[DRY-RUN] ' if dry_run else ''}remove {name}")
        if dry_run:
            continue
        try:
            remove_schedule_entry(name)
            summary.removed += 1
        except (OSError, MalformedDataError, LockTimeout) as exc:
            summary.errors += 1
            print(f"[ERROR] remove failed: {name}: {exc}")

    _run_cleanup(summary, dry_run=dry_run)
    return summary


def _print_summary(summary: MigrationSummary) -> None:
    print("Summary")
    print(f"  matched:        {summary.matched}")
    print(f"  installed:      {summary.installed_brain}")
    print(f"  preserved:      {summary.preserved_brain}")
    print(f"  removed:        {summary.removed}")
    print(f"  cleanup_deleted:{summary.cleanup_deleted}")
    print(f"  errors:         {summary.errors}")
    if summary.skipped_reason is not None:
        print(f"  skipped:        {summary.skipped_reason}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate provider-check schedules to active-brain refresh."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview planned changes without writing files.",
    )
    args = setup_cli(parser)

    summary = run_migration(dry_run=args.dry_run)
    _print_summary(summary)
    if summary.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
