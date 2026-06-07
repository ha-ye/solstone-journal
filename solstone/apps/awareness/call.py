# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for the awareness system.

Auto-discovered by ``think.call`` and mounted as ``sol call awareness ...``.
Every verb reaches the journal only over HTTP via the Convey client; this
module imports no journal/domain function and performs no filesystem I/O.
"""

import json

import typer

from solstone.think.convey_client import convey_cli, get_client

app = typer.Typer(help="Awareness system — solstone's self-knowledge.")

_LOG_PAGE_SIZE = 100  # the awareness /api/log route's max_limit


@app.command("status")
@convey_cli
def status(
    section: str | None = typer.Argument(
        None, help="Section to read (e.g., 'journal'). Omit for all."
    ),
) -> None:
    """Show current awareness state."""
    state = get_client().request("GET", "/app/awareness/api/state")
    if not state:
        typer.echo("No awareness state yet.")
        return

    if section:
        value = state.get(section)
        if value is None:
            typer.echo(f"No '{section}' state.")
            return
        typer.echo(json.dumps(value, indent=2))
    else:
        typer.echo(json.dumps(state, indent=2))


@app.command("imports")
@convey_cli
def imports_cmd(
    record: str | None = typer.Option(
        None, "--record", "-r", help="Record a completed import (source type)."
    ),
    declined: bool = typer.Option(
        False, "--declined", help="Record that user declined import offer."
    ),
    nudge: bool = typer.Option(
        False, "--nudge", help="Record that triage nudged about imports."
    ),
) -> None:
    """Read or update import tracking state."""
    client = get_client()
    if record:
        state = client.request(
            "POST", "/app/awareness/api/imports", json={"record": record}
        )
    elif declined:
        state = client.request(
            "POST", "/app/awareness/api/imports", json={"declined": True}
        )
    elif nudge:
        state = client.request(
            "POST", "/app/awareness/api/imports", json={"nudge": True}
        )
    else:
        state = client.request("GET", "/app/awareness/api/imports")
    typer.echo(json.dumps(state, indent=2))


@app.command("log-read")
@convey_cli
def log_read_cmd(
    day: str | None = typer.Argument(
        None, help="Day in YYYYMMDD format (defaults to today)."
    ),
    kind: str | None = typer.Option(
        None, "--kind", "-k", help="Filter by entry kind (e.g., 'observation')."
    ),
    limit: int = typer.Option(
        0, "--limit", "-n", help="Max entries to return (0=all)."
    ),
) -> None:
    """Read entries from the daily awareness log."""
    client = get_client()
    entries: list = []
    offset = 0
    while True:
        params: dict = {"limit": _LOG_PAGE_SIZE, "offset": offset}
        if day:
            params["day"] = day
        if kind:
            params["kind"] = kind
        body = client.request("GET", "/app/awareness/api/log", params=params)
        items = body["items"]
        entries.extend(items)
        offset += len(items)
        if not items or len(entries) >= body["total"]:
            break

    if limit > 0:
        entries = entries[-limit:]

    if not entries:
        typer.echo("No entries found.")
        return

    typer.echo(json.dumps(entries, indent=2))


@app.command("log")
@convey_cli
def log_cmd(
    kind: str = typer.Argument(
        help="Entry type: state, observation, nudge, interaction."
    ),
    message: str | None = typer.Argument(None, help="Human-readable message."),
    key: str | None = typer.Option(
        None, "--key", "-k", help="Dotted key for state entries."
    ),
    data: str | None = typer.Option(None, "--data", "-d", help="JSON data payload."),
) -> None:
    """Append an entry to the daily awareness log."""
    parsed_data = None
    if data:
        try:
            parsed_data = json.loads(data)
        except json.JSONDecodeError:
            typer.echo("Error: --data must be valid JSON", err=True)
            raise typer.Exit(1)

    entry = get_client().request(
        "POST",
        "/app/awareness/api/log",
        json={"kind": kind, "key": key, "message": message, "data": parsed_data},
    )
    typer.echo(json.dumps(entry, indent=2))
