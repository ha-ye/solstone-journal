# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Edge source registry and extractor contract.

An EDGE_SOURCES entry maps a chronicle-free journal-relative glob pattern to an
extractor function identified by ``(module_path, function_name)``. The indexer
driver loads source payloads before dispatch: ``.jsonl`` sources receive
``list[dict]`` from ``load_jsonl()``, and ``.json`` sources receive the parsed
JSON payload from ``read_json()``. Order matters: first match wins.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from importlib import import_module
from typing import Callable


@dataclass(frozen=True)
class EdgeContext:
    """Context passed to pure edge extractors."""

    path: str
    day: str
    facet: str
    resolve: Callable[[str], str | None]
    drop: Callable[[], None]


def segment_ref(path: str) -> tuple[str, str]:
    """Return (composite segment id, segment key) for a day-rooted segment path."""
    parts = path.replace("\\", "/").split("/")
    if len(parts) < 3:
        raise ValueError(f"invalid day-rooted segment path: {path}")
    return "/".join(parts[:3]), parts[2]


EDGE_SOURCES: dict[str, tuple[str, str]] = {
    "facets/*/activities/*.jsonl": (
        "solstone.think.activities",
        "extract_activity_edges",
    ),
    "facets/*/entities/*.jsonl": (
        "solstone.think.entities.edges",
        "extract_copresence_edges",
    ),
    "facets/*/events/*.jsonl": (
        "solstone.think.event_formatter",
        "extract_event_edges",
    ),
    "*/*/*/talents/speaker_labels.json": (
        "solstone.apps.speakers.edges",
        "extract_speaker_edges",
    ),
}

# Day-rooted patterns use discover_files()' chronicle-root branch and receive
# chronicle-free paths.


def edge_source_patterns() -> tuple[list[str], list[str]]:
    """Return EDGE_SOURCES split into structural and day-rooted pattern lists."""
    structural = [pattern for pattern in EDGE_SOURCES if not pattern.startswith("*/")]
    day_rooted = [pattern for pattern in EDGE_SOURCES if pattern.startswith("*/")]
    return structural, day_rooted


def _pattern_matches(pattern: str, rel_path: str) -> bool:
    pattern_parts = pattern.split("/")
    path_parts = rel_path.replace("\\", "/").split("/")
    if len(pattern_parts) != len(path_parts):
        return False
    return all(
        fnmatch.fnmatch(path_part, pattern_part)
        for pattern_part, path_part in zip(pattern_parts, path_parts, strict=True)
    )


def get_edge_source(rel_path: str) -> Callable | None:
    """Return the edge extractor for a chronicle-free journal-relative path."""
    for pattern, (module_path, func_name) in EDGE_SOURCES.items():
        if _pattern_matches(pattern, rel_path):
            module = import_module(module_path)
            return getattr(module, func_name)
    return None
