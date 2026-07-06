# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import copy
import json

import pytest

from solstone.think.contract import journal
from solstone.think.journal_io.migrate import locked_rewrite_jsonl, rewrite_json


def _contract_fixture(name: str) -> bytes:
    return (journal.ROOT / "tests" / "fixtures" / "contract" / name).read_bytes()


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


def test_contract_validator_accepts_screen_no_raw_fixture() -> None:
    bundle = journal.build_bundle()
    schema = bundle["schemas"]["screen-jsonl"]["schema"]

    issues = journal.validate_contract_file(
        "screen.jsonl",
        _contract_fixture("tmux_screen_no_raw.jsonl"),
        schema,
    )

    assert issues == []


def test_contract_validator_accepts_audio_no_raw_fixture() -> None:
    bundle = journal.build_bundle()
    schema = bundle["schemas"]["audio-jsonl"]["schema"]

    issues = journal.validate_contract_file(
        "audio.jsonl",
        _contract_fixture("external_audio_no_raw.jsonl"),
        schema,
    )

    assert issues == []


def test_contract_validator_accepts_producer_headers_with_raw() -> None:
    bundle = journal.build_bundle()
    screen_schema = bundle["schemas"]["screen-jsonl"]["schema"]
    audio_schema = bundle["schemas"]["audio-jsonl"]["schema"]

    screen = b'{"raw":"screen.webm","observer":"desk"}\n{"timestamp":1.0}\n'
    audio = b'{"raw":"audio.flac","observer":"mic"}\n{"start":"00:00:00","text":"hi"}\n'

    assert journal.validate_contract_file("screen.jsonl", screen, screen_schema) == []
    assert journal.validate_contract_file("audio.jsonl", audio, audio_schema) == []


def test_contract_validator_accepts_audio_sound_tags_header() -> None:
    bundle = journal.build_bundle()
    audio_schema = bundle["schemas"]["audio-jsonl"]["schema"]
    header = {
        "raw": "audio.flac",
        "sound_tags": {
            "engine": "ced.cpp v0.1.0",
            "model": "ced-tiny-q8_0",
            "threshold": 0.1,
            "window_s": 10,
            "agg": "max",
            "windows": 2,
            "tags": {"Speech": 0.872, "Music": 0.201},
        },
    }
    audio = json.dumps(header).encode("utf-8") + b'\n{"start":"00:00:00","text":"hi"}\n'

    assert journal.validate_contract_file("audio.jsonl", audio, audio_schema) == []


def test_contract_validator_still_rejects_non_raw_floor_violations() -> None:
    bundle = journal.build_bundle()
    screen_schema = bundle["schemas"]["screen-jsonl"]["schema"]
    audio_schema = bundle["schemas"]["audio-jsonl"]["schema"]

    screen_issues = journal.validate_contract_file(
        "screen.jsonl",
        b'{"observer":"tmux"}\n{"content":{}}\n',
        screen_schema,
    )
    audio_issues = journal.validate_contract_file(
        "audio.jsonl",
        b'{"observer":"external"}\n{"start":"00:00:00"}\n',
        audio_schema,
    )

    assert any("timestamp" in issue.message for issue in screen_issues)
    assert any("text" in issue.message for issue in audio_issues)


def test_old_floor_no_raw_fixtures_failed_only_on_raw() -> None:
    bundle = journal.build_bundle()
    screen_schema = copy.deepcopy(bundle["schemas"]["screen-jsonl"]["schema"])
    audio_schema = copy.deepcopy(bundle["schemas"]["audio-jsonl"]["schema"])
    screen_schema["$defs"]["header"]["required"] = ["raw"]
    audio_schema["$defs"]["header"]["required"] = ["raw"]

    screen_issues = journal.validate_contract_file(
        "screen.jsonl",
        _contract_fixture("tmux_screen_no_raw.jsonl"),
        screen_schema,
    )
    audio_issues = journal.validate_contract_file(
        "audio.jsonl",
        _contract_fixture("external_audio_no_raw.jsonl"),
        audio_schema,
    )

    assert len(screen_issues) == 1
    assert "raw" in screen_issues[0].message
    assert "required" in screen_issues[0].message
    assert len(audio_issues) == 1
    assert "raw" in audio_issues[0].message
    assert "required" in audio_issues[0].message


def test_validate_journal_tree_accepts_no_raw_at_rest_files(tmp_path) -> None:
    segment = tmp_path / "chronicle" / "20260601" / "tmux" / "093000_300"
    segment.mkdir(parents=True)
    (segment / "screen.jsonl").write_bytes(
        _contract_fixture("tmux_screen_no_raw.jsonl")
    )
    (segment / "audio.jsonl").write_bytes(
        _contract_fixture("external_audio_no_raw.jsonl")
    )

    raw_segment = tmp_path / "chronicle" / "20260601" / "tmux" / "093500_300"
    raw_segment.mkdir(parents=True)
    (raw_segment / "screen.jsonl").write_bytes(
        b'{"raw":"screen.webm","observer":"desk"}\n{"timestamp":1.0}\n'
    )

    assert journal.validate_journal_tree(tmp_path, journal.build_bundle()) == []


def test_schema_for_filename_selects_screen_and_audio_sidecars() -> None:
    bundle = journal.build_bundle()

    for filename in (
        "screen.jsonl",
        "audio.jsonl",
        "123456_screen.jsonl",
        "src_audio.jsonl",
    ):
        assert journal.schema_for_filename(filename, bundle) is not None


def test_journal_contract_docs_cover_floor_and_maintenance_playbook() -> None:
    playbook = (
        journal.ROOT / "docs" / "journal-format-contract-maintenance.md"
    ).read_text(encoding="utf-8")

    assert "## Adding an observer with a new format" in playbook
    assert "## Forward-compatibility governing principle" in playbook
    assert journal.__doc__ is not None
    assert "Floor model" in journal.__doc__
    assert "producer-owned invariant" in journal.__doc__


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
