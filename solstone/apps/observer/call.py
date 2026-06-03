# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for observer owner operations."""

from __future__ import annotations

import json

import typer

from solstone.apps.observer.share_delete import SHARE_STREAM, delete_source_stream

app = typer.Typer(help="Observer data operations.")


@app.command("delete-source")
def delete_source() -> None:
    """Delete everything the iOS Share Sheet contributed and print a receipt."""
    receipt = delete_source_stream(SHARE_STREAM)
    typer.echo(json.dumps(receipt, indent=2))
