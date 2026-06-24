# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import copy
import json

import pytest

from solstone.think.contract import journal
from solstone.think.journal_io.migrate import locked_rewrite_jsonl, rewrite_json


def test_journal_contract_bundle_discovers_writer_adjacent_schemas() -> None:
    bundle = journal.build_bundle()
    formats = set(bundle["schemas"])

    assert {
        "observer-ingest-envelope",
        "stream-json",
        "audio-jsonl",
        "screen-jsonl",
    }.issubset(formats)

    for entry in bundle["schemas"].values():
        meta = entry["schema"]["x-journal-contract"]
        assert meta["schema_owner"]
        assert meta["reference_writer"]
        assert meta["allowed_producers"]
        assert meta["write_discipline"]


def test_contract_validator_accepts_audio_jsonl_and_reports_missing_text() -> None:
    bundle = journal.build_bundle()
    schema = bundle["schemas"]["audio-jsonl"]["schema"]

    valid = b'{"raw":"audio.flac"}\n{"start":"00:00:00","text":"hello"}\n'
    assert journal.validate_contract_file("audio.jsonl", valid, schema) == []

    invalid = b'{"raw":"audio.flac"}\n{"start":"00:00:00"}\n'
    issues = journal.validate_contract_file("audio.jsonl", invalid, schema)

    assert any("'text' is a required property" in issue.message for issue in issues)


def test_contract_breaking_change_tripwire_flags_removed_key_fields() -> None:
    committed = journal.build_bundle()
    current = copy.deepcopy(committed)
    key_fields = current["schemas"]["audio-jsonl"]["schema"]["x-journal-contract"][
        "key_fields"
    ]
    key_fields.remove("record.text")

    breaking = journal.classify_breaking_changes(current, committed)

    assert "audio-jsonl: removed key field 'record.text'" in breaking


def test_contract_validates_committed_fixture_journal() -> None:
    issues = journal.validate_journal_tree(
        journal.ROOT / "tests" / "fixtures" / "journal",
        journal.build_bundle(),
    )

    assert issues == []


def test_migration_helpers_support_dry_run_and_locked_jsonl_rewrite(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"a":1}\n', encoding="utf-8")

    dry_run = rewrite_json(
        state_path,
        lambda value: {**value, "b": 2},
        dry_run=True,
    )
    assert dry_run.files_changed == 1
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"a": 1}

    rewrite = rewrite_json(state_path, lambda value: {**value, "b": 2})
    assert rewrite.files_changed == 1
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"a": 1, "b": 2}

    transcript_path = tmp_path / "audio.jsonl"
    transcript_path.write_text(
        '{"raw":"audio.flac"}\n{"start":"00:00:00","text":"hello"}\n',
        encoding="utf-8",
    )

    result = locked_rewrite_jsonl(
        transcript_path,
        lambda record: (
            {**record, "text": record["text"].upper()} if "text" in record else record
        ),
    )

    lines = transcript_path.read_text(encoding="utf-8").splitlines()
    assert result.records_seen == 2
    assert result.records_changed == 1
    assert json.loads(lines[1])["text"] == "HELLO"


def test_migration_validator_failure_preserves_original_file(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"a":1}\n', encoding="utf-8")

    def reject_b(path):
        value = json.loads(path.read_text(encoding="utf-8"))
        return ["b is invalid"] if value.get("b") == 2 else []

    with pytest.raises(ValueError, match="b is invalid"):
        rewrite_json(state_path, lambda value: {**value, "b": 2}, validator=reject_b)

    assert json.loads(state_path.read_text(encoding="utf-8")) == {"a": 1}
