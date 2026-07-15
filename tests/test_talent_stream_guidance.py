# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from solstone.think.talents import (
    _stream_content_description,
    _stream_import_guidance,
)


def test_stream_content_description_browser_suffix():
    description = _stream_content_description("suze.browser")

    assert "browser web apps" in description
    assert "Gmail or Slack" in description


def test_stream_import_guidance_browser_suffix():
    guidance = _stream_import_guidance("suze.browser")

    assert guidance.startswith("## Content Guidance\n\n")
    assert "web apps the owner was reading in their browser" in guidance
    assert "visible page text" in guidance
    assert "segment_start snapshot" in guidance


def test_stream_guidance_preserves_live_capture_outputs():
    assert _stream_content_description(None) == (
        "audio transcription and screen recording"
    )
    assert _stream_content_description("archon") == (
        "audio transcription and screen recording"
    )
    assert _stream_import_guidance(None) == _stream_import_guidance("archon")
    assert "## Live Capture Guidance" in _stream_import_guidance("archon")


def test_stream_guidance_preserves_exact_import_outputs():
    assert _stream_content_description("import.chatgpt") == (
        "an imported ChatGPT conversation"
    )
    guidance = _stream_import_guidance("import.chatgpt")

    assert guidance.startswith("## Content Guidance\n\n")
    assert "This is an AI conversation." in guidance


def test_stream_guidance_preserves_unknown_import_fallback():
    assert _stream_content_description("import.foo") == "imported content from foo"
    guidance = _stream_import_guidance("import.foo")

    assert guidance.startswith("## Content Guidance\n\n")
    assert "This is imported content." in guidance


def test_stream_guidance_preserves_unknown_stream_defaults():
    assert _stream_content_description("whatever") == "captured content"
    assert _stream_import_guidance("whatever") == ""
