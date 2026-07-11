# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI commands for entity management.

Auto-discovered by ``think.call`` and mounted as ``sol call entities ...``.
Every verb reaches the journal only over HTTP via the Convey client; this
module imports no journal/domain function and performs no filesystem I/O.
"""

import json
import os
import re

import typer

from solstone.convey.reasons import (
    EDGE_INDEX_UNAVAILABLE,
    ENTITY_ALIAS_CONFLICT,
    ENTITY_ALREADY_EXISTS,
    ENTITY_BLOCKED,
    ENTITY_BUSY,
    ENTITY_NOT_FOUND,
    ENTITY_OPERATION_FAILED,
    INVALID_ENTITY_TYPE,
    INVALID_REQUEST_VALUE,
)
from solstone.think.convey_client import ConveyClientError, get_client

app = typer.Typer(help="Entity management.")


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


def _params(**values: object) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, object] | None = None,
    json_body: dict[str, object] | None = None,
) -> object:
    return get_client().request(method, path, params=params, json=json_body)


def _handle_entity_error(
    err: ConveyClientError,
    *,
    entity: str | None = None,
    type_: str | None = None,
) -> None:
    detail = err.detail or ""
    if err.reason_code == ENTITY_BUSY.code:
        _exit_with(ENTITY_BUSY.message)
    if err.reason_code == INVALID_ENTITY_TYPE.code and type_ is not None:
        _exit_with(f"Error: Invalid entity type '{type_}'.")
    if err.reason_code == ENTITY_BLOCKED.code:
        name = detail or entity or "entity"
        _exit_with(f"Error: Entity '{name}' is blocked.")
    if err.reason_code == ENTITY_NOT_FOUND.code:
        name = detail or entity or "entity"
        _exit_with(f"Error: Entity '{name}' not found.")
    if err.reason_code == EDGE_INDEX_UNAVAILABLE.code:
        _exit_with(EDGE_INDEX_UNAVAILABLE.message)
    if err.reason_code == ENTITY_ALIAS_CONFLICT.code and detail:
        _exit_with(f"Error: {detail}")
    if err.reason_code in {
        INVALID_REQUEST_VALUE.code,
        ENTITY_OPERATION_FAILED.code,
        ENTITY_ALREADY_EXISTS.code,
    }:
        if detail:
            _exit_with(f"Error: {detail}")

    typer.echo(err.error, err=True)
    raise typer.Exit(1)


def _plural(value: int, singular: str, plural: str | None = None) -> str:
    noun = singular if value == 1 else plural or f"{singular}s"
    return f"{value} {noun}"


def _normalize_kinds(values: list[str] | None) -> list[str] | None:
    kinds: list[str] = []
    for raw in values or []:
        for item in raw.split(","):
            kind = item.strip()
            if kind:
                kinds.append(kind)
    return kinds or None


def _edge_filter_params(
    *,
    kinds: list[str] | None,
    facet: str | None,
    day_from: str | None,
    day_to: str | None,
) -> dict[str, object]:
    return _params(
        kinds=_normalize_kinds(kinds),
        facet=facet,
        day_from=day_from,
        day_to=day_to,
    )


def _display_entity(entity_id: object, name: object = None) -> str:
    entity_id_text = str(entity_id or "")
    name_text = str(name or "")
    if name_text and name_text != entity_id_text:
        return f"{name_text} ({entity_id_text})"
    return entity_id_text


def _candidate_display(candidate: dict) -> str:
    return _display_entity(candidate.get("id"), candidate.get("name"))


def _render_edge_resolution_error(body: dict, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(body, indent=2, ensure_ascii=False))
        raise typer.Exit(1)

    query = str(body.get("query") or "entity")
    candidates = body.get("candidates") or []
    if candidates:
        labels = ", ".join(
            _candidate_display(candidate) for candidate in candidates[:3]
        )
        _exit_with(f"Error: Entity '{query}' not found. Did you mean: {labels}")
    _exit_with(f"Error: Entity '{query}' not found.")


def _edge_body_or_exit(
    path: str,
    *,
    params: dict[str, object],
    json_output: bool,
) -> dict:
    try:
        body = _request("GET", path, params=params)
    except ConveyClientError as err:
        _handle_entity_error(err)
    if not isinstance(body, dict):
        _exit_with("I couldn't read the journal response.")
    if body.get("resolved") is None and "query" in body and "candidates" in body:
        _render_edge_resolution_error(body, json_output=json_output)
    return body


def _echo_json(body: dict) -> None:
    typer.echo(json.dumps(body, indent=2, ensure_ascii=False))


def _format_kinds(kinds: object) -> str:
    if not isinstance(kinds, dict):
        return ""
    parts = []
    for kind in sorted(kinds):
        info = kinds.get(kind)
        count = info.get("count") if isinstance(info, dict) else None
        if count:
            parts.append(f"{kind}:{count}")
    return ", ".join(parts)


def _format_seen(item: dict) -> str:
    first_seen = item.get("first_seen")
    last_seen = item.get("last_seen")
    if first_seen and last_seen and first_seen != last_seen:
        return f"seen={first_seen}..{last_seen}"
    if first_seen or last_seen:
        return f"seen={first_seen or last_seen}"
    return ""


def _format_directed(item: dict) -> str:
    directed = item.get("directed")
    if not isinstance(directed, dict):
        return ""
    parts = []
    out_count = int(directed.get("out") or 0)
    in_count = int(directed.get("in") or 0)
    if out_count:
        parts.append(f"out:{out_count}")
    if in_count:
        parts.append(f"in:{in_count}")
    return f"directed={', '.join(parts)}" if parts else ""


def _format_evidence(row: dict, *, include_source: bool = False) -> str:
    parts = [str(row.get("day") or "unknown-day"), str(row.get("kind") or "unknown")]
    label = str(row.get("label") or "")
    anchor = str(row.get("anchor") or "")
    text = " ".join(parts)
    if label:
        text += f" - {label}"
    if anchor:
        text += f" ({anchor})"
    if include_source:
        source = str(row.get("source") or "")
        path = str(row.get("path") or "")
        source_parts = []
        if source:
            source_parts.append(f"[{source}]")
        if path:
            source_parts.append(path)
        if source_parts:
            text += " " + " ".join(source_parts)
    return text


def _render_neighbor(index: int, neighbor: dict) -> None:
    parts = [
        _display_entity(neighbor.get("entity_id"), neighbor.get("name")),
        f"score={float(neighbor.get('score') or 0):.2f}",
        f"count={int(neighbor.get('count') or 0)}",
    ]
    kinds = _format_kinds(neighbor.get("kinds"))
    if kinds:
        parts.append(f"kinds={kinds}")
    seen = _format_seen(neighbor)
    if seen:
        parts.append(seen)
    directed = _format_directed(neighbor)
    if directed:
        parts.append(directed)
    typer.echo(f"  {index}. {' '.join(parts)}")
    for row in neighbor.get("evidence") or []:
        if isinstance(row, dict):
            typer.echo(f"     - {_format_evidence(row)}")


def _render_network(body: dict) -> None:
    neighbors = body.get("neighbors") if isinstance(body.get("neighbors"), list) else []
    total = int(body.get("total_neighbors") or 0)
    entity_id = str(body.get("entity_id") or "")
    if total == 0:
        typer.echo(f"No recorded connections for {entity_id}.")
        return

    label = _plural(total, "recorded connection")
    if len(neighbors) < total:
        typer.echo(f"{label} for {entity_id} (showing {len(neighbors)}):")
    else:
        typer.echo(f"{label} for {entity_id}:")
    for index, neighbor in enumerate(neighbors, 1):
        if isinstance(neighbor, dict):
            _render_neighbor(index, neighbor)


def _render_history(body: dict) -> None:
    evidence = body.get("evidence") if isinstance(body.get("evidence"), list) else []
    total = int(body.get("total") or 0)
    entity_id = str(body.get("entity_id") or "")
    peer = _display_entity(body.get("peer_id"), body.get("peer_name"))
    if total == 0:
        typer.echo(f"No recorded connection history between {entity_id} and {peer}.")
        return

    label = _plural(total, "evidence row")
    offset = int(body.get("offset") or 0)
    suffix = ""
    if offset or len(evidence) < total:
        if evidence:
            suffix = f" (showing {offset + 1}-{offset + len(evidence)})"
        else:
            suffix = " (showing 0)"
    typer.echo(f"{label} for {entity_id} <-> {peer}{suffix}:")
    for row in evidence:
        if isinstance(row, dict):
            typer.echo(f"  - {_format_evidence(row, include_source=True)}")


def _render_overview(body: dict) -> None:
    entities = body.get("entities") if isinstance(body.get("entities"), list) else []
    totals = body.get("totals") if isinstance(body.get("totals"), dict) else {}
    total_edges = int(totals.get("edges") or 0)
    total_entities = int(totals.get("entities") or 0)
    if total_edges == 0 or total_entities == 0:
        typer.echo("No recorded connections in the edge index.")
        return

    edge_label = _plural(total_edges, "edge")
    entity_label = _plural(total_entities, "entity", "entities")
    suffix = f" (showing {len(entities)})" if len(entities) < total_entities else ""
    typer.echo(f"Network overview: {edge_label} across {entity_label}{suffix}:")
    kinds = _format_kinds(body.get("kinds"))
    if kinds:
        typer.echo(f"Kinds: {kinds}")
    for index, entity in enumerate(entities, 1):
        if isinstance(entity, dict):
            parts = [
                _display_entity(entity.get("entity_id"), entity.get("name")),
                f"score={float(entity.get('score') or 0):.2f}",
                f"count={int(entity.get('count') or 0)}",
            ]
            entity_kinds = _format_kinds(entity.get("kinds"))
            if entity_kinds:
                parts.append(f"kinds={entity_kinds}")
            seen = _format_seen(entity)
            if seen:
                parts.append(seen)
            typer.echo(f"  {index}. {' '.join(parts)}")


def _render_resolve_error(facet: str, entity: str, body: dict) -> None:
    if body.get("blocked"):
        blocked_name = body.get("blocked_name") or entity
        _exit_with(f"Error: Entity '{blocked_name}' is blocked.")
    candidates = body.get("candidates") or []
    if candidates:
        names = ", ".join(str(c.get("name", "")) for c in candidates[:3])
        _exit_with(f"Error: Entity '{entity}' not found. Did you mean: {names}")
    _exit_with(f"Error: Entity '{entity}' not found in facet '{facet}'.")


def _resolved_from_body_or_exit(facet: str, entity: str, body: dict) -> dict:
    resolved = body.get("resolved")
    if isinstance(resolved, dict):
        return resolved
    _render_resolve_error(facet, entity, body)
    raise typer.Exit(1)


def _resolve_entity_or_exit(facet: str, entity: str) -> dict:
    try:
        body = _request(
            "GET",
            f"/app/entities/api/{facet}/resolve",
            params={"name": entity},
        )
    except ConveyClientError as err:
        _handle_entity_error(err, entity=entity)
    if not isinstance(body, dict):
        _exit_with("I couldn't read the journal response.")
    return _resolved_from_body_or_exit(facet, entity, body)


def _echo_merge_candidate_error(result: dict) -> None:
    typer.echo(f"Error: {result.get('error', 'operation failed')}", err=True)
    raise typer.Exit(1)


def _echo_merge_preview(fields: dict) -> None:
    typer.echo("Merge preview:")
    akas = fields["akas_added"]
    if akas:
        typer.echo(f"  aliases added: {', '.join(akas)}")
    else:
        typer.echo("  aliases added: none")
    typer.echo(f"  emails added: {fields['emails_added_count']}")
    typer.echo(
        "  facet links: "
        f"{fields['facet_moved_count']} moved, "
        f"{fields['facet_merged_count']} merged"
    )
    typer.echo(f"  observations moved: {fields['observations_appended']}")
    typer.echo(
        "  speaker labels updated: "
        f"{fields['labels_rewritten']} labels, "
        f"{fields['corrections_rewritten']} corrections"
    )
    typer.echo(
        "  voice samples moved: "
        f"{fields['voiceprints_added']} added, "
        f"{fields['voiceprints_target_total']} total"
    )
    errors = fields["segment_errors"]
    if errors:
        typer.echo(f"  segment update errors: {len(errors)}")


@app.command("list")
def list_entities(
    facet: str | None = typer.Argument(None, help="Facet name (or set SOL_FACET)."),
    facet_opt: str | None = typer.Option(
        None, "--facet", "-f", help="Facet name (or set SOL_FACET)."
    ),
    day: str | None = typer.Option(
        None, "--day", "-d", help="Day (YYYYMMDD) for detected entities."
    ),
) -> None:
    """List entities for a facet."""
    facet = _resolve_sol_facet(facet or facet_opt)
    try:
        if day is None:
            body = _request("GET", f"/app/entities/api/{facet}")
            entities = body.get("attached", []) if isinstance(body, dict) else []
        else:
            body = _request(
                "GET",
                f"/app/entities/api/{facet}/detected",
                params={"day": day},
            )
            entities = body.get("items", []) if isinstance(body, dict) else []
    except ConveyClientError as err:
        _handle_entity_error(err)

    if not entities:
        typer.echo("No entities found.")
        return

    label = f"detected for {day}" if day else "attached"
    typer.echo(f"{len(entities)} {label} entities:")
    for entity in entities:
        typer.echo(
            "  - "
            f"{entity.get('name')} ({entity.get('type')}): "
            f"{entity.get('description', '')}"
        )


@app.command("move")
def move_entity(
    entity: str = typer.Argument(help="Entity name or partial match."),
    from_facet: str = typer.Option(..., "--from", help="Source facet."),
    to_facet: str = typer.Option(..., "--to", help="Destination facet."),
    merge: bool = typer.Option(
        False,
        "--merge",
        help="Merge if entity already exists in destination.",
    ),
    consent: bool = typer.Option(
        False,
        "--consent",
        help="Assert that explicit user approval was obtained before calling this command (agent audit trail).",
    ),
) -> None:
    """Move an entity from one facet to another."""
    try:
        source_body = _request(
            "GET",
            f"/app/entities/api/{from_facet}/resolve",
            params={"name": entity},
        )
    except ConveyClientError as err:
        _handle_entity_error(err, entity=entity)
    if not isinstance(source_body, dict):
        _exit_with("I couldn't read the journal response.")

    if not source_body.get("facet_exists"):
        _exit_with(f"Error: Facet '{from_facet}' (--from) does not exist.")

    try:
        target_body = _request(
            "GET",
            f"/app/entities/api/{to_facet}/resolve",
            params={"name": entity},
        )
    except ConveyClientError as err:
        _handle_entity_error(err, entity=entity)
    if not isinstance(target_body, dict):
        _exit_with("I couldn't read the journal response.")

    if not target_body.get("facet_exists"):
        _exit_with(f"Error: Facet '{to_facet}' (--to) does not exist.")

    resolved = _resolved_from_body_or_exit(from_facet, entity, source_body)
    entity_name = str(resolved.get("name", entity))

    try:
        _request(
            "POST",
            "/app/entities/api/move",
            json_body={
                "entity": entity_name,
                "from_facet": from_facet,
                "to_facet": to_facet,
                "merge": merge,
                "consent": consent,
            },
        )
    except ConveyClientError as err:
        _handle_entity_error(err, entity=entity_name)
    typer.echo(f"Moved entity '{entity_name}' from '{from_facet}' to '{to_facet}'.")


@app.command("detect")
def detect_entity(
    type_: str = typer.Argument(metavar="TYPE", help="Entity type."),
    entity: str = typer.Argument(help="Entity name or identifier."),
    description: str = typer.Argument(help="Description."),
    facet: str | None = typer.Option(
        None, "--facet", "-f", help="Facet name (or set SOL_FACET)."
    ),
    day: str | None = typer.Option(
        None, "--day", "-d", help="Day (YYYYMMDD, or set SOL_DAY)."
    ),
) -> None:
    """Record a detected entity for a day in a facet."""
    facet = _resolve_sol_facet(facet)
    day = _resolve_sol_day(day)
    try:
        body = _request(
            "POST",
            f"/app/entities/api/{facet}/detected",
            json_body={
                "day": day,
                "type": type_,
                "entity": entity,
                "description": description,
            },
        )
    except ConveyClientError as err:
        _handle_entity_error(err, entity=entity, type_=type_)
    name = body.get("name", entity) if isinstance(body, dict) else entity
    typer.echo(f"Entity '{name}' detected for {day}.")


@app.command("attach")
def attach_entity(
    type_: str = typer.Argument(metavar="TYPE", help="Entity type."),
    entity: str = typer.Argument(help="Entity name."),
    description: str = typer.Argument(help="Description."),
    facet: str | None = typer.Option(
        None, "--facet", "-f", help="Facet name (or set SOL_FACET)."
    ),
) -> None:
    """Attach an entity permanently to a facet."""
    facet = _resolve_sol_facet(facet)
    try:
        _request(
            "POST",
            f"/app/entities/api/{facet}/attach",
            json_body={
                "type": type_,
                "name": entity,
                "description": description,
            },
        )
    except ConveyClientError as err:
        if err.reason_code == ENTITY_ALREADY_EXISTS.code:
            typer.echo(f"Entity '{entity}' already attached.")
            return
        _handle_entity_error(err, entity=entity, type_=type_)
    typer.echo(f"Entity '{entity}' attached.")


@app.command("update")
def update_entity(
    entity: str = typer.Argument(help="Entity name or identifier."),
    description: str = typer.Argument(help="New description."),
    facet: str | None = typer.Option(
        None, "--facet", "-f", help="Facet name (or set SOL_FACET)."
    ),
    day: str | None = typer.Option(
        None, "--day", "-d", help="Day for detected entities."
    ),
) -> None:
    """Update an entity description."""
    facet = _resolve_sol_facet(facet)
    if day is None:
        resolved = _resolve_entity_or_exit(facet, entity)
        resolved_name = str(resolved.get("name", entity))
        entity_id = str(resolved.get("id") or "")
        try:
            _request(
                "POST",
                f"/app/entities/api/{facet}/update-description",
                json_body={
                    "entity_id": entity_id,
                    "description": description,
                    "entity": entity,
                    "name": resolved_name,
                },
            )
        except ConveyClientError as err:
            _handle_entity_error(err, entity=resolved_name)
        typer.echo(f"Entity '{resolved_name}' updated.")
        return

    try:
        _request(
            "POST",
            f"/app/entities/api/{facet}/update-detected",
            json_body={"day": day, "entity": entity, "description": description},
        )
    except ConveyClientError as err:
        _handle_entity_error(err, entity=entity)
    typer.echo(f"Entity '{entity}' updated for {day}.")


@app.command("aka")
def add_aka(
    entity: str = typer.Argument(help="Entity name or identifier."),
    aka_value: str = typer.Argument(metavar="AKA", help="Alias to add."),
    facet: str | None = typer.Option(
        None, "--facet", "-f", help="Facet name (or set SOL_FACET)."
    ),
) -> None:
    """Add an alias to an attached entity."""
    facet = _resolve_sol_facet(facet)
    resolved = _resolve_entity_or_exit(facet, entity)
    resolved_name = str(resolved.get("name", ""))

    base_name = re.sub(r"\s*\([^)]+\)", "", resolved_name).strip()
    first_word = base_name.split()[0] if base_name else None
    if first_word and aka_value.lower() == first_word.lower():
        typer.echo(
            f"Alias '{aka_value}' is the first word of '{resolved_name}' (skipped)."
        )
        return

    aka_list = resolved.get("aka", [])
    if not isinstance(aka_list, list):
        aka_list = []

    if aka_value in aka_list:
        typer.echo(f"Alias '{aka_value}' already exists for '{resolved_name}'.")
        return

    entity_id = str(resolved.get("id") or "")
    try:
        _request(
            "POST",
            f"/app/entities/api/{facet}/aka",
            json_body={
                "entity_id": entity_id,
                "aka": aka_value,
                "exclude_name": resolved_name,
                "entity": entity,
            },
        )
    except ConveyClientError as err:
        _handle_entity_error(err, entity=resolved_name)
    typer.echo(f"Added alias '{aka_value}' to '{resolved_name}'.")


@app.command("record-merge-candidate")
def record_merge_candidate(
    source: str = typer.Argument(
        help="Variant name (folds INTO the canonical target)."
    ),
    target: str = typer.Argument(help="Canonical name to keep (merge target)."),
    facet: str | None = typer.Option(
        None, "--facet", "-f", help="Facet (or set SOL_FACET)."
    ),
    day: str | None = typer.Option(
        None, "--day", "-d", help="Review day YYYYMMDD (or set SOL_DAY)."
    ),
    evidence: str = typer.Option(
        ..., "--evidence", help="Short human summary of the evidence band."
    ),
    basis: str = typer.Option(
        "name-variant", "--basis", help="Variant relationship basis."
    ),
    detections: int | None = typer.Option(
        None, "--detections", help="Combined detection count (strength signal)."
    ),
    needs: int | None = typer.Option(
        None, "--needs", help="Detections still needed to cross promotion threshold."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output the record as JSON."
    ),
) -> None:
    """Record a proposed entity merge (source variant -> canonical target)."""
    facet = _resolve_sol_facet(facet)
    day = _resolve_sol_day(day)
    try:
        body = _request(
            "POST",
            "/app/entities/api/record-merge-candidate",
            json_body={
                "facet": facet,
                "day": day,
                "source": source,
                "target": target,
                "evidence": evidence,
                "basis": basis,
                "detections": detections,
                "needs": needs,
            },
        )
    except ConveyClientError as err:
        _handle_entity_error(err)
    if not isinstance(body, dict):
        _exit_with("I couldn't read the journal response.")
    row = body["row"]
    if json_output:
        typer.echo(json.dumps(row, indent=2, ensure_ascii=False))
        return
    if body.get("created"):
        typer.echo(f"merge candidate recorded: {source} -> {target}")
        return
    typer.echo(
        f"merge candidate updated: {source} -> {target} (status: {row.get('status')})"
    )


@app.command("merge-candidates")
def list_merge_candidates(
    facet: str | None = typer.Option(None, "--facet", "-f", help="Filter by facet."),
    status: str | None = typer.Option(None, "--status", help="Filter by status."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """List recorded entity merge candidates."""
    try:
        body = _request(
            "GET",
            "/app/entities/api/merge-candidates",
            params=_params(facet=facet, status=status),
        )
    except ConveyClientError as err:
        _handle_entity_error(err)
    rows = body.get("items", []) if isinstance(body, dict) else []

    if json_output:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return

    if not rows:
        typer.echo("No merge candidates found.")
        return

    for row in rows:
        evidence_data = row.get("evidence", {})
        typer.echo(
            f"{row.get('source', '')} -> {row.get('target', '')}  "
            f"[{row.get('status', '')}]  facet={row.get('facet', '')}  "
            f"detections={evidence_data.get('detection_count')}  "
            f"needs={evidence_data.get('needs')}  "
            f"last={row.get('last_surfaced', '')}"
        )


@app.command("accept-merge-candidate")
def accept_merge_candidate(
    source_slug: str = typer.Argument(help="Source entity slug to merge from."),
    target_slug: str = typer.Argument(help="Target entity slug to merge into."),
    facet: str | None = typer.Option(
        None, "--facet", "-f", help="Facet name (or set SOL_FACET)."
    ),
    commit: bool = typer.Option(False, "--commit/--no-commit"),
) -> None:
    """Preview or accept one recorded entity merge candidate."""
    facet = _resolve_sol_facet(facet)
    try:
        result = _request(
            "POST",
            "/app/entities/api/accept-merge-candidate",
            json_body={
                "facet": facet,
                "source_slug": source_slug,
                "target_slug": target_slug,
                "commit": commit,
            },
        )
    except ConveyClientError as err:
        _handle_entity_error(err)
    if not isinstance(result, dict):
        _exit_with("I couldn't read the journal response.")
    status = result.get("status")
    if status == "error":
        _echo_merge_candidate_error(result)
    if status == "preview":
        _echo_merge_preview(result["fields"])
        return
    if status == "accepted":
        typer.echo(f"Accepted merge candidate: {source_slug} -> {target_slug}")
        return
    if status == "already_accepted":
        typer.echo(f"Merge candidate already accepted: {source_slug} -> {target_slug}")
        return
    typer.echo(f"accept result for {source_slug} -> {target_slug}: {status}")


@app.command("dismiss-merge-candidate")
def dismiss_merge_candidate(
    source_slug: str = typer.Argument(help="Source entity slug."),
    target_slug: str = typer.Argument(help="Target entity slug."),
    facet: str | None = typer.Option(
        None, "--facet", "-f", help="Facet name (or set SOL_FACET)."
    ),
) -> None:
    """Dismiss one recorded entity merge candidate."""
    facet = _resolve_sol_facet(facet)
    try:
        result = _request(
            "POST",
            "/app/entities/api/dismiss-merge-candidate",
            json_body={
                "facet": facet,
                "source_slug": source_slug,
                "target_slug": target_slug,
            },
        )
    except ConveyClientError as err:
        _handle_entity_error(err)
    if not isinstance(result, dict):
        _exit_with("I couldn't read the journal response.")
    status = result.get("status")
    if status == "error":
        _echo_merge_candidate_error(result)
    if status == "dismissed":
        typer.echo(f"Dismissed merge candidate: {source_slug} -> {target_slug}")
        return
    if status == "already_dismissed":
        typer.echo(f"Merge candidate already dismissed: {source_slug} -> {target_slug}")
        return
    typer.echo(f"dismiss result for {source_slug} -> {target_slug}: {status}")


@app.command("merge")
def merge(
    source_slug: str = typer.Argument(help="Source entity slug to merge from."),
    target_slug: str = typer.Argument(help="Target entity slug to merge into."),
    commit: bool = typer.Option(False, "--commit/--no-commit"),
    keep_source_as_aka: bool = typer.Option(
        True,
        "--keep-source-as-aka/--no-keep-source-as-aka",
    ),
) -> None:
    """Plan or commit a journal-entity merge."""
    try:
        body = _request(
            "POST",
            "/app/entities/api/merge",
            json_body={
                "source_slug": source_slug,
                "target_slug": target_slug,
                "commit": commit,
                "keep_source_as_aka": keep_source_as_aka,
            },
        )
    except ConveyClientError as err:
        _handle_entity_error(err)
    if not isinstance(body, dict):
        _exit_with("I couldn't read the journal response.")
    output = json.dumps(body, indent=2, default=str)
    if "error" in body:
        typer.echo(output, err=True)
        raise typer.Exit(1)
    typer.echo(output)


@app.command("network")
def entity_network(
    entity: str = typer.Argument(help="Entity name or identifier."),
    kinds: list[str] = typer.Option(
        [],
        "--kinds",
        help="Edge kind filter. Repeat or comma-separate for multiple kinds.",
    ),
    facet: str | None = typer.Option(None, "--facet", "-f", help="Facet filter."),
    day_from: str | None = typer.Option(None, "--day-from", help="Start day YYYYMMDD."),
    day_to: str | None = typer.Option(None, "--day-to", help="End day YYYYMMDD."),
    limit: int = typer.Option(25, "--limit", "-n", help="Max neighbors."),
    evidence_limit: int = typer.Option(
        5,
        "--evidence-limit",
        help="Evidence rows per neighbor.",
    ),
    include_principal: bool = typer.Option(
        False,
        "--include-principal",
        help="Include the principal entity in neighbors.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show one-hop recorded connections for an entity."""
    params = _edge_filter_params(
        kinds=kinds,
        facet=facet,
        day_from=day_from,
        day_to=day_to,
    )
    params.update(
        {
            "entity": entity,
            "limit": limit,
            "evidence_limit": evidence_limit,
            "include_principal": include_principal,
        }
    )
    body = _edge_body_or_exit(
        "/app/entities/api/network",
        params=params,
        json_output=json_output,
    )
    if json_output:
        _echo_json(body)
        return
    _render_network(body)


