# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json

import pytest

from solstone.think.tools.sol import _SPECIES_PREAMBLE, _hydrate


@pytest.fixture
def journal_path(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "journal.json").write_text(json.dumps({}))
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    return tmp_path


def test_identity_hydrate_reads_partner_section(journal_path):
    identity_dir = journal_path / "identity"
    identity_dir.mkdir()
    (identity_dir / "partner.md").write_text("partner body")

    output = _hydrate()

    expected = ["# species", "# partner"]
    positions = [output.index(marker) for marker in expected]
    assert positions == sorted(positions)
    assert "partner body" in output


def test_identity_hydrate_marks_missing_sections(journal_path):
    identity_dir = journal_path / "identity"
    identity_dir.mkdir()
    (identity_dir / "partner.md").write_text("partner body")

    output = _hydrate()

    assert "# partner\n\npartner body\n" in output


def test_identity_hydrate_handles_empty_identity_directory(journal_path):
    output = _hydrate()

    assert "# partner\n\n(not present)\n" in output


def test_identity_hydrate_starts_with_species_preamble(journal_path):
    identity_dir = journal_path / "identity"
    identity_dir.mkdir()
    (identity_dir / "partner.md").write_text("partner body")

    output = _hydrate()

    assert output.startswith("# species\n\n")
    assert _SPECIES_PREAMBLE in output
    expected = ["# species", "# partner"]
    positions = [output.index(marker) for marker in expected]
    assert positions == sorted(positions)


def test_identity_hydrate_strips_duplicate_section_heading(journal_path):
    identity_dir = journal_path / "identity"
    identity_dir.mkdir()
    (identity_dir / "partner.md").write_text("# partner\n\npartner body\n")

    output = _hydrate()

    assert output.splitlines().count("# partner") == 1
    assert "# partner\n\npartner body" in output


def test_identity_hydrate_preserves_non_matching_heading(journal_path):
    identity_dir = journal_path / "identity"
    identity_dir.mkdir()
    (identity_dir / "partner.md").write_text("# My Custom Heading\n\npartner body\n")

    output = _hydrate()

    assert "# My Custom Heading" in output
    assert output.splitlines().count("# partner") == 1
    assert "partner body" in output
