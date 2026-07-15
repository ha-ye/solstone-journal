# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Fixtures for transcripts app tests."""

import os
import shutil
import sys
from pathlib import Path

import pytest

from solstone.think.utils import get_project_root

ROOT = Path(get_project_root())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for name, module in list(sys.modules.items()):
    if name.split(".", 1)[0] != "solstone":
        continue
    module_file = getattr(module, "__file__", None)
    if module_file and not Path(module_file).resolve().is_relative_to(ROOT):
        sys.modules.pop(name, None)

from solstone.convey import create_app
from tests._baseline_harness import copytree_tracked


@pytest.fixture(autouse=True)
def _journal_env(request, monkeypatch):
    """Point tests at a copied journal when needed, otherwise the tracked fixture."""
    if "journal_copy" in request.fixturenames:
        journal_copy = request.getfixturevalue("journal_copy")
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_copy))
        return

    monkeypatch.setenv(
        "SOLSTONE_JOURNAL",
        os.path.join(os.getcwd(), "tests", "fixtures", "journal"),
    )


@pytest.fixture
def client(journal_copy, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal_copy))
    app = create_app(str(journal_copy))
    return app.test_client()


@pytest.fixture
def journal_copy(tmp_path, monkeypatch):
    src = Path(get_project_root()) / "tests" / "fixtures" / "journal"
    dst = tmp_path / "journal"
    copytree_tracked(src, dst)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(dst.resolve()))
    return dst


@pytest.fixture
def seed_browser_fixture_inventory(journal_copy):
    """Overlay browser fixture files that are new to this lode into journal_copy."""

    source_chronicle = ROOT / "tests" / "fixtures" / "journal" / "chronicle"
    target_chronicle = journal_copy / "chronicle"

    def seed() -> None:
        for day in ("20260701", "20260702"):
            shutil.copytree(
                source_chronicle / day,
                target_chronicle / day,
                dirs_exist_ok=True,
            )
        docs_file = (
            source_chronicle
            / "20260703"
            / "suze.browser"
            / "000141_317"
            / "browser_docs-google-com.jsonl"
        )
        target_file = (
            target_chronicle
            / "20260703"
            / "suze.browser"
            / "000141_317"
            / "browser_docs-google-com.jsonl"
        )
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(docs_file, target_file)
        corrupt = (
            target_chronicle
            / "20260701"
            / "workstation.browser"
            / "100000_300"
            / "browser_corrupt-example-com.jsonl"
        )
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("{bad json\n", encoding="utf-8")

    return seed
