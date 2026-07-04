# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for body app views.

Auto-discovered by ``think.call`` and mounted as ``sol call body``.
This module reaches journal data only over the Convey HTTP client.
"""

from __future__ import annotations

import json

import typer

from solstone.think.convey_client import convey_cli, get_client

app = typer.Typer(help="Imported health data views.")


def _echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("status")
@convey_cli
def status(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print the full JSON response.",
    ),
) -> None:
    """Show imported health data status."""

    payload = get_client().request("GET", "/app/body/api/status")
    if json_output:
        _echo_json(payload)
        return

    if not isinstance(payload, dict):
        typer.echo("I couldn't read the response from the body app.", err=True)
        raise typer.Exit(1)

    imports = payload.get("imports") or []
    normalized = payload.get("normalized") or {}
    coverage = payload.get("coverage_window") or {}
    typer.echo(f"imports: {len(imports)}")
    typer.echo(f"entries: {normalized.get('total', 0)}")
    if coverage.get("start"):
        typer.echo(f"coverage: {coverage.get('start')} to {coverage.get('end')}")


@app.command("day")
@convey_cli
def day(
    day_value: str = typer.Argument(..., help="Day as YYYYMMDD."),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print the full JSON response.",
    ),
) -> None:
    """Show one day of imported health data."""

    payload = get_client().request("GET", f"/app/body/api/day/{day_value}")
    if json_output:
        _echo_json(payload)
        return

    if not isinstance(payload, dict):
        typer.echo("I couldn't read the response from the body app.", err=True)
        raise typer.Exit(1)

    glucose = payload.get("glucose") or {}
    typer.echo(f"day: {payload.get('day', day_value)}")
    typer.echo(f"entries: {payload.get('entry_total', 0)}")
    typer.echo(
        "glucose: "
        f"count={glucose.get('count', 0)} "
        f"min={glucose.get('min')} "
        f"max={glucose.get('max')} "
        f"mean={glucose.get('mean')} "
        f"unit={glucose.get('unit')}"
    )
