# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI for app-owned scheduled maintenance routines."""

import sys

import typer

from solstone.think.journal_io.errors import MalformedDataError
from solstone.think.maintenance import (
    MaintenanceDescriptorError,
    discover_routines,
    get_routine_statuses,
    register_maintenance_schedules,
)
from solstone.think.schedule_config import get_schedules_path, read_schedules
from solstone.think.utils import init_cli_runtime

app = typer.Typer(help="Manage app-owned scheduled maintenance routines")


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


@app.callback()
def _configure(
    verbose: bool = typer.Option(
        False, "-v", "--verbose", help="Enable verbose output"
    ),
    debug: bool = typer.Option(False, "-d", "--debug", help="Enable debug logging"),
) -> None:
    """Manage app-owned scheduled maintenance routines."""
    init_cli_runtime(verbose, debug)


@app.command("list")
def list_routines() -> None:
    """List maintenance routines and schedule status."""
    try:
        routines = discover_routines()
        if not routines:
            print("No maintenance routines found.")
            return

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
    except (MalformedDataError, OSError) as exc:
        _print_schedule_error(exc)
        raise typer.Exit(1)
    except MaintenanceDescriptorError as exc:
        print(str(exc), file=sys.stderr)
        raise typer.Exit(1)


@app.command("sync")
def sync() -> None:
    """Register missing maintenance schedules."""
    try:
        summary = register_maintenance_schedules()
        _print_summary(summary)
    except (MalformedDataError, OSError) as exc:
        _print_schedule_error(exc)
        raise typer.Exit(1)
    except MaintenanceDescriptorError as exc:
        print(str(exc), file=sys.stderr)
        raise typer.Exit(1)


@app.command(
    "run",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
def run_routine(
    routine_id: str = typer.Argument(..., metavar="id"),
    routine_args: list[str] = typer.Argument(None, metavar="args"),
) -> None:
    """Run one maintenance routine."""
    try:
        routines = discover_routines()
        routine = routines.get(routine_id)
        if routine is None:
            print(
                f"Unknown maintenance routine: {routine_id}. "
                "Run `journal maintenance list` to see available routines.",
                file=sys.stderr,
            )
            raise typer.Exit(1)
        raise typer.Exit(int(routine.run(list(routine_args or []))))
    except MaintenanceDescriptorError as exc:
        print(str(exc), file=sys.stderr)
        raise typer.Exit(1)


def main() -> None:
    """Entry point for ``journal maintenance``."""
    app()


if __name__ == "__main__":
    main()
