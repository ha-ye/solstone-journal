# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for observe/describe.py category discovery and configuration."""

from pathlib import Path

from solstone.observe import describe as describe_module
from solstone.observe.describe import _build_redact_instruction
from solstone.think.prompts import load_prompt


def test_categories_discovered():
    """Test that categories are discovered on import."""
    CATEGORIES = describe_module.CATEGORIES

    # Should have discovered at least some categories
    assert isinstance(CATEGORIES, dict)
    assert len(CATEGORIES) > 0


def test_categories_have_required_fields():
    """Test that all categories have required metadata."""
    CATEGORIES = describe_module.CATEGORIES

    for category, metadata in CATEGORIES.items():
        # Every category must have description
        assert "description" in metadata, f"Category {category} missing 'description'"
        assert isinstance(metadata["description"], str)
        assert len(metadata["description"]) > 0

        # Every category should have output field (defaulted if not set)
        assert "output" in metadata, f"Category {category} missing 'output'"
        assert metadata["output"] in ("json", "markdown")

        # Every category should have context field (for provider resolution)
        assert "context" in metadata, f"Category {category} missing 'context'"
        assert metadata["context"].startswith("observe.describe.")

        # Every category should have an output-token budget
        assert "max_output_tokens" in metadata
        assert isinstance(metadata["max_output_tokens"], int)
        assert metadata["max_output_tokens"] > 0


def test_extractable_categories_have_prompts():
    """Test that extractable categories have valid prompts loaded."""
    CATEGORIES = describe_module.CATEGORIES

    extractable_count = 0
    for category, metadata in CATEGORIES.items():
        if "prompt" in metadata:
            extractable_count += 1
            assert isinstance(metadata["prompt"], str)
            assert len(metadata["prompt"]) > 0, f"Category {category} has empty prompt"

    # Sanity check: we should have at least some extractable categories
    assert extractable_count > 0, "No extractable categories found"


def test_category_max_output_token_defaults_and_overrides():
    """Test category output-token defaults and explicit overrides."""
    categories_dir = Path(describe_module.__file__).resolve().parent / "categories"
    declared_overrides = []
    defaulted_categories = []

    for md_path in categories_dir.glob("*.md"):
        category = md_path.stem
        prompt_content = load_prompt(category, base_dir=categories_dir)
        metadata = describe_module.CATEGORIES[category]

        if "max_output_tokens" in prompt_content.metadata:
            declared_overrides.append(category)
            assert (
                metadata["max_output_tokens"]
                == prompt_content.metadata["max_output_tokens"]
            )
        else:
            defaulted_categories.append(category)
            assert metadata["max_output_tokens"] == 4096

    assert declared_overrides
    assert defaulted_categories


def test_categorization_prompt_built():
    """Test that categorization prompt is built correctly."""
    prompt = describe_module.CATEGORIZATION_PROMPT

    # Should contain all category descriptions
    for category, metadata in describe_module.CATEGORIES.items():
        assert f"- {category}:" in prompt
        assert metadata["description"] in prompt

    # Should have the template structure
    assert "primary" in prompt
    assert "secondary" in prompt
    assert "overlap" in prompt
    assert "Categories (choose one):" in prompt


def test_categorization_prompt_alphabetical():
    """Test that categories in prompt are alphabetically ordered."""
    prompt = describe_module.CATEGORIZATION_PROMPT

    # Extract category lines from prompt
    lines = prompt.split("\n")
    category_lines = [line for line in lines if line.startswith("- ") and ":" in line]

    # Extract category names
    categories = [line.split(":")[0].replace("- ", "") for line in category_lines]

    # Should be sorted
    assert categories == sorted(categories)


