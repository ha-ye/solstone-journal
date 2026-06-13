# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared command verb polarity helpers."""

from __future__ import annotations

import re

READ_VERBS: frozenset[str] = frozenset(
    {
        "load",
        "get",
        "read",
        "scan",
        "list",
        "show",
        "find",
        "match",
        "resolve",
        "query",
        "lookup",
        "status",
        "check",
        "validate",
        "discover",
        "format",
        "render",
        "extract",
        "parse",
        "view",
        "inspect",
        "info",
        "describe",
        "search",
    }
)

WRITE_VERBS: frozenset[str] = frozenset(
    {
        "save",
        "create",
        "add",
        "insert",
        "append",
        "attach",
        "delete",
        "remove",
        "update",
        "rename",
        "move",
        "promote",
        "merge",
        "seed",
        "consolidate",
        "bootstrap",
        "backfill",
        "dispatch",
        "record",
        "ingest",
        "import",
        "rebuild",
    }
)


def _verb_segments(verb: str) -> list[str]:
    base = verb.lstrip("_")
    return [part for part in re.split(r"[-_]+", base) if part]


def is_read_verb(verb: str) -> bool:
    """Return True when any hyphen/underscore-split segment is a read verb."""
    return any(part in READ_VERBS for part in _verb_segments(verb))


def classify_verb(verb: str) -> str:
    """Classify a CLI verb as read, write, or other by naming convention."""
    segments = _verb_segments(verb)
    if any(part in READ_VERBS for part in segments):
        return "read"
    if any(part in WRITE_VERBS for part in segments):
        return "write"
    return "other"
