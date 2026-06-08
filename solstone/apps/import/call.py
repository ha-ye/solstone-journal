# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for import review and resolution.

Auto-discovered by ``think.call`` and mounted as ``sol call import ...``.
Every verb reaches the journal only over HTTP via the Convey client; this
module imports no journal/domain function and performs no filesystem I/O.
"""

from __future__ import annotations

import json

import typer

from solstone.convey.reasons import (
    IMPORT_NOT_FOUND,
    INVALID_REQUEST_VALUE,
    JOURNAL_SOURCE_PROBLEM,
)
from solstone.think.convey_client import ConveyClientError, get_client

app = typer.Typer(help="Import review and resolution.")


def _fail(message: str) -> None:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)


def _params(**values: object) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}


def _handle_import_error(
    err: ConveyClientError, *, source: str, list_staged: bool = False
) -> None:
    if err.reason_code == JOURNAL_SOURCE_PROBLEM.code:
        _fail(
            f"Import source '{source}' not found. Check available sources in "
            "~/.local/share/solstone/app-storage/import/journal_sources/."
        )
    if list_staged and err.reason_code == INVALID_REQUEST_VALUE.code:
        _fail("Area must be one of: entities, facets, config.")
    if err.reason_code in {IMPORT_NOT_FOUND.code, INVALID_REQUEST_VALUE.code}:
        if err.detail:
            _fail(err.detail)
    typer.echo(err.error, err=True)
    raise typer.Exit(1)


@app.command("list-staged")
def list_staged(
    source: str = typer.Option(..., "--source", help="Import source name."),
    area: str | None = typer.Option(
        None, "--area", help="Area: entities, facets, or config."
    ),
) -> None:
    try:
        body = get_client().request(
            "GET",
            f"/app/import/api/journal-sources/{source}/staged",
            params=_params(area=area),
        )
    except ConveyClientError as err:
        _handle_import_error(err, source=source, list_staged=True)

    items = body.get("items", []) if isinstance(body, dict) else []
    # The route omits non-dict config diffs; the old in-process CLI errored there.
    for item in items:
        typer.echo(json.dumps(item, ensure_ascii=False))


@app.command("resolve-entity")
def resolve_entity(
    source_id: str = typer.Argument(help="Source entity ID."),
    action: str = typer.Argument(help="Action: merge, create, or skip."),
    source: str = typer.Option(..., "--source", help="Import source name."),
    target: str | None = typer.Option(
        None, "--target", help="Target entity ID for merge."
    ),
) -> None:
    try:
        body = get_client().request(
            "POST",
            f"/app/import/api/journal-sources/{source}/resolve-entity",
            json={"source_id": source_id, "action": action, "target": target},
        )
    except ConveyClientError as err:
        _handle_import_error(err, source=source)

    if action == "merge":
        typer.echo(f"Merged {source_id} into {target}.")
    elif action == "create":
        target_id = body.get("target_id") if isinstance(body, dict) else None
        typer.echo(f"Created entity {target_id} from {source_id}.")
    else:
        typer.echo(f"Skipped staged entity {source_id}.")


@app.command("resolve-staged-facet")
def apply_staged_facet(
    staged_file: str = typer.Argument(
        help="Staged file path relative to facets/staged/."
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Apply the staged item into the journal."
    ),
    skip: bool = typer.Option(False, "--skip", help="Discard the staged item."),
    source: str = typer.Option(..., "--source", help="Import source name."),
) -> None:
    if apply == skip:
        _fail("Exactly one of --apply or --skip is required.")

    mode = "apply" if apply else "skip"
    try:
        get_client().request(
            "POST",
            f"/app/import/api/journal-sources/{source}/resolve-facet",
            json={"staged_file": staged_file, "mode": mode},
        )
    except ConveyClientError as err:
        _handle_import_error(err, source=source)

    if mode == "apply":
        typer.echo(f"Applied staged facet file {staged_file}.")
    else:
        typer.echo(f"Skipped staged facet file {staged_file}.")


@app.command("resolve-config")
def resolve_config(
    field: str = typer.Argument(help="Dotted config field path."),
    action: str = typer.Argument(help="Action: apply or keep."),
    source: str = typer.Option(..., "--source", help="Import source name."),
) -> None:
    try:
        get_client().request(
            "POST",
            f"/app/import/api/journal-sources/{source}/resolve-config",
            json={"field": field, "action": action},
        )
    except ConveyClientError as err:
        _handle_import_error(err, source=source)

    typer.echo(f"Resolved config field {field} with action {action}.")


@app.command("resolve-config-all")
def resolve_config_all(
    source: str = typer.Option(..., "--source", help="Import source name."),
    category: str = typer.Option(
        ..., "--category", help="Category: transferable or preference."
    ),
) -> None:
    try:
        body = get_client().request(
            "POST",
            f"/app/import/api/journal-sources/{source}/resolve-config-all",
            json={"category": category},
        )
    except ConveyClientError as err:
        _handle_import_error(err, source=source)

    count = body.get("count", 0) if isinstance(body, dict) else 0
    typer.echo(f"Applied {count} {category} config field(s).")
