# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for journal identity — identity directory read/write commands."""

import json
import re

import pytest
from typer.testing import CliRunner

from solstone.think.tools.sol import app

runner = CliRunner()
_HISTORY_FIELDS = [
    "ts",
    "file",
    "actor",
    "op",
    "section",
    "reason",
    "before_hash",
    "after_hash",
    "bytes_before",
    "bytes_after",
]


def _read_history(journal_path):
    history = journal_path / "identity" / "history.jsonl"
    return [json.loads(line) for line in history.read_text().splitlines()]


def _assert_history_record(record, *, file_name, actor, op, section, reason):
    assert list(record) == _HISTORY_FIELDS
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", record["ts"])
    assert record["file"] == file_name
    assert record["actor"] == actor
    assert record["op"] == op
    assert record["section"] == section
    assert record["reason"] == reason
    assert isinstance(record["before_hash"], str)
    assert isinstance(record["after_hash"], str)
    assert isinstance(record["bytes_before"], int)
    assert isinstance(record["bytes_after"], int)


@pytest.fixture
def journal_with_identity(tmp_path, monkeypatch):
    """Set up a journal with identity/ containing partner.md."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))

    # Provide minimal config for ensure_identity_directory
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "journal.json").write_text(
        json.dumps({"identity": {"name": "Test User"}})
    )

    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()

    partner_md = """\
# partner

Behavioral profile of the journal owner — observed patterns that help sol
adapt its responses, timing, and initiative to how this person actually works.

## work patterns
[observing]

## communication style
[observing]

## relationship priorities
[observing]

## decision style
[observing]

## expertise domains
[observing]
"""
    (identity_dir / "partner.md").write_text(partner_md)
    (identity_dir / "health.md").write_text(
        "## Status\n\n"
        "not yet generated\n\n"
        "## Needs your attention\n\n"
        "## Auto-repairs (last 7d)\n\n"
        "## Trends (last 7d)\n",
        encoding="utf-8",
    )

    return tmp_path


class TestSolPartnerRead:
    def test_read_partner(self, journal_with_identity):
        result = runner.invoke(app, ["partner"])
        assert result.exit_code == 0
        assert "# partner" in result.output
        assert "## work patterns" in result.output

    def test_read_partner_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "journal.json").write_text(json.dumps({}))
        # ensure_identity_directory creates partner.md
        result = runner.invoke(app, ["partner"])
        assert result.exit_code == 0


class TestSolPartnerWrite:
    def test_write_partner(self, journal_with_identity):
        new_content = "# partner\n\n## work patterns\nPrefers mornings for deep work.\n"
        result = runner.invoke(app, ["partner", "--write"], input=new_content)
        assert result.exit_code == 0
        assert "partner.md updated" in result.output

        partner_path = journal_with_identity / "identity" / "partner.md"
        assert partner_path.read_text() == new_content

    def test_write_partner_empty_stdin(self, journal_with_identity):
        result = runner.invoke(app, ["partner", "--write"], input="")
        assert result.exit_code == 1
        assert "no content" in result.output


class TestSolPartnerUpdateSection:
    def test_update_section_work_patterns(self, journal_with_identity):
        result = runner.invoke(
            app,
            ["partner", "--update-section", "work patterns"],
            input="Prefers async communication and morning deep work.",
        )
        assert result.exit_code == 0
        assert "Updated ## work patterns" in result.output

        partner_path = journal_with_identity / "identity" / "partner.md"
        content = partner_path.read_text()
        assert "Prefers async communication" in content
        assert "## communication style" in content
        assert "## decision style" in content

    def test_update_section_not_found(self, journal_with_identity):
        result = runner.invoke(
            app,
            ["partner", "--update-section", "nonexistent"],
            input="content",
        )
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_update_section_empty_stdin(self, journal_with_identity):
        result = runner.invoke(
            app,
            ["partner", "--update-section", "work patterns"],
            input="",
        )
        assert result.exit_code == 1
        assert "no content" in result.output


class TestSolWriteDoesNotEscapeIdentityDir:
    """Verify that journal identity only writes to identity/ files."""

    def test_partner_write_stays_in_identity_dir(self, journal_with_identity):
        """Write to partner.md goes to identity/partner.md, not anywhere else."""
        result = runner.invoke(app, ["partner", "--write"], input="test content\n")
        assert result.exit_code == 0
        partner_path = journal_with_identity / "identity" / "partner.md"
        assert partner_path.read_text() == "test content\n"
        journal_files = set(
            f.name for f in journal_with_identity.iterdir() if f.is_file()
        )
        assert "partner.md" not in journal_files


class TestSolPartnerValueOption:
    def test_write_partner_with_value(self, journal_with_identity):
        new_content = "# partner\n\n## work patterns\nMorning person.\n"
        result = runner.invoke(app, ["partner", "--write", "--value", new_content])
        assert result.exit_code == 0
        assert "partner.md updated" in result.output
        partner_path = journal_with_identity / "identity" / "partner.md"
        assert partner_path.read_text() == new_content

    def test_update_section_with_value(self, journal_with_identity):
        result = runner.invoke(
            app,
            [
                "partner",
                "--update-section",
                "work patterns",
                "--value",
                "Prefers mornings",
            ],
        )
        assert result.exit_code == 0
        assert "Updated ## work patterns" in result.output
        content = (journal_with_identity / "identity" / "partner.md").read_text()
        assert "Prefers mornings" in content

    def test_value_empty_string_errors(self, journal_with_identity):
        result = runner.invoke(app, ["partner", "--write", "--value", "   "])
        assert result.exit_code == 1
        assert "no content" in result.output


class TestSolHistoryLogging:
    def test_partner_write_logs_history(self, journal_with_identity):
        runner.invoke(app, ["partner", "--write", "--value", "# partner\n\nProfile.\n"])
        records = _read_history(journal_with_identity)
        assert len(records) == 1
        _assert_history_record(
            records[0],
            file_name="partner.md",
            actor="journal identity partner --write",
            op="replace",
            section=None,
            reason="manual replace",
        )

    def test_multiple_writes_append(self, journal_with_identity):
        runner.invoke(app, ["partner", "--write", "--value", "# partner\n\nFirst.\n"])
        runner.invoke(app, ["partner", "--write", "--value", "# partner\n\nSecond.\n"])
        records = _read_history(journal_with_identity)
        assert len(records) == 2

    def test_partner_update_section_logs_history(self, journal_with_identity):
        runner.invoke(
            app,
            [
                "partner",
                "--update-section",
                "work patterns",
                "--value",
                "Morning focus",
            ],
        )
        records = _read_history(journal_with_identity)
        assert len(records) == 1
        _assert_history_record(
            records[0],
            file_name="partner.md",
            actor="journal identity partner --update-section <heading>",
            op="update_section",
            section="work patterns",
            reason="manual section update",
        )
