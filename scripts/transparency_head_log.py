#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Append-only in-repo transparency head witness log."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.release_candidate_driver import DriverError
from scripts.transparency_core import (
    HEAD_LOG,
    canonical_json_bytes,
    failure,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class HeadLogRow:
    product: str
    seq: int
    version: str
    entry_sha256: str
    published_utc: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_sha256": self.entry_sha256,
            "product": self.product,
            "published_utc": self.published_utc,
            "seq": self.seq,
            "version": self.version,
        }


@dataclass(frozen=True)
class WitnessStatus:
    state: str
    message: str


def head_log_path(root: Path) -> Path:
    return root / HEAD_LOG


def read_head_log(root: Path) -> tuple[HeadLogRow, ...]:
    path = head_log_path(root)
    if not path.exists():
        return ()
    rows: list[HeadLogRow] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DriverError(
                [
                    failure(
                        "transparency head log row is not JSON",
                        expected=f"{HEAD_LOG}:{line_number} JSON object",
                        actual=type(exc).__name__,
                        repair=f"restore {HEAD_LOG} from git and retry",
                    )
                ]
            ) from None
        try:
            rows.append(
                HeadLogRow(
                    product=str(payload["product"]),
                    seq=int(payload["seq"]),
                    version=str(payload["version"]),
                    entry_sha256=str(payload["entry_sha256"]),
                    published_utc=str(payload["published_utc"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DriverError(
                [
                    failure(
                        "transparency head log row is invalid",
                        expected="product, seq, version, entry_sha256, published_utc",
                        actual=f"{HEAD_LOG}:{line_number} {type(exc).__name__}",
                        repair=f"restore {HEAD_LOG} from git and retry",
                    )
                ]
            ) from None
    return tuple(rows)


def highest_seq(root: Path, *, product: str) -> int:
    rows = [row.seq for row in read_head_log(root) if row.product == product]
    return max(rows, default=0)


def append_head_row(root: Path, row: HeadLogRow) -> bool:
    path = head_log_path(root)
    rows = read_head_log(root)
    for existing in rows:
        if existing.product == row.product and existing.seq == row.seq:
            if existing.entry_sha256 != row.entry_sha256:
                raise DriverError(
                    [
                        failure(
                            "transparency head log fork detected",
                            expected=f"{row.product} seq {row.seq} entry {existing.entry_sha256}",
                            actual=row.entry_sha256,
                            repair=f"stop and audit {HEAD_LOG} before publishing again",
                        )
                    ]
                )
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(canonical_json_bytes(row.as_dict(), label=HEAD_LOG))
    return True


def git_witness_status(
    root: Path,
    *,
    runner: Runner = subprocess.run,
) -> WitnessStatus:
    tracked = runner(
        ["git", "ls-files", "--error-unmatch", "--", HEAD_LOG],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if tracked.returncode == 1:
        return WitnessStatus(
            state="written-untracked",
            message=(
                f"{HEAD_LOG} row is present but untracked; run: "
                f"git add {HEAD_LOG} && git commit"
            ),
        )
    if tracked.returncode != 0:
        return WitnessStatus(
            state="witness-unavailable",
            message=(
                "git witness status unavailable; verify the transparency head row "
                f"with: git ls-files --error-unmatch -- {HEAD_LOG}"
            ),
        )
    diff = runner(
        ["git", "diff", "--quiet", "--exit-code", "HEAD", "--", HEAD_LOG],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if diff.returncode == 0:
        return WitnessStatus(
            state="written-and-committed",
            message=f"{HEAD_LOG} row is present and committed",
        )
    if diff.returncode == 1:
        return WitnessStatus(
            state="written-uncommitted",
            message=(
                f"{HEAD_LOG} row is present but uncommitted; run: "
                f"git add {HEAD_LOG} && git commit"
            ),
        )
    return WitnessStatus(
        state="witness-unavailable",
        message=(
            "git witness status unavailable; verify the transparency head row "
            f"with: git diff --quiet --exit-code HEAD -- {HEAD_LOG}"
        ),
    )