@app.command("history")
def entity_history(
    entity: str = typer.Argument(help="Entity name or identifier."),
    peer: str | None = typer.Argument(
        None,
        help="Peer entity name or identifier. Defaults to the principal entity.",
    ),
    kinds: list[str] = typer.Option(
        [],
        "--kinds",
        help="Edge kind filter. Repeat or comma-separate for multiple kinds.",
    ),
    facet: str | None = typer.Option(None, "--facet", "-f", help="Facet filter."),
    day_from: str | None = typer.Option(None, "--day-from", help="Start day YYYYMMDD."),
    day_to: str | None = typer.Option(None, "--day-to", help="End day YYYYMMDD."),
    limit: int = typer.Option(50, "--limit", "-n", help="Max evidence rows."),
    offset: int = typer.Option(0, "--offset", help="Evidence row offset."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show recorded connection evidence for an entity pair."""
    params = _edge_filter_params(
        kinds=kinds,
        facet=facet,
        day_from=day_from,
        day_to=day_to,
    )
    params.update(
        _params(
            entity=entity,
            peer=peer,
            limit=limit,
            offset=offset,
        )
    )
    body = _edge_body_or_exit(
        "/app/entities/api/history",
        params=params,
        json_output=json_output,
    )
    if json_output:
        _echo_json(body)
        return
    _render_history(body)


@app.command("overview")
def entity_overview(
    kinds: list[str] = typer.Option(
        [],
        "--kinds",
        help="Edge kind filter. Repeat or comma-separate for multiple kinds.",
    ),
    facet: str | None = typer.Option(None, "--facet", "-f", help="Facet filter."),
    day_from: str | None = typer.Option(None, "--day-from", help="Start day YYYYMMDD."),
    day_to: str | None = typer.Option(None, "--day-to", help="End day YYYYMMDD."),
    limit: int = typer.Option(25, "--limit", "-n", help="Max entities."),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Show a global recorded-connections overview."""
    params = _edge_filter_params(
        kinds=kinds,
        facet=facet,
        day_from=day_from,
        day_to=day_to,
    )
    params["limit"] = limit
    body = _edge_body_or_exit(
        "/app/entities/api/overview",
        params=params,
        json_output=json_output,
    )
    if json_output:
        _echo_json(body)
        return
    _render_overview(body)


@app.command("observations")
def list_observations(
    entity: str = typer.Argument(help="Entity name or identifier."),
    facet: str | None = typer.Option(
        None, "--facet", "-f", help="Facet name (or set SOL_FACET)."
    ),
) -> None:
    """List observations for an attached entity."""
    facet = _resolve_sol_facet(facet)
    resolved = _resolve_entity_or_exit(facet, entity)
    resolved_name = str(resolved.get("name", ""))
    try:
        body = _request(
            "GET",
            f"/app/entities/api/{facet}/observations",
            params={"name": resolved_name},
        )
    except ConveyClientError as err:
        _handle_entity_error(err, entity=resolved_name)
    obs = body.get("items", []) if isinstance(body, dict) else []

    if not obs:
        typer.echo(f"No observations for '{resolved_name}'.")
        return

    typer.echo(f"{len(obs)} observations for '{resolved_name}':")
    for i, observation in enumerate(obs, 1):
        typer.echo(f"  {i}. {observation.get('content', '')}")


@app.command("observe")
def observe_entity(
    entity: str = typer.Argument(help="Entity name or identifier."),
    content: str = typer.Argument(help="Observation content."),
    facet: str | None = typer.Option(
        None, "--facet", "-f", help="Facet name (or set SOL_FACET)."
    ),
    source_day: str | None = typer.Option(None, "--source-day", help="Day (YYYYMMDD)."),
) -> None:
    """Add an observation to an attached entity."""
    facet = _resolve_sol_facet(facet)
    resolved = _resolve_entity_or_exit(facet, entity)
    resolved_name = str(resolved.get("name", ""))
    try:
        _request(
            "POST",
            f"/app/entities/api/{facet}/observe",
            json_body={
                "name": resolved_name,
                "content": content,
                "source_day": source_day,
                "entity": entity,
            },
        )
    except ConveyClientError as err:
        _handle_entity_error(err, entity=resolved_name)
    typer.echo(f"Observation added to '{resolved_name}'.")


@app.command("search")
def entity_search(
    query_pos: str | None = typer.Argument(None, help="Text search."),
    query: str | None = typer.Option(None, "--query", "-q", help="Text search."),
    type_: str | None = typer.Option(None, "--type", "-t", help="Entity type."),
    facet: str | None = typer.Option(None, "--facet", "-f", help="Filter by facet."),
    since: str | None = typer.Option(None, "--since", help="Detected since YYYYMMDD."),
    limit: int = typer.Option(20, "--limit", "-n", help="Max results."),
) -> None:
    """Search entities by text, type, facet, or activity."""
    query = query_pos or query
    try:
        body = _request(
            "GET",
            "/app/entities/api/search",
            params=_params(
                query=query,
                type=type_,
                facet=facet,
                since=since,
                limit=limit,
            ),
        )
    except ConveyClientError as err:
        _handle_entity_error(err)
    results = body.get("items", []) if isinstance(body, dict) else []
    if not results:
        typer.echo("No entities found.")
        return
    typer.echo(f"{len(results)} entities:")
    for entity in results:
        facets = ", ".join(entity.get("facets", []))
        typer.echo(f"  - {entity['name']} ({entity['type']}): {entity['description']}")
        if facets:
            typer.echo(f"    facets: {facets}")
