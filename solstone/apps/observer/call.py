# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for observer owner operations.

Every verb reaches the journal only over HTTP via the Convey client; this
module imports no journal/domain function and performs no filesystem I/O.
"""

from __future__ import annotations

import json

import typer

from solstone.think.convey_client import convey_cli, get_client

app = typer.Typer(help="Observer data operations.")


@app.command("delete-source")
@convey_cli
def delete_source() -> None:
    """Delete everything the iOS Share Sheet contributed and print a receipt."""
    receipt = get_client().request("POST", "/app/observer/api/delete-source")
    typer.echo(json.dumps(receipt, indent=2))
