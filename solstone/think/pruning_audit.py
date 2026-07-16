# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared audit writer for journal pruning runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from solstone.think.utils import day_log_checked


@dataclass
class AuditOutcome:
    """Outcome of writing pruning audit trails."""

    kind: str
    per_day_failures: dict[str, str] = field(default_factory=dict)
    global_record_written: bool = False
    global_record_error: str | None = None
    partial_error: bool = False


def write_prune_audit(
    journal_path: Path,
    *,
    kind: str,
    run_record: dict,
    per_day_messages: dict[str, str],
) -> AuditOutcome:
    """Write pruning audit logs.

    Callers are responsible for invoking this only after real work: not dry-run,
    not disabled, and at least one file or directory actually deleted.
    """
    if kind not in {"journal_logs", "raw_media", "raw_media_offload"}:
        raise ValueError(
            "kind must be 'journal_logs', 'raw_media', or 'raw_media_offload'"
        )

    outcome = AuditOutcome(kind=kind)
    for day, message in per_day_messages.items():
        try:
            day_log_checked(day, message)
        except Exception as exc:
            outcome.per_day_failures[day] = str(exc)

    try:
        audit_dir = journal_path / "health" / "pruning-runs"
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_path = audit_dir / f"{datetime.now():%Y%m%d}.jsonl"
        with open(audit_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(run_record) + "\n")
        outcome.global_record_written = True
    except Exception as exc:
        outcome.global_record_error = str(exc)

    outcome.partial_error = bool(outcome.per_day_failures) or (
        outcome.global_record_error is not None
    )
    return outcome
