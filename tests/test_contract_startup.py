# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solstone.convey import create_app
from solstone.think.contract import journal as contract_journal


def _setup_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    journal = tmp_path / "journal"
    journal.mkdir()

    config_dir = journal / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "journal.json"
    config_file.write_text(
        json.dumps(
            {
                "setup": {"completed_at": 1700000000000},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    return journal


def test_startup_populates_contract_bundle_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _setup_journal(tmp_path, monkeypatch)

    app = create_app(journal=str(journal))
    bundle = app.config["JOURNAL_CONTRACT_BUNDLE"]

    assert bundle["contract"] == "solstone-journal-at-rest"
    assert {
        "observer-ingest-envelope",
        "stream-json",
        "audio-jsonl",
        "screen-jsonl",
    }.issubset(bundle["schemas"])


def test_startup_fails_loud_on_missing_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _setup_journal(tmp_path, monkeypatch)
    missing = Path("/nonexistent/solstone-contract/layout.json")
    monkeypatch.setattr(contract_journal, "LAYOUT_PATH", missing)

    with pytest.raises(RuntimeError) as exc_info:
        create_app(journal=str(journal))

    message = str(exc_info.value)
    assert "failed to load at startup" in message
    assert str(missing) in message
    assert exc_info.value.__cause__ is not None


def test_startup_fails_loud_on_invalid_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _setup_journal(tmp_path, monkeypatch)
    broken_path = tmp_path / "broken.schema.json"
    broken_path.write_text(json.dumps({"type": 123}), encoding="utf-8")
    monkeypatch.setattr(
        contract_journal,
        "discover_schema_sources",
        lambda root=contract_journal.ROOT: [broken_path],
    )

    with pytest.raises(RuntimeError) as exc_info:
        create_app(journal=str(journal))

    message = str(exc_info.value)
    assert "failed to load at startup" in message
    assert "invalid JSON Schema" in message
    assert broken_path.name in message
    assert exc_info.value.__cause__ is not None
