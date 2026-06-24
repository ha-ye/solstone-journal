# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Safe field-migration helpers for journal maintenance tasks."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solstone.think.journal_io.atomic import write_json, write_jsonl
from solstone.think.journal_io.locking import hold_lock


@dataclass(frozen=True)
class RewriteResult:
    files_seen: int = 1
    files_changed: int = 0
    records_seen: int = 0
    records_changed: int = 0
    dry_run: bool = False


Validator = Callable[[Path], list[str]]


def _validation_error(errors: list[str]) -> ValueError:
    return ValueError("; ".join(errors))


def _validate_candidate(path: Path, body: str, validator: Validator | None) -> None:
    if validator is None:
        return
    with tempfile.TemporaryDirectory(
        dir=path.parent,
        prefix=f".{path.name}.validate_",
    ) as tmpdir:
        candidate = Path(tmpdir) / path.name
        candidate.write_text(body, encoding="utf-8")
        errors = validator(candidate)
        if errors:
            raise _validation_error(errors)


def validate_fixture(path: Path, validator: Validator) -> list[str]:
    """Run a migration validator against a fixture or journal path."""
    return validator(path)


def rewrite_json(
    path: Path,
    transform: Callable[[Any], Any],
    *,
    dry_run: bool = False,
    validator: Validator | None = None,
) -> RewriteResult:
    """Atomically rewrite one JSON file after applying ``transform``."""
    before = json.loads(path.read_text(encoding="utf-8"))
    after = transform(before)
    changed = after != before
    if changed and not dry_run:
        _validate_candidate(path, json.dumps(after, indent=2) + "\n", validator)
        write_json(path, after)
    return RewriteResult(
        files_changed=1 if changed else 0,
        records_seen=1,
        records_changed=1 if changed else 0,
        dry_run=dry_run,
    )


def rewrite_jsonl(
    path: Path,
    transform: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    dry_run: bool = False,
    validator: Validator | None = None,
) -> RewriteResult:
    """Atomically rewrite a JSONL file after applying ``transform`` per record."""
    records: list[dict[str, Any]] = []
    changed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        before = json.loads(line)
        if not isinstance(before, dict):
            raise ValueError(f"{path}: JSONL records must be objects")
        after = transform(dict(before))
        records.append(after)
        if after != before:
            changed += 1
    if changed and not dry_run:
        body = "".join(json.dumps(record) + "\n" for record in records)
        _validate_candidate(path, body, validator)
        write_jsonl(path, records)
    return RewriteResult(
        files_changed=1 if changed else 0,
        records_seen=len(records),
        records_changed=changed,
        dry_run=dry_run,
    )


def locked_rewrite_json(
    path: Path,
    transform: Callable[[Any], Any],
    *,
    dry_run: bool = False,
    validator: Validator | None = None,
) -> RewriteResult:
    """Hold the journal sidecar lock while rewriting one JSON file."""
    with hold_lock(path):
        return rewrite_json(path, transform, dry_run=dry_run, validator=validator)


def locked_rewrite_jsonl(
    path: Path,
    transform: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    dry_run: bool = False,
    validator: Validator | None = None,
) -> RewriteResult:
    """Hold the journal sidecar lock while rewriting one JSONL file."""
    with hold_lock(path):
        return rewrite_jsonl(path, transform, dry_run=dry_run, validator=validator)
