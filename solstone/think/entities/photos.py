# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Journal entity photo entries.

Stored at entities/<slug>/photos.jsonl with one JSON object per line:
{"day": "YYYYMMDD", "face_cluster_pk": 123}

Example: save_entity_photos("alice", [{"day": "20240218", "face_cluster_pk": 1}])
"""

import json
from pathlib import Path
from typing import Any

from solstone.think.entities.core import atomic_write
from solstone.think.utils import get_journal


def entity_photos_path(entity_id: str) -> Path:
    """Return path to journal-level entity photos file."""
    return Path(get_journal()) / "entities" / entity_id / "photos.jsonl"


def save_entity_photos(entity_id: str, entries: list[dict[str, Any]]) -> None:
    """Save photo entries to an entity's photos file using atomic write."""
    path = entity_photos_path(entity_id)
    if not entries:
        path.unlink(missing_ok=True)
        return

    sorted_entries = sorted(
        (
            {
                "day": entry["day"],
                "face_cluster_pk": int(entry["face_cluster_pk"]),
            }
            for entry in entries
        ),
        key=lambda entry: (entry["day"], entry["face_cluster_pk"]),
    )
    content = "".join(
        json.dumps(entry, ensure_ascii=False) + "\n" for entry in sorted_entries
    )
    atomic_write(path, content, prefix=".photos_")
