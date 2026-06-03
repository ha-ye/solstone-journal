# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for facet review candidates."""

from __future__ import annotations

import json

import typer

from solstone.think.curation import (
    accept_facet_candidate,
    dismiss_facet_candidate,
)
from solstone.think.facet_review_candidates import load_candidates
from solstone.think.utils import require_solstone

app = typer.Typer(help="Facet review candidates.")


@app.callback()
def _require_up() -> None:
    require_solstone()


def _echo_result(result: dict, *, action: str, name_key: str) -> None:
    status = result.get("status")
    if status == "error":
        typer.echo(f"Error: {result.get('error', 'operation failed')}", err=True)
        raise typer.Exit(1)
    if status == "accepted":
        typer.echo(
            f"Accepted facet candidate '{name_key}' as '{result.get('facet_slug')}'."
        )
        return
    if status == "dismissed":
        typer.echo(f"Dismissed facet candidate '{name_key}'.")
        return
    if status == "already_accepted":
        typer.echo(f"Facet candidate '{name_key}' already accepted.")
        return
    if status == "already_dismissed":
        typer.echo(f"Facet candidate '{name_key}' already dismissed.")
        return
    typer.echo(f"{action} result for '{name_key}': {status}")


@app.command("list-candidates")
def list_facet_candidates(
    status: str | None = typer.Option(None, "--status", help="Filter by status."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List recorded facet review candidates."""
    rows = load_candidates()
    if status is not None:
        rows = [row for row in rows if row.get("status") == status]

    if json_output:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return

    if not rows:
        typer.echo("No facet candidates found.")
        return

    for row in rows:
        typer.echo(
            f"{row.get('name', '')}  "
            f"[{row.get('status', '')}]  "
            f"count={row.get('count')}  "
            f"last={row.get('last_surfaced', '')}"
        )


@app.command("accept")
def accept_facet(
    name_key: str = typer.Argument(help="Facet candidate name_key to accept."),
) -> None:
    """Accept one facet review candidate."""
    result = accept_facet_candidate(name_key)
    _echo_result(result, action="accept", name_key=name_key)


@app.command("dismiss")
def dismiss_facet(
    name_key: str = typer.Argument(help="Facet candidate name_key to dismiss."),
) -> None:
    """Dismiss one facet review candidate."""
    result = dismiss_facet_candidate(name_key)
    _echo_result(result, action="dismiss", name_key=name_key)
