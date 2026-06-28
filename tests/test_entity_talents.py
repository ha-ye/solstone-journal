# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for entity detection and review agent configurations."""

import pytest

from solstone.think.talent import get_talent


@pytest.fixture
def fixture_journal(monkeypatch):
    """Set SOLSTONE_JOURNAL to tests/fixtures/journal for testing."""
    monkeypatch.setenv("SOLSTONE_JOURNAL", "tests/fixtures/journal")
    yield
    # No cleanup needed - just testing reads


def test_entities_review_agent_config(fixture_journal):
    """Test review agent configuration loads correctly."""
    # Entity agents are in apps/entities/talent/ so use app-qualified name
    config = get_talent("entities:entities_review")

    # Verify required fields
    assert config["name"] == "entities:entities_review"
    assert "user_instruction" in config
    assert len(config["user_instruction"]) > 0

    # Verify JSON metadata fields from entities_review.json
    assert config.get("type") == "generate"
    assert config.get("title") == "Entity Reviewer"
    assert config.get("schedule") == "daily"
    assert config.get("priority") == 56
    assert config.get("multi_facet") is True
    assert config.get("output") == "json"
    assert isinstance(config.get("json_schema"), dict)


def test_entities_review_agent_instruction_content(fixture_journal):
    """Test review agent instruction contains expected sections."""
    config = get_talent("entities:entities_review")
    prompt = config["user_instruction"]

    assert prompt
    assert "sol call" not in prompt
    assert "emit_final" not in prompt
    assert "$facets" not in prompt
    assert "2+" not in prompt
    assert "3+" not in prompt
    assert "5+" not in prompt
    assert "timeless description" in prompt
    assert "canonical" in prompt


def test_agent_context_with_facet_focus(fixture_journal):
    """Test that get_talent with facet parameter uses focused single-facet context."""
    config = get_talent("chat", facet="full-featured")

    prompt = config["user_instruction"]

    # Should have Facet Focus section instead of Available Facets
    assert "## Facet Focus" in prompt
    assert "Available Facets" not in prompt

    # Should include the focused facet's details
    assert "Full Featured Facet" in prompt
    assert "A facet for testing all features" in prompt

    # Should include entity details from the focused facet (detailed format)
    assert "## Entities" in prompt
    assert "Entity 1" in prompt or "First test entity" in prompt
