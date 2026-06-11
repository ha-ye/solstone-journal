# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""CLI interface for speaker voiceprint management.

Auto-discovered by ``think.call`` and mounted as ``sol call speakers ...``.
Every verb reaches the journal only over HTTP via the Convey client; this
module imports no journal/domain function and performs no filesystem I/O.

Speaker writer commands preview by default; pass ``--commit`` to persist.
For ``attribute-segment``, ``--save`` / ``--accumulate`` only take effect
when ``--commit`` is also passed.

Commands:
    sol call speakers status [section]
    sol call speakers bootstrap [--commit] [--json]
    sol call speakers resolve-names [--commit] [--json]
    sol call speakers attribute-segment <day> <stream> <segment> [--commit] [--json]
    sol call speakers backfill [--commit] [--json]
    sol call speakers backfill-last-seen [--commit] [--json]
    sol call speakers wipe [--commit] [--json]
    sol call speakers discover [--json]
    sol call speakers identify <cluster-id> <name> [--entity-id ID]
    sol call speakers merge-names <alias> <canonical>
    sol call speakers link-import <name> --entity-id <ID>
    sol call speakers seed-from-imports [--commit] [--json]
    sol call speakers suggest [--limit N] [--json]
    sol call speakers detect [--json]
    sol call speakers confirm-owner [--backfill] [--json]
    sol call speakers reject-owner
    sol call speakers owner-ready