def test_categorization_prompt_is_frozen_literal():
    """Test that categorization prompt matches the hand-authored literal."""
    expected = """You have one job: identify the primary foreground and (if present) secondary app categories in this desktop screenshot, and return ONLY this JSON:

{
  "visual_description":"<1–2 sentences describing what is visible>",
  "primary": "<largest and most visible app category>",
  "secondary": "<second most visible app category or 'none'>",
  "overlap": <boolean, does the primary overlap or cover the secondary, or is it fully standalone and separate>
}

Rules:
- For visual_description summarize the **overall desktop view** in **1–2 sentences** for a visually impaired person, first state what kind of content dominates the screen (app UI, photo/video, feed/thread, text document, terminal, or meeting), then summarize layout and window arrangement.
- For the most visible primary foreground app choose the best category from the list below.
- Set "secondary" to "none" and "overlap" to true if the primary effectively fills the screen or no distinct second category/window is visible.
- Set overlap to true if the primary app overlaps, covers, clips, or obscures the secondary in any way.
- Only set a category for secondary if it is very visible and occupies more than 30% of the screen.

Categories (choose one):
- browsing: General web browsing, news, shopping, or reference pages without a dominant social feed or media viewer
- calendar: Calendar and scheduling interfaces: day/week/month views, agenda lists, event details, event creation forms, booking pages, availability grids, and RSVP/scheduling workflows
- code: Code editors and IDEs
- gaming: Video games, puzzles, idle games
- media: Photos, video players, image galleries, or visual media dominating the view, even when displayed inside a browser tab
- meeting: Video calls/conferencing (Zoom, Meet, Teams, Webex, etc.)
- messaging: Chat or email apps (Slack, Discord, Messages/iMessage, Gmail, etc.)
- productivity: Spreadsheets, slides, document editors, task and issue tracking tools, dashboards, other workplace desktop or web apps and professional tools that are not primarily calendar/scheduling views
- reading: Documents, articles, PDFs, documentation
- social: Social platforms with feeds, threads, profiles, posts, comments, or timelines (X, Bluesky, Reddit, Instagram, TikTok, LinkedIn, Mastodon, HN)
- terminal: Command line interfaces, logs, shell

Tie-break rules:
- If a photo, video, image gallery, or visual media fills most of the screen, choose media even when it is inside a browser.
- If the dominant surface is a feed, thread, profile, posts, comments, or timeline, choose social rather than browsing.
- Choose browsing for ordinary web pages, search, news, shopping, or documentation when no social feed or media viewer dominates.
- Choose calendar for calendar grids, agenda views, event detail/edit forms, availability pickers, booking pages, and scheduling assistants, even when they appear inside a browser or productivity suite.
- Choose meeting only for an active live call/conference UI; a calendar event for a meeting is calendar, not meeting.
- Choose messaging/email when the dominant surface is an email or chat conversation, even if it discusses scheduling; choose calendar when an invite/event editor, RSVP pane, availability grid, or booking flow is dominant."""
    prompt = describe_module.CATEGORIZATION_PROMPT

    assert prompt == expected
    assert "–" in prompt
    assert "1-2 sentences" not in prompt
    assert "�" not in prompt
    assert "Tie-break rules:" in prompt
    assert prompt.index("- terminal:") < prompt.index("Tie-break rules:")
    assert prompt.rstrip().endswith(
        "choose calendar when an invite/event editor, RSVP pane, availability grid, or booking flow is dominant."
    )


def test_redact_instruction_empty():
    """Test that empty/missing redact list returns empty string."""
    assert _build_redact_instruction([]) == ""
    assert _build_redact_instruction(None) == ""


def test_redact_instruction_format():
    """Test that redact instruction formats rules correctly."""
    rules = [
        "use *** instead of any visible passwords",
        "replace personal email addresses with [redacted]",
    ]
    result = _build_redact_instruction(rules)

    # Should contain the header
    assert "Redaction rules" in result
    assert "do not generalize" in result

    # Should contain each rule as a bullet
    for rule in rules:
        assert f"- {rule}" in result


def test_redact_instruction_no_vague_language():
    """Test that redact instruction doesn't add vague privacy language."""
    rules = ["replace bank account numbers with ***"]
    result = _build_redact_instruction(rules)

    # Should not contain vague directives the model could over-apply
    lower = result.lower()
    assert "sensitive" not in lower
    assert "confidential" not in lower
    assert "personally identifiable" not in lower
    assert "pii" not in lower
