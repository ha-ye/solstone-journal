# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import importlib
from pathlib import Path

from tests.helpers.journal_config import seed_journal_config

mod = importlib.import_module(
    "solstone.apps.sol.maint.006_rename_unified_triage_providers"
)


def _seed_journal_config(journal: Path, data: object) -> Path:
    return seed_journal_config(data, journal)


def test_rename_unified_and_triage_provider_contexts_is_retired_noop(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config_path = _seed_journal_config(
        tmp_path,
        {
            "providers": {
                "contexts": {
                    "talent.system.unified": {"provider": "openai"},
                    "talent.system.triage": {"provider": "anthropic"},
                    "talent.system.morning_briefing": {"provider": "google"},
                }
            }
        },
    )
    before_bytes = config_path.read_bytes()
    before_mtime_ns = config_path.stat().st_mtime_ns

    summary = mod.run_migration(tmp_path, dry_run=False)

    assert summary.renamed == 0
    assert summary.removed == 0
    assert summary.preserved == 0
    assert summary.errors == 0
    assert summary.skipped_reason == "retired"
    assert config_path.read_bytes() == before_bytes
    assert config_path.stat().st_mtime_ns == before_mtime_ns


def test_retired_migration_dry_run_is_also_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    config_path = _seed_journal_config(
        tmp_path,
        {"providers": {"contexts": {"talent.system.unified": {"provider": "openai"}}}},
    )
    before = config_path.read_bytes()

    summary = mod.run_migration(tmp_path, dry_run=True)

    assert summary.skipped_reason == "retired"
    assert config_path.read_bytes() == before
