# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for chat app tools."""

from __future__ import annotations

import json

import typer

from solstone.think.convey_client import convey_cli, get_client

app = typer.Typer(help="Chat tools.")


@app.command("start")
@convey_cli
def cmd_start(
    summary: str = typer.Option(..., "--summary", help="Short request summary."),
    message: str | None = typer.Option(None, "--message", help="Optional message."),
    category: str = typer.Option(..., "--category", help="Request category."),
    dedupe: str = typer.Option(..., "--dedupe", help="Deduplication key."),
    dedupe_window: str | None = typer.Option(
        None,
        "--dedupe-window",
        help="Deduplication window, e.g. 24h.",
    ),
    since_ts: int = typer.Option(..., "--since-ts", help="Since timestamp in ms."),
    trigger_talent: str = typer.Option(
        ...,
        "--trigger-talent",
        help="Talent that triggered the request.",
    ),
) -> None:
    """Start a sol-initiated chat request."""
    if not summary.strip():
        typer.echo("Error: summary is required", err=True)
        raise typer.Exit(1)
    if len(summary.strip()) > 80:
        typer.echo("Error: summary must be 80 characters or fewer", err=True)
        raise typer.Exit(1)
    if message is not None and len(message.strip()) > 500:
        typer.echo("Error: message must be 500 characters or fewer", err=True)
        raise typer.Exit(1)
    if not dedupe.strip():
        typer.echo("Error: dedupe is required", err=True)
        raise typer.Exit(1)
    if not trigger_talent.strip():
        typer.echo("Error: trigger_talent is required", err=True)
        raise typer.Exit(1)
    if since_ts <= 0:
        typer.echo("Error: since_ts must be positive", err=True)
        raise typer.Exit(1)

    result = get_client().request(
        "POST",
        "/api/chat/start",
        json={
            "summary": summary,
            "message": message,
            "category": category,
            "dedupe": dedupe,
            "dedupe_window": dedupe_window,
            "since_ts": since_ts,
            "trigger_talent": trigger_talent,
        },
    )
    typer.echo(json.dumps(result))