"""

import json
import time
from typing import Any

import typer

from solstone.convey.reasons import (
    SPEAKER_COMMAND_FAILED,
    SPEAKER_OWNER_CENTROID_REQUIRED,
)
from solstone.think.convey_client import ConveyClientError, convey_cli, get_client

app = typer.Typer(
    name="speakers",
    help="Speaker voiceprint management.",
    no_args_is_help=True,
)


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    return get_client().request(method, path, params=params, json=json_body)


def _exit_owner_centroid_required(err: ConveyClientError) -> None:
    typer.echo(f"Error: {err.detail}", err=True)
    raise typer.Exit(1) from err


def _exit_speaker_command_failed(err: ConveyClientError) -> None:
    typer.echo(err.detail, err=True)
    raise typer.Exit(1) from err


@app.command("status")
@convey_cli
def status(
    section: str | None = typer.Argument(
        None,
        help=(
            "Section to show (embeddings, owner, speakers, clusters, imports, "
            "attribution). Omit for all."
        ),
    ),
) -> None:
    """Show speaker subsystem status as JSON."""
    body = _request("GET", "/app/speakers/api/status")
    valid = ["embeddings", "owner", "speakers", "clusters", "imports", "attribution"]
    if section is None:
        result = body
    elif section in valid:
        result = body[section]
    else:
        result = {"error": f"Unknown section '{section}'. Valid: {', '.join(valid)}"}
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command("bootstrap")
@convey_cli
def bootstrap(
    commit: bool = typer.Option(
        False,
        "--commit",
        help="Persist results. Without this flag the command only reports what would happen.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output full result as JSON."
    ),
) -> None:
    """Bootstrap voiceprints from single-speaker segments.

    Scans the full journal for segments where speakers.json lists exactly
    one speaker. In those segments, all non-owner embeddings belong to that
    speaker. Saves them as voiceprints using the owner centroid for
    owner subtraction.
    """
    if not commit and not json_output:
        typer.echo("REPORT ONLY — pass --commit to persist.\n")

    if not json_output:
        typer.echo("Bootstrapping voiceprints from single-speaker segments...")
    try:
        stats = _request(
            "POST", "/app/speakers/api/bootstrap", json_body={"commit": commit}
        )
    except ConveyClientError as err:
        if err.reason_code == SPEAKER_OWNER_CENTROID_REQUIRED.code:
            _exit_owner_centroid_required(err)
        raise

    if json_output:
        typer.echo(json.dumps(stats, indent=2, default=str))
        return

    typer.echo(f"\nSegments scanned: {stats['segments_scanned']}")
    typer.echo(f"Single-speaker segments: {stats['single_speaker_segments']}")
    typer.echo(f"Unique speakers: {len(stats['speakers_found'])}")
    typer.echo(f"Entities created: {stats['entities_created']}")
    typer.echo(f"Embeddings saved: {stats['embeddings_saved']}")
    typer.echo(f"Embeddings skipped (owner): {stats['embeddings_skipped_owner']}")
    typer.echo(
        f"Embeddings skipped (duplicate): {stats['embeddings_skipped_duplicate']}"
    )

    if stats["speakers_found"]:
        typer.echo("\nTop speakers by embedding count:")
        sorted_speakers = sorted(
            stats["speakers_found"].items(), key=lambda x: x[1], reverse=True
        )
        for name, count in sorted_speakers[:15]:
            typer.echo(f"  {name}: {count}")
        if len(sorted_speakers) > 15:
            typer.echo(f"  ... and {len(sorted_speakers) - 15} more")

    if stats["errors"]:
        typer.echo(f"\nErrors ({len(stats['errors'])}):", err=True)
        for err in stats["errors"]:
            typer.echo(f"  {err}", err=True)


@app.command("resolve-names")
@convey_cli
def resolve_names(
    commit: bool = typer.Option(
        False,
        "--commit",
        help="Persist results. Without this flag the command only reports what would happen.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output full result as JSON."
    ),
) -> None:
    """Resolve speaker name variants using voiceprint similarity.

    Compares voiceprint centroids between all entities. Pairs with cosine
    similarity > 0.90 are flagged as the same person. Unambiguous variants
    (short name is first word of full name) are auto-merged by adding the
    short name as an aka on the canonical entity.
    """
    if not commit and not json_output:
        typer.echo("REPORT ONLY — pass --commit to persist.\n")

    if not json_output:
        typer.echo("Resolving speaker name variants...")
    stats = _request(
        "POST", "/app/speakers/api/resolve-names", json_body={"commit": commit}
    )

    if json_output:
        typer.echo(json.dumps(stats, indent=2, default=str))
        return

    typer.echo(f"\nEntities with voiceprints: {stats['entities_with_voiceprints']}")
    typer.echo(f"Pairs compared: {stats['pairs_compared']}")
    typer.echo(f"High-similarity pairs: {len(stats['matches_found'])}")

    if stats["auto_merged"]:
        typer.echo(f"\nAuto-merged ({len(stats['auto_merged'])}):")
        for merge in stats["auto_merged"]:
            typer.echo(
                f"  {merge['alias']} -> {merge['canonical']} ({merge['similarity']})"
            )

    if stats["ambiguous"]:
        typer.echo(f"\nAmbiguous ({len(stats['ambiguous'])}):")
        for amb in stats["ambiguous"]:
            candidates = ", ".join(
                f"{c['name']} ({c['similarity']})" for c in amb["candidates"]
            )
            typer.echo(f"  {amb['name']}: {candidates}")

    if stats["errors"]:
        typer.echo(f"\nErrors ({len(stats['errors'])}):", err=True)
        for err in stats["errors"]:
            typer.echo(f"  {err}", err=True)


@app.command("attribute-segment")
@convey_cli
def attribute_segment_cmd(
    day: str = typer.Argument(..., help="Day in YYYYMMDD format."),
    stream: str = typer.Argument(..., help="Stream name."),
    segment: str = typer.Argument(..., help="Segment key (HHMMSS_LEN)."),
    commit: bool = typer.Option(
        False, "--commit", help="Persist speaker labels and voiceprint accumulation."
    ),
    save: bool = typer.Option(
        True, "--save/--no-save", help="Write speaker_labels.json."
    ),
    accumulate: bool = typer.Option(
        True,
        "--accumulate/--no-accumulate",
        help="Run voiceprint accumulation.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output full result as JSON."
    ),
) -> None:
    """Run speaker attribution (Layers 1-3) on a single segment.

    Classifies each sentence using owner detection, structural heuristics,
    and acoustic voiceprint matching.  Optionally writes speaker_labels.json
    and accumulates high-confidence voiceprints.
    """
    error_detail = None
    try:
        wrap = _request(
            "POST",
            "/app/speakers/api/attribute-segment",
            json_body={
                "day": day,
                "stream": stream,
                "segment": segment,
                "commit": commit,
                "save": save,
                "accumulate": accumulate,
            },
        )
    except ConveyClientError as err:
        if err.reason_code == SPEAKER_OWNER_CENTROID_REQUIRED.code:
            wrap = None
            error_detail = err.detail
        else:
            raise

    if not commit and not json_output:
        typer.echo("REPORT ONLY — pass --commit to persist.\n")

    if error_detail is not None:
        typer.echo(f"Error: {error_detail}", err=True)
        raise typer.Exit(1)

    result = wrap["result"]
    labels = result.get("labels", [])
    unmatched = result.get("unmatched", [])
    source = result.get("source")

    if json_output:
        typer.echo(json.dumps(result, indent=2))
    else:
        resolved = sum(1 for lab in labels if lab["speaker"] is not None)
        typer.echo(f"Sentences: {len(labels)}")
        typer.echo(f"Resolved:  {resolved}")
        typer.echo(f"Unmatched: {len(unmatched)}")

        methods: dict[str, int] = {}
        for lab in labels:
            m = lab.get("method") or "unmatched"
            methods[m] = methods.get(m, 0) + 1
        typer.echo("\nBy method:")
        for method, count in sorted(methods.items()):
            typer.echo(f"  {method}: {count}")

    if commit and save and not json_output:
        typer.echo(f"\nWrote: {wrap['written_path']}")

    if commit and accumulate and source:
        saved = wrap.get("accumulated")
        if saved and not json_output:
            typer.echo("\nAccumulated voiceprints:")
            for eid, count in saved.items():
                typer.echo(f"  {eid}: {count} embeddings")


@app.command("backfill")
@convey_cli
def backfill(
    commit: bool = typer.Option(
        False,
        "--commit",
        help="Persist results. Without this flag the command only reports what would happen.",
    ),
    reattribute: bool = typer.Option(
        False,
        "--reattribute",
        help="Also re-attribute segments that already have speaker labels (preserves user corrections).",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output full result as JSON."
    ),
) -> None:
    """Run speaker attribution across all segments with embeddings.

    Processes segments oldest-first for progressive voiceprint building.
    Skips segments that already have speaker_labels.json (safe to re-run).
    """
    if not commit and not json_output:
        typer.echo("REPORT ONLY — pass --commit to persist.\n")

    if not json_output:
        typer.echo("Scanning journal for segments with embeddings...")

    start = time.monotonic()
    stats = _request(
        "POST",
        "/app/speakers/api/backfill",
        json_body={"commit": commit, "reattribute": reattribute},
    )
    elapsed = time.monotonic() - start

    if json_output:
        typer.echo(json.dumps(stats, indent=2, default=str))
        return

    typer.echo("\n")
    typer.echo(f"Total segments scanned:    {stats['total_segments']}")
    typer.echo(f"With embeddings:           {stats['total_eligible']}")
    typer.echo(f"Without embeddings:        {stats['skipped_no_embed']}")
    typer.echo(f"Already labeled (skipped): {stats['already_labeled']}")
    typer.echo(f"Processed this run:        {stats['processed']}")
    typer.echo(f"Elapsed:                   {elapsed:.1f}s")

    speakers = stats.get("speakers_seen", {})
    if speakers:
        typer.echo(f"\nSpeakers identified ({len(speakers)}):")
        sorted_speakers = sorted(speakers.items(), key=lambda x: x[1], reverse=True)
        for eid, count in sorted_speakers[:20]:
            typer.echo(f"  {eid}: {count} attributions")
        if len(sorted_speakers) > 20:
            typer.echo(f"  ... and {len(sorted_speakers) - 20} more")

    if stats["errors"]:
        typer.echo(f"\nErrors ({len(stats['errors'])}):", err=True)
        for err in stats["errors"][:10]:
            typer.echo(f"  {err}", err=True)
        if len(stats["errors"]) > 10:
            typer.echo(f"  ... and {len(stats['errors']) - 10} more", err=True)


@app.command("backfill-last-seen")
@convey_cli
def backfill_last_seen_cmd(
    commit: bool = typer.Option(
        False,
        "--commit",
        help="Persist results. Without this flag the command only reports what would happen.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output full result as JSON."
    ),
) -> None:
    """Backfill last_seen_ts on existing voiceprint metadata rows."""
    if not commit and not json_output:
        typer.echo("REPORT ONLY — pass --commit to persist.\n")

    stats = _request(
        "POST", "/app/speakers/api/backfill-last-seen", json_body={"commit": commit}
    )

    if json_output:
        typer.echo(json.dumps(stats, indent=2, default=str))
        return

    typer.echo(f"Speaker label files read: {stats['labels_read']}")
    typer.echo(f"Entities seen:            {stats['entities_seen']}")
    typer.echo(f"Voiceprint rows scanned:  {stats['rows_scanned']}")
    typer.echo(f"Rows pending:             {stats['rows_pending']}")
    typer.echo(f"Rows written:             {stats['rows_written']}")

    pending = stats.get("pending", {})
    if pending:
        typer.echo("\nPending by entity:")
        for entity_id, item in pending.items():
            typer.echo(f"  {entity_id}: {item['rows']}")

    if stats.get("errors"):
        typer.echo("\nErrors:", err=True)
        for error in stats["errors"]:
            typer.echo(f"  {error}", err=True)


@app.command()
@convey_cli
def wipe(
    commit: bool = typer.Option(
        False,
        "--commit",
        help="Actually delete files. Without this flag the command only reports what would happen.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output full result as JSON."
    ),
) -> None:
    """Remove all legacy speaker artifacts from the journal (DESTRUCTIVE).

    DESTRUCTIVE. Without --commit, prints a report of what would be
    removed. With --commit, permanently deletes segment-embedding NPZs,
    speaker labels/corrections, per-entity voiceprints, owner centroids,
    and the owner-candidate snapshot.
    """
    if not commit and not json_output:
        typer.echo("REPORT ONLY — pass --commit to persist.\n")

    report = _request("POST", "/app/speakers/api/wipe", json_body={"commit": commit})

    if json_output:
        typer.echo(json.dumps(report, indent=2, default=str))
        return

    typer.echo(
        f"segment_embeddings : {report['segment_embeddings']['count']} files "
        f"({report['segment_embeddings']['bytes']} B)"
    )
    typer.echo(
        f"speaker_labels     : {report['speaker_labels']['count']} files "
        f"({report['speaker_labels']['bytes']} B)"
    )
    typer.echo(
        f"speaker_corrections: {report['speaker_corrections']['count']} files "
        f"({report['speaker_corrections']['bytes']} B)"
    )
    typer.echo(
        f"entity_voiceprints : {report['entity_voiceprints']['count']} files "
        f"({report['entity_voiceprints']['bytes']} B)"
    )
    typer.echo(
        f"owner_centroids    : {report['owner_centroids']['count']} files "
        f"({report['owner_centroids']['bytes']} B)"
    )
    typer.echo(
        f"owner_candidate    : {report['owner_candidate']['count']} files "
        f"({report['owner_candidate']['bytes']} B)"
    )
    typer.echo(
        f"total              : {report['total_files']} files ({report['total_bytes']} B)"
    )


@app.command()
@convey_cli
def discover(
    json_output: bool = typer.Option(
        False, "--json", help="Output full result as JSON."
    ),
) -> None:
    """Discover recurring unknown speakers across segments."""
    result = _request("POST", "/app/speakers/api/discovery/scan")
    if json_output:
        typer.echo(json.dumps(result, indent=2, default=str))
        return
    clusters = result.get("clusters", [])

    if not clusters:
        typer.echo("No recurring unknown speakers found.")
        raise typer.Exit()

    typer.echo(f"Found {len(clusters)} unknown speaker cluster(s):\n")
    for cluster in clusters:
        typer.echo(
            f"  Cluster {cluster['cluster_id']}: "
            f"{cluster['size']} samples across {cluster['segment_count']} segments"
        )
        for sample in cluster.get("samples", []):
            text_preview = (sample.get("text") or "")[:60]
            typer.echo(
                f"    - {sample['day']}/{sample['stream']}/{sample['segment_key']} "
                f"sid={sample['sentence_id']}: {text_preview}"
            )
        typer.echo()


@app.command()
@convey_cli
def identify(
    cluster_id: int = typer.Argument(..., help="Cluster ID from discovery output."),
    name: str = typer.Argument(..., help="Speaker name to assign."),
    entity_id: str | None = typer.Option(
        None, "--entity-id", help="Link to existing entity ID instead of name matching."
    ),
) -> None:
    """Identify a discovered unknown speaker cluster."""
    try:
        result = _request(
            "POST",
            "/app/speakers/api/discovery/identify-cli",
            json_body={"cluster_id": cluster_id, "name": name, "entity_id": entity_id},
        )
    except ConveyClientError as err:
        if err.reason_code == SPEAKER_COMMAND_FAILED.code:
            _exit_speaker_command_failed(err)
        raise
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command("merge-names")
@convey_cli
def merge_names_cmd(
    alias: str = typer.Argument(..., help="Alias/variant speaker name to merge from."),
    canonical: str = typer.Argument(..., help="Canonical speaker name to merge into."),
) -> None:
    """Merge a speaker name variant into a canonical entity."""
    try:
        result = _request(
            "POST",
            "/app/speakers/api/merge-names",
            json_body={"alias": alias, "canonical": canonical},
        )
    except ConveyClientError as err:
        if err.reason_code == SPEAKER_COMMAND_FAILED.code:
            _exit_speaker_command_failed(err)
        raise
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command("link-import")
@convey_cli
def link_import_cmd(
    name: str = typer.Argument(..., help="Import participant name to link."),
    entity_id: str = typer.Option(..., "--entity-id", help="Entity ID to link to."),
) -> None:
    """Link an import participant name as an aka on an existing entity."""
    try:
        result = _request(
            "POST",
            "/app/speakers/api/link-import",
            json_body={"name": name, "entity_id": entity_id},
        )
    except ConveyClientError as err:
        if err.reason_code == SPEAKER_COMMAND_FAILED.code:
            _exit_speaker_command_failed(err)
        raise
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command("seed-from-imports")
@convey_cli
def seed_from_imports_cmd(
    commit: bool = typer.Option(
        False,
        "--commit",
        help="Persist results. Without this flag the command only reports what would happen.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Output full result as JSON."
    ),
) -> None:
    """Seed voiceprints from import segments with speaker-attributed transcripts.

    Scans import streams for segments with both conversation_transcript.jsonl
    (with speaker labels) and audio embeddings. Maps each embedding to a speaker
    via time-based alignment, matches speakers to existing entities, and saves
    embeddings as voiceprints with owner contamination guard.
    """
    if not commit and not json_output:
        typer.echo("REPORT ONLY — pass --commit to persist.\n")

    if not json_output:
        typer.echo("Seeding voiceprints from import segments...")
    try:
        stats = _request(
            "POST",
            "/app/speakers/api/seed-from-imports",
            json_body={"commit": commit},
        )
    except ConveyClientError as err:
        if err.reason_code == SPEAKER_OWNER_CENTROID_REQUIRED.code:
            _exit_owner_centroid_required(err)
        raise

    if json_output:
        typer.echo(json.dumps(stats, indent=2, default=str))
        return

    typer.echo(f"\nSegments scanned: {stats['segments_scanned']}")
    typer.echo(f"Segments with speakers: {stats['segments_with_speakers']}")
    typer.echo(f"Unique speakers: {len(stats['speakers_found'])}")
    typer.echo(f"Embeddings saved: {stats['embeddings_saved']}")
    typer.echo(f"Embeddings skipped (owner): {stats['embeddings_skipped_owner']}")
    typer.echo(
        f"Embeddings skipped (duplicate): {stats['embeddings_skipped_duplicate']}"
    )

    if stats["speakers_found"]:
        typer.echo("\nSpeakers by embedding count:")
        sorted_speakers = sorted(
            stats["speakers_found"].items(), key=lambda x: x[1], reverse=True
        )
        for name, count in sorted_speakers[:15]:
            typer.echo(f"  {name}: {count}")

    if stats["speakers_unmatched"]:
        typer.echo(f"\nUnmatched speakers ({len(stats['speakers_unmatched'])}):")
        for name in stats["speakers_unmatched"]:
            typer.echo(f"  {name}")

    if stats["errors"]:
        typer.echo(f"\nErrors ({len(stats['errors'])}):", err=True)
        for err in stats["errors"]:
            typer.echo(f"  {err}", err=True)


@app.command()
@convey_cli
def suggest(
    limit: int = typer.Option(
        5, "--limit", "-n", help="Maximum suggestions to return."
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON array."),
) -> None:
    """Suggest speaker curation opportunities."""
    body = _request("GET", "/app/speakers/api/suggest", params={"limit": limit})
    if json_output:
        typer.echo(json.dumps(body["items"], indent=2, default=str))
        return

    typer.echo(body["markdown"])


@app.command("detect")
@convey_cli
def detect_cmd(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Run owner voice candidate detection."""
    result = _request("POST", "/app/speakers/api/owner/detect")
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command("confirm-owner")
@convey_cli
def confirm_owner_cmd(
    backfill_after: bool = typer.Option(
        True,
        "--backfill/--no-backfill",
        help="Run attribution backfill after confirming.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON."),
) -> None:
    """Confirm the owner voice candidate and save the centroid.

    By default, automatically runs attribution backfill on all segments
    after saving the centroid.
    """
    try:
        result = _request("POST", "/app/speakers/api/owner/confirm-cli")
    except ConveyClientError as err:
        if err.reason_code == SPEAKER_COMMAND_FAILED.code:
            _exit_speaker_command_failed(err)
        raise

    if not json_output:
        typer.echo(
            f"Owner centroid confirmed (principal: {result['principal_id']}, "
            f"cluster_size: {result['cluster_size']})"
        )

    if backfill_after:
        if not json_output:
            typer.echo("Running attribution backfill...")

        stats = _request(
            "POST", "/app/speakers/api/backfill", json_body={"commit": True}
        )

        if json_output:
            result["backfill"] = stats
        else:
            typer.echo(
                f"Backfill complete: {stats['processed']} segments processed, "
                f"{stats['already_labeled']} already labeled"
            )

    if json_output:
        typer.echo(json.dumps(result, indent=2, default=str))


@app.command("reject-owner")
@convey_cli
def reject_owner_cmd() -> None:
    """Reject the owner voice candidate and enter 14-day cooldown."""
    result = _request("POST", "/app/speakers/api/owner/reject-cli")
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command("owner-ready")
@convey_cli
def owner_ready_cmd() -> None:
    """Check if owner voice detection should be surfaced to the user."""
    result = _request("POST", "/app/speakers/api/owner/ready")
    typer.echo(json.dumps(result, indent=2, default=str))
