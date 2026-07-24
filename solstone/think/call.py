# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI interface for the journal compatibility subtree.

The native `sol` binary owns migrated `sol call <app> <verb>` commands. The
remaining Python Typer surface is exactly `sol call journal ...`, mounted here
for the private native compatibility helper.
"""

import typer

from solstone.think.tools.call import app as journal_app

call_app = typer.Typer(
    name="call",
    help="Call journal functions from the command line.",
    no_args_is_help=True,
)

call_app.add_typer(journal_app, name="journal")


def main() -> None:
    """Entry point for the private ``sol call journal`` compatibility path."""
    from solstone.think.utils import CorruptConfigError

    try:
        call_app()
    except CorruptConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
