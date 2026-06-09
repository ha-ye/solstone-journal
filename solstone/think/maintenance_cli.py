# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI for app-owned scheduled maintenance routines."""

from __future__ import annotations

import argparse
import sys

from solstone.think.journal_io.errors import MalformedDataError
from solstone.think.maintenance import (
    MaintenanceDescriptorError,
    discover_routines,
    get_routine_statuses,
    register_maintenance_schedules,
)
from solstone.think.schedule_config import get_schedules_path, read_schedules
from solstone.think.utils import setup_cli


def _print_summary(summary: dict[str, list[str]]) -> None:
    for key in ("added", "synced", "divergent", "disabled"):
        ids = summary[key]
        suffix = f": {', '.join(ids)}" if ids else ""
        print(f"{key}: {len(ids)}{suffix}")

    for routine_id in summary["divergent"]:
        print(f"WARNING: {routine_id} schedule is divergent; preserved unchanged")
    for routine_id in summary["disabled"]:
        print(f"WARNING: {routine_id} schedule is disabled; preserved unchanged")


def _print_schedule_error(exc: BaseException) -> None:
    path = get_schedules_path()
    cause = exc.__cause__ or exc
    print(f"Error reading/updating {path}: {exc} (cause: {cause})", file=sys.stderr)


def _cmd_list(_args: argparse.Namespace) -> int:
    routines = discover_routines()
    if not routines:
        print("No maintenance routines found.")
        return 0

    raw_schedules = read_schedules()
    statuses = get_routine_statuses(routines, raw_schedules)
    id_width = max(max(len(routine_id) for routine_id in routines), 2)
    every_width = 8
    status_width = 9
    runtime_width = 11

    print(
        f"  {'ID':<{id_width}}  {'EVERY':<{every_width}}  "
        f"{'STATUS':<{status_width}}  {'MAX RUNTIME':<{runtime_width}}  DESCRIPTION"
    )
    for routine_id, routine in routines.items():
        max_runtime = routine.max_runtime or "-"
        print(
            f"  {routine_id:<{id_width}}  {routine.every:<{every_width}}  "
            f"{statuses[routine_id]:<{status_width}}  "
            f"{max_runtime:<{runtime_width}}  {routine.description}"
        )
    return 0


def _cmd_sync(_args: argparse.Namespace) -> int:
    summary = register_maintenance_schedules()
    _print_summary(summary)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    routines = discover_routines()
    routine_id = args.routine_id
    routine = routines.get(routine_id)
    if routine is None:
        print(
            f"Unknown maintenance routine: {routine_id}. "
            "Run `journal maintenance list` to see available routines.",
            file=sys.stderr,
        )
        return 1
    return int(routine.run(list(args.routine_args)))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage app-owned scheduled maintenance routines"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="List maintenance routines and schedule status")
    subparsers.add_parser("sync", help="Register missing maintenance schedules")

    run_parser = subparsers.add_parser("run", help="Run one maintenance routine")
    run_parser.add_argument("routine_id", metavar="id")
    run_parser.add_argument("routine_args", nargs=argparse.REMAINDER, metavar="args")
    return parser


def main() -> None:
    """Entry point for ``journal maintenance``."""
    parser = _build_parser()
    args = setup_cli(parser)

    try:
        if args.command == "list":
            try:
                exit_code = _cmd_list(args)
            except (MalformedDataError, OSError) as exc:
                _print_schedule_error(exc)
                exit_code = 1
        elif args.command == "sync":
            try:
                exit_code = _cmd_sync(args)
            except (MalformedDataError, OSError) as exc:
                _print_schedule_error(exc)
                exit_code = 1
        elif args.command == "run":
            exit_code = _cmd_run(args)
        else:  # pragma: no cover - argparse enforces choices
            parser.error(f"unknown command: {args.command}")
            exit_code = 2
    except MaintenanceDescriptorError as exc:
        print(str(exc), file=sys.stderr)
        exit_code = 1

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
