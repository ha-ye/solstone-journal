# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests import verify_indexer_differential as harness
from tests._indexer_differential_fixtures import (
    FULLTEXT_TOP10_JACCARD_MIN,
    MARKDOWN_PARITY_CORPUS_FILES,
    MARKDOWN_PARITY_FULLTEXT_QUERY_CASES,
    MARKDOWN_PARITY_METADATA_FILTER_CASES,
)

ROOT = Path(__file__).resolve().parents[2]
MARKDOWN_PARITY_FIXTURE = ROOT / "tests" / "fixtures" / "markdown_parity"
pytestmark = pytest.mark.integration


def _quote_command(*parts: str | Path) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts)


def _build_native_binary() -> Path:
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not installed")
    subprocess.run(
        [
            "cargo",
            "build",
            "--manifest-path",
            "core/Cargo.toml",
            "--release",
            "-p",
            "solstone-core",
        ],
        cwd=ROOT,
        check=True,
    )
    binary = ROOT / "core" / "target" / "release" / "solstone-core"
    assert binary.exists()
    return binary


def _build_markdown_parity_corpus(dst: Path) -> Path:
    for entry in MARKDOWN_PARITY_CORPUS_FILES:
        source = MARKDOWN_PARITY_FIXTURE / entry["fixture_path"]
        target = dst / entry["fixture_path"]
        assert source.exists(), entry["fixture_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return dst


@pytest.mark.timeout(600)
def test_native_markdown_chunker_matches_python_functional_parity(
    tmp_path: Path,
) -> None:
    structures = {entry["structure"] for entry in MARKDOWN_PARITY_CORPUS_FILES}
    assert {
        "intro paragraph before ordinary list",
        "intro paragraph before three-row table",
        "definition-list 2-of-4 grouping boundary",
        "definition-list 2-of-5 non-grouping boundary",
        "multi-paragraph blockquote",
        "fenced code with info string",
        "loose nested list",
        "list item containing two paragraphs",
        "list item containing a fenced code block",
        "single over-2048-char neutral line plus searchable paragraph",
    } <= structures
    assert any(
        case["reference_distinct_paths"] > 10
        for case in MARKDOWN_PARITY_FULLTEXT_QUERY_CASES
    )

    journal_bin = Path(sys.executable).with_name("journal")
    if not journal_bin.exists():
        pytest.skip("journal entry point is not installed")

    source_journal = _build_markdown_parity_corpus(tmp_path / "source-journal")
    report = harness.run_differential(
        journal=source_journal,
        command_a=_quote_command(journal_bin, "indexer", "--rescan-full"),
        command_b=_quote_command(_build_native_binary(), "indexer", "--rescan-full"),
        work_root=tmp_path / "work",
        mode="functional",
        copy_mode="full",
        fulltext_cases=MARKDOWN_PARITY_FULLTEXT_QUERY_CASES,
        metadata_cases=MARKDOWN_PARITY_METADATA_FILTER_CASES,
    )

    assert report["classification"] == "functionally-equal", json.dumps(
        report,
        indent=2,
        sort_keys=True,
    )
    assert report["functional"]["failed_components"] == []
    assert all(
        case["jaccard"] >= FULLTEXT_TOP10_JACCARD_MIN
        for case in report["functional"]["fulltext"]["cases"]
    )
