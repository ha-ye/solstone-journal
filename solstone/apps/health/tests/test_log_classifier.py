# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solstone.apps.health.log_classifier import classify_level

FIXTURE_PATH = (
    Path(__file__).resolve().parents[4] / "tests/fixtures/health_logs_classified.jsonl"
)


def _load_fixture_rows() -> list[dict[str, str]]:
    return [
        json.loads(line)
        for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(row, id=f"{index}-{row['service']}-{row['level']}")
        for index, row in enumerate(_load_fixture_rows())
    ],
)
def test_golden_fixture_classifications(row):
    assert classify_level(row["stream"], row["line"]) == row["level"]


@pytest.mark.parametrize(
    "line",
    [
        "ERROR:root:failed to process segment",
        "2026-04-16 ERROR supervisor task failed",
        "agent emitted ERROR while writing output",
        "[worker] ERROR could not open file",
        "ERROR",
        "pre ERROR post",
        "CRITICAL handler also emitted ERROR",
        "FATAL runner abort after ERROR",
        "ERROR: exit code 1",
        "Task failed with ERROR status",
        "stderr ERROR line",
        "ERROR loading provider",
        "ERROR in handler",
        "ERROR - retry exhausted",
        "ERROR: [Errno 2] missing file",
        "subprocess returned ERROR",
        "ERROR while streaming stdout",
        "pipeline ERROR token",
        "ERROR after traceback",
        "ERROR before shutdown",
    ],
)
def test_old_stderr_error_heuristic_is_subset(line):
    assert classify_level("stderr", line) == "error"
