# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owns <journal>/.config/vertex-credentials.json for Vertex credentials."""

import json
from pathlib import Path

from solstone.think.journal_io.atomic import atomic_replace


def _canonical_path(journal_root: Path) -> Path:
    return journal_root / ".config" / "vertex-credentials.json"


def save_vertex_credentials(creds_data: dict, journal_root: Path) -> Path:
    """Save Vertex credentials to the canonical journal secret path."""
    path = _canonical_path(journal_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(creds_data, indent=2, ensure_ascii=False) + "\n"
    atomic_replace(path, payload, mode=0o600)
    return path


def delete_vertex_credentials(configured_path: str | Path, journal_root: Path) -> bool:
    """Guard canonical path resolution before unlinking Vertex credentials.

    Returns whether the guard matched and an unlink was attempted. This does not
    touch config; callers own config mutation.
    """
    canonical = _canonical_path(journal_root)
    if Path(configured_path).resolve() != canonical.resolve():
        return False
    try:
        canonical.unlink(missing_ok=True)
    except OSError:
        pass
    return True
