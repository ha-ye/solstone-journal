# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for todo management.

Auto-discovered by ``think.call`` and mounted as ``sol call todos ...``.
Every verb reaches the journal only over HTTP via the Convey client; this
module imports no journal/domain function and performs no filesystem I/O.
"""

import json
import os
import subprocess
from datetime import datetime
from typing import Any

import typer

from solstone.convey.reasons import (
    INVALID_DAY,
    INVALID_REQUEST_VALUE,
    OPERATION_NO_LONGER_AVAILABLE,
    TODO_BUSY,
    TODO_OPERATION_FAILED,
)
from solstone.think.convey_client import ConveyClientError, get_client

app = typer.Typer(help="Todo checklist management.")


def _get_sol_facet() -> str | None:
    return os.environ.get("SOL_FACET") or None


def _resolve_sol_day(arg: str | None) -> str:
    if arg:
        return arg
    env = os.environ.get("SOL_DAY") or None
    if env:
        return env
    typer.echo("Error: day is required (pass as argument or set SOL_DAY).", err=True)
    raise typer.Exit(1)


def _resolve_sol_day_or_today(arg: str | None) -> str:
    if arg:
        return arg
    env = os.environ.get("SOL_DAY") or None
    if env:
        return env
    return datetime.now().strftime("%Y%m%d")


def _resolve_sol_facet(arg: str | None) -> str:
    if arg:
        return arg
    env = _get_sol_facet()
    if env:
        return env
    typer.echo(
        "Error: facet is required (pass as argument or set SOL_FACET).", err=True
    )
    raise typer.Exit(1)


def _exit_with(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def _handle_todo_error(
    err: ConveyClientError,
    *,
    from_facet: str | None = None,
    to_facet: str | None = None,
    warn_operation_failed: bool = False,
) -> None:
    detail = err.detail or ""
    if err.reason_code == TODO_BUSY.code:
        _exit_with("Error: todo list is busy, try again.")
    if err.reason_code == INVALID_REQUEST_VALUE.code and detail:
        if from_facet is not None and to_facet is not None:
            if detail == from_facet:
                _exit_with(f"Error: Facet '{detail}' (--from) does not exist.")
            if detail == to_facet:
                _exit_with(f"Error: Facet '{detail}' (--to) does not exist.")
        _exit_with(f"Error: {detail}")
    if err.reason_code in {INVALID_DAY.code, OPERATION_NO_LONGER_AVAILABLE.code}:
        if detail:
            _exit_with(f"Error: {detail}")
    if err.reason_code == TODO_OPERATION_FAILED.code:
        if detail and warn_operation_failed:
            typer.echo(f"Warning: {detail}", err=True)
            raise typer.Exit(1)
        if detail:
            _exit_with(f"Error: {detail}")

    typer.echo(err.error, err=True)
    raise typer.Exit(1)


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    return get_client().request(method, path, params=params, json=json_body)


def _print_checklist_response(body: dict[str, Any]) -> None:
    if body["mode"] == "range":
        facet_count = int(body["facet_count"])
        for section in body["sections"]:
            if facet_count > 1:
                typer.echo(f"## {section['facet']}")
            for day_entry in section["days"]:
                if day_entry["has_items"]:
                    typer.echo(f"### {day_entry['day']}")
                    typer.echo(day_entry["display"])
                    typer.echo()
        return

    day = body["day"]
    facets = body["facets"]
    if not facets:
        typer.echo(f"No todos found for {day}.")
        return

    if len(facets) == 1:
        entry = facets[0]
        if not entry["has_items"]:
            typer.echo(f"No todos found for {day}.")
            return
        typer.echo(entry["display"])
        return

    for entry in facets:
        typer.echo(f"## {entry['facet']}")
        if entry["has_items"]:
            typer.echo(entry["display"])
        typer.echo()


@app.command("list")
def list_todos(
    day: str | None = typer.Argument(
        None, help="Journal day in YYYYMMDD format (or use --day / SOL_DAY)."
    ),
    day_opt: str | None = typer.Option(
        None,
        "--day",
        "-d",
        help="Journal day (alternative to the positional argument; or set SOL_DAY).",
    ),
    facet: str | None = typer.Option(
        None, "--facet", "-f", help="Facet name. Omit to show all facets."
    ),
    to: str | None = typer.Option(
        None, "--to", help="End day for range query (YYYYMMDD, inclusive)."
    ),
) -> None:
    """Show the todo checklist for a day (or date range)."""
    if day is not None and day_opt is not None and day != day_opt:
        _exit_with(
            f"Error: conflicting day given as argument ({day}) and --day ({day_opt})."
        )
    day = _resolve_sol_day_or_today(day if day is not None else day_opt)
    if facet is None:
        facet = _get_sol_facet()

    if to is not None and to < day:
        _exit_with(f"Error: --to ({to}) must not be before day ({day})")

    params: dict[str, Any] = {"day": day}
    if facet is not None:
        params["facet"] = facet
    if to is not None:
        params["to"] = to

    try:
        body = _request("GET", "/app/todos/api/checklist", params=params)
    except ConveyClientError as err:
        _handle_todo_error(err)
    _print_checklist_response(body)


@app.command("add")
def add_todo(
    text: str = typer.Argument(help="Todo item text."),
    day: str | None = typer.Option(
        None, "--day", "-d", help="Journal day in YYYYMMDD format (or set SOL_DAY)."
    ),
    facet: str | None = typer.Option(
        None, "--facet", "-f", help="Facet name (or set SOL_FACET)."
    ),
    nudge: str | None = typer.Option(
        None,
        "--nudge",
        "-n",
        help="Nudge time: HH:MM, now, tomorrow HH:MM, or YYYYMMDDTHH:MM.",
    ),
    force: bool = typer.Option(
        False, "--force", help="Skip duplicate check and add anyway."
    ),
) -> None:
    """Add a new todo item."""
    day = _resolve_sol_day(day)
    facet = _resolve_sol_facet(facet)

    try:
        datetime.strptime(day, "%Y%m%d")
    except ValueError:
        _exit_with(f"Error: invalid day format '{day}'")

    try:
        body = _request(
            "POST",
            "/app/todos/api/add",
            json_body={
                "text": text,
                "day": day,
                "facet": facet,
                "nudge": nudge,
                "force": force,
            },
        )
    except ConveyClientError as err:
        _handle_todo_error(err)

    if body.get("status") == "duplicate":
        typer.echo(f"Duplicate detected for: {text}", err=True)
        for match in body["matches"]:
            typer.echo(
                f"  [{match['score']:.0f}%] {match['facet']}/{match['day']} "
                f"line {match['line']}: {match['text']}",
                err=True,
            )
        typer.echo("Use --force to add anyway.", err=True)
        raise typer.Exit(1)

    typer.echo(body["display"])


@app.command("done")
def done_todo(
    line_number: int = typer.Argument(help="1-based line number of the todo."),
    day: str | None = typer.Option(
        None, "--day", "-d", help="Journal day in YYYYMMDD format (or set SOL_DAY)."
    ),
    facet: str | None = typer.Option(
        None, "--facet", "-f", help="Facet name (or set SOL_FACET)."
    ),
) -> None:
    """Mark a todo item as done."""
    day = _resolve_sol_day(day)
    facet = _resolve_sol_facet(facet)

    try:
        body = _request(
            "POST",
            "/app/todos/api/done",
            json_body={"day": day, "facet": facet, "line_number": line_number},
        )
    except ConveyClientError as err:
        _handle_todo_error(err)
    typer.echo(body["display"])


@app.command("cancel")
def cancel_todo(
    line_number: int = typer.Argument(help="1-based line number of the todo."),
    day: str | None = typer.Option(
        None, "--day", "-d", help="Journal day in YYYYMMDD format (or set SOL_DAY)."
    ),
    facet: str | None = typer.Option(
        None, "--facet", "-f", help="Facet name (or set SOL_FACET)."
    ),
) -> None:
    """Cancel a todo item."""
    day = _resolve_sol_day(day)
    facet = _resolve_sol_facet(facet)

    try:
        body = _request(
            "POST",
            "/app/todos/api/cancel",
            json_body={"day": day, "facet": facet, "line_number": line_number},
        )
    except ConveyClientError as err:
        _handle_todo_error(err)
    typer.echo(body["display"])


@app.command("move")
def move_todo(
    line_number: int = typer.Argument(
        help="Line number of the todo to move (1-indexed)."
    ),
    day: str = typer.Option(..., "--day", help="Day in YYYYMMDD format."),
    from_facet: str = typer.Option(..., "--from", help="Source facet."),
    to_facet: str = typer.Option(..., "--to", help="Destination facet."),
    consent: bool = typer.Option(
        False,
        "--consent",
        help="Assert that explicit user approval was obtained before calling this command (agent audit trail).",
    ),
) -> None:
    """Move an open todo from one facet to another."""
    try:
        body = _request(
            "POST",
            "/app/todos/api/move",
            json_body={
                "day": day,
                "from_facet": from_facet,
                "to_facet": to_facet,
                "line_number": line_number,
                "consent": consent,
            },
        )
    except ConveyClientError as err:
        _handle_todo_error(
            err,
            from_facet=from_facet,
            to_facet=to_facet,
            warn_operation_failed=True,
        )

    cancelled = body["cancelled"]
    typer.echo(
        f"Moved todo {line_number} ('{cancelled['text']}') "
        f"from '{from_facet}' to '{to_facet}'."
    )


@app.command("upcoming")
def upcoming_todos(
    limit: int = typer.Option(20, "--limit", "-l", help="Maximum number of todos."),
    facet: str | None = typer.Option(
        None, "--facet", "-f", help="Facet name. Omit to show all facets."
    ),
) -> None:
    """Show upcoming todos across future days."""
    if facet is None:
        facet = _get_sol_facet()

    params: dict[str, Any] = {"limit": limit}
    if facet is not None:
        params["facet"] = facet
    try:
        body = _request("GET", "/app/todos/api/upcoming", params=params)
    except ConveyClientError as err:
        _handle_todo_error(err)
    typer.echo(body["upcoming"])


@app.command("list-nudges-due")
def list_nudges_due(
    facet: str | None = typer.Option(
        None,
        "--facet",
        "-f",
        help="Facet name (or set SOL_FACET). Omit to check all facets.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List due, unnotified todo nudges."""
    params: dict[str, Any] = {}
    if facet is not None:
        params["facet"] = facet
    try:
        body = _request("GET", "/app/todos/api/nudges-due", params=params)
    except ConveyClientError as err:
        _handle_todo_error(err)
    items = body["items"]

    if json_output:
        payload = [
            {
                "day": item["day"],
                "facet": item["facet"],
                "index": item["index"],
                "text": item["text"],
                "nudge": item["nudge"],
                "nudge_display": item["nudge_display"],
            }
            for item in items
        ]
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if not items:
        typer.echo("No nudges due.")
        return

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(item["facet"], []).append(item)

    if len(grouped) == 1:
        for item in next(iter(grouped.values())):
            typer.echo(f"{item['index']}: {item['display_line']}")
        return

    for facet_name, facet_items in grouped.items():
        typer.echo(f"## {facet_name}")
        for item in facet_items:
            typer.echo(f"{item['index']}: {item['display_line']}")
        typer.echo()


@app.command("dispatch-nudges")
def dispatch_nudges(
    facet: str | None = typer.Option(
        None,
        "--facet",
        "-f",
        help="Facet name (or set SOL_FACET). Omit to check all facets.",
    ),
) -> None:
    """Dispatch due, unnotified todo nudges."""
    body: dict[str, Any]
    json_body: dict[str, Any] = {}
    if facet is not None:
        json_body["facet"] = facet
    try:
        body = _request("POST", "/app/todos/api/dispatch-nudges", json_body=json_body)
    except ConveyClientError as err:
        _handle_todo_error(err)

    for item in body["items"]:
        try:
            subprocess.run(
                [
                    "sol",
                    "notify",
                    item["text"],
                    "--title",
                    "Todo Reminder",
                    "--icon",
                    "✅",
                    "--app",
                    "todos",
                    "--facet",
                    item["facet"],
                    "--action",
                    f"/app/todos/{body['day']}",
                ],
                check=False,
                capture_output=True,
            )
        except FileNotFoundError:
            pass

    typer.echo(f"dispatched {len(body['items'])} nudge(s)")
