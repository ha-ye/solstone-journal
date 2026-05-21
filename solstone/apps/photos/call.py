# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import sys

import typer

from solstone.think.utils import get_journal, require_solstone

app = typer.Typer(
    name="photos",
    help="Photo intelligence from macOS Photos library.",
    no_args_is_help=True,
)


@app.callback()
def _require_up() -> None:
    require_solstone()


@app.command("sync")
def sync(
    library: str | None = typer.Option(
        None,
        "--library",
        help="Path to Photos.sqlite. Default: ~/Pictures/Photos Library.photoslibrary/database/Photos.sqlite",
    ),
) -> None:
    """Sync face clusters from macOS Photos to entity photo entries."""
    if sys.platform != "darwin":
        typer.echo("This command requires macOS (Photos library is macOS-only).")
        raise typer.Exit(1)

    import logging
    from pathlib import Path

    from solstone.apps.photos.reader import read_face_clusters
    from solstone.think.entities.journal import load_all_journal_entities
    from solstone.think.entities.matching import build_name_resolution_map
    from solstone.think.entities.photos import save_entity_photos

    logger = logging.getLogger(__name__)

    if library is None:
        library = str(
            Path.home()
            / "Pictures"
            / "Photos Library.photoslibrary"
            / "database"
            / "Photos.sqlite"
        )

    if not Path(library).exists():
        typer.echo(f"Photos database not found: {library}")
        raise typer.Exit(1)

    try:
        clusters = read_face_clusters(library)
    except Exception as e:
        typer.echo(f"Error reading Photos database: {e}")
        raise typer.Exit(1)

    journal = Path(get_journal())
    existing_slugs = {
        path.parent.name for path in journal.glob("entities/*/photos.jsonl")
    }

    typer.echo(f"Found {len(clusters)} named face clusters.")

    entity_dicts = list(load_all_journal_entities().values())
    face_names = [cluster["name"] for cluster in clusters]
    name_map = build_name_resolution_map(face_names, entity_dicts)

    matched = {name for name, entity_id in name_map.items() if entity_id}
    typer.echo(f"Matched {len(matched)} to entities.")

    slug_to_entries: dict[str, list[dict]] = {}
    for cluster in clusters:
        slug = name_map.get(cluster["name"])
        if not slug:
            continue

        entries = slug_to_entries.setdefault(slug, [])
        for day in cluster["days"]:
            entries.append(
                {
                    "day": day,
                    "face_cluster_pk": cluster["person_pk"],
                }
            )

    for slug in existing_slugs | set(slug_to_entries):
        save_entity_photos(slug, slug_to_entries.get(slug, []))

    entry_count = sum(len(entries) for entries in slug_to_entries.values())
    typer.echo(f"Created {entry_count} photo entries.")
    logger.info(
        "Photo sync: %d clusters, %d matched, %d entries",
        len(clusters),
        len(matched),
        entry_count,
    )
