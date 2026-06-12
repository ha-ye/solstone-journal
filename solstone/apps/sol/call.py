# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for the agent identity system.

Every verb reaches the journal only over HTTP via the Convey client; this
module imports no journal/domain function and performs no filesystem I/O.
Auto-discovered by ``think.call`` and mounted as ``sol call sol ...``.
"""

import json

import typer

from solstone.think.convey_client import convey_cli, get_client

app = typer.Typer(help="Agent identity — name and status.")


@app.command("set-name")
@convey_cli
def set_name(
    name: str = typer.Argument(..., help="New agent name."),
    status: str = typer.Option(
        "chosen",
        "--status",
        "-s",
        help="Name status (chosen, self-named, deferred, default).",
    ),
) -> None:
    """Set the agent name."""
    agent = get_client().request(
        "POST",
        "/app/sol/api/set-name",
        json={"name": name, "status": status},
    )
    typer.echo(json.dumps(agent, indent=2))


@app.command("reset")
@convey_cli
def reset() -> None:
    """Reset the agent name to default."""
    agent = get_client().request("POST", "/app/sol/api/reset")
    typer.echo(json.dumps(agent, indent=2))


@app.command("set-owner")
@convey_cli
def set_owner(
    name: str = typer.Argument(..., help="Owner name."),
    bio: str = typer.Option(None, "--bio", "-b", help="Short owner bio."),
) -> None:
    """Set the journal owner's name (and optional bio)."""
    body = get_client().request(
        "POST",
        "/app/sol/api/set-owner",
        json={"name": name, "bio": bio},
    )
    typer.echo(json.dumps(body, indent=2))


@app.command("sol-init")
@convey_cli
def sol_init() -> None:
    """Initialize the identity directory."""
    body = get_client().request("POST", "/app/sol/api/sol-init")
    typer.echo(json.dumps(body, indent=2))
