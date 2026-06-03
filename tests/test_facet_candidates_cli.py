# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from solstone.think.facet_candidates_cli import run
from solstone.think.facet_review_candidates import (
    facet_review_candidates_path,
    load_candidates,
)


def _write_segment_sense(
    journal: Path,
    day: str,
    segment: str,
    speculative_facet: str,
    *,
    stream: str = "archon",
) -> None:
    talents_dir = journal / "chronicle" / day / stream / segment / "talents"
    talents_dir.mkdir(parents=True, exist_ok=True)
    (talents_dir / "sense.json").write_text(
        json.dumps({"speculative_facet": speculative_facet}),
        encoding="utf-8",
    )


def test_run_records_and_upserts_facet_candidates(monkeypatch, tmp_path):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    day = datetime.now().strftime("%Y%m%d")
    for segment in ("090000_300", "093000_300", "100000_300"):
        _write_segment_sense(tmp_path, day, segment, "Home Reno")

    first_count = run()
    rows = load_candidates()

    assert first_count == 1
    assert facet_review_candidates_path().exists()
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "Home Reno"
    assert row["name_key"] == "home reno"
    assert row["status"] == "open"
    assert row["count"] == 3
    first_surfaced = row["first_surfaced"]
    created_at = row["created_at"]

    second_count = run()
    rows = load_candidates()

    assert second_count == 1
    assert len(rows) == 1
    assert rows[0]["first_surfaced"] == first_surfaced
    assert rows[0]["created_at"] == created_at
