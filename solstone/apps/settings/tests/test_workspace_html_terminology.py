# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
from pathlib import Path

from solstone.apps.settings import install_copy

WORKSPACE = Path(__file__).resolve().parents[1] / "workspace.html"
LEGACY_TERMS = (
    "Downloading...",
    "Verifying...",
    "Installing...",
    "Validating key...",
    "Install may be stuck",
    "Retry?",
    "'enabling'",
    "'key-validating'",
    "'install-failed'",
    "'installed-no-key'",
    "'invalid-key'",
    "'not-enabled'",
    "stuck_enabling",
    "state.state",
)
UNIQUE_INSTALL_COPY_NAMES = (
    "STT_LOCAL_REQUIREMENTS_TEMPLATE",
    "STT_LOCAL_UNSUPPORTED",
    "STT_DETECTED_MEMORY_TEMPLATE",
    "STT_DETECTED_MEMORY_UNKNOWN",
    "STT_NO_LOCAL_STT_RECOVERY",
    "STT_EXPLICIT_LOCAL_LOW_TEMPLATE",
)


def _workspace_text() -> str:
    return WORKSPACE.read_text(encoding="utf-8")


def test_workspace_has_no_legacy_install_state_terms():
    text = _workspace_text()

    for term in LEGACY_TERMS:
        assert term not in text


def test_workspace_does_not_duplicate_install_copy_strings():
    text = _workspace_text()

    for name in UNIQUE_INSTALL_COPY_NAMES:
        value = getattr(install_copy, name)
        assert text.count(value) == 0


def test_workspace_observer_nav_copy_uses_devices():
    text = _workspace_text()

    assert '<optgroup label="devices">' in text
    assert '<option value="observer">devices</option>' in text
    assert '<div class="settings-nav-label">devices</div>' in text
    assert re.search(
        r'<button[^>]+data-section="observer"[^>]*>\s*devices\s*</button>',
        text,
    )
    assert "<h2>devices</h2>" in text

    assert '<optgroup label="observation">' not in text
    assert '<option value="observer">observer</option>' not in text
    assert '<div class="settings-nav-label">observation</div>' not in text
    assert (
        re.search(
            r'<button[^>]+data-section="observer"[^>]*>\s*observer\s*</button>',
            text,
        )
        is None
    )
    assert "<h2>observer</h2>" not in text


def test_workspace_owner_copy_uses_approved_case_and_ellipsis_folds():
    text = _workspace_text()
    unescaped = text.replace("\\'", "'")

    expected_phrases = (
        "your identity information used in AI templates and transcription.",
        "brief description of yourself used in AI templates",
        "select pronouns…",
        "import audio from Plaud recorder. log into the web portal and extract token from browser console.",
        "key configured (enter new value to replace)",
        "key available from system environment",
        "enter API key",
        " ✓ valid",
        "invalid",
        "speech-to-text backend and processing settings.",
        "speech-to-text engine to use for transcription",
        "installed Parakeet model version for local transcription",
        "runtime preference for Parakeet on this host",
        "abort Parakeet model work if it exceeds this timeout",
        "platform runtime:",
        "keep audio files even when no speech is detected",
        "selecting this backend fetches two external artifacts into this journal's",
        "provider cache: the parakeet.cpp server from github.com and the speech model",
        "install with <code>journal install-provider parakeet</code>.",
        "couldn't load transcription settings",
        "configure how the observer works and connects to your journal.",
        "when your screen is idle, the observer can take in terminal content from active tmux sessions.",
        "how often to poll terminal content",
        "changes take effect on next observer restart.",
        "couldn't load observer settings",
        "screen analysis settings for observe-describe.",
        "maximum frames to extract detailed content from per 5 minute screen segment (big impact on token usage)",
        "add a redaction rule…",
        "instructions the ai follows to redact sensitive content from screen analysis. double-click a rule to edit it.",
        "categories",
        "set importance and extraction rules for each screen category. double-click guidance text to edit.",
        "loading…",
        "no categories",
        "disabled for ignored categories",
        "no extraction guidance",
        "save failed",
        "rule must be 200 characters or less",
        "maximum of 50 rules allowed",
        "configure how your sol communicates with sol pbc support.",
        "detect repeated errors and suggest filing a support ticket. no data is sent — only a local notification.",
        "strip installation identifiers when submitting feedback.",
        "support portal endpoint. change this if you run your own portal.",
        "manage muted facets. muted facets are hidden from the facet bar but retain all their data.",
        "no facets yet.",
        "failed to load facets.",
        "no muted facets.",
        "failed to load muted facets.",
        "unmuted",
        "load failed",
        "error loading facet configuration",
        "failed to unmute facet",
        "reload to apply",
        "click to update facet bar",
        "your assistant's name and identity.",
        "the name your assistant uses to identify itself",
        "how the name was set: default, chosen, self-named, or deferred",
        "reset",
        "name reset to sol",
        "reset failed",
        "could not reset name",
        "no actions logged",
        "no activities attached yet. add activities below.",
        "all default activities attached",
        "no instructions",
        "no description",
        "no description set",
        "detection hints and level guidance",
        "add failed",
        "update failed",
        "remove failed",
        "creation failed",
        "validation error",
        "name is required",
        "no redaction rules configured",
        "could not save name",
        "couldn't refresh storage settings — showing last known state.",
        "couldn't load storage settings",
        "use global default",
        "keep forever",
        "keep N days",
        "delete after processing",
        "all streams",
        "invalid input",
        "enter a positive number of days.",
        "cleanup failed",
        "couldn't load settings",
        "'blue'",
        "'green'",
        "'teal'",
        "'yellow'",
        "'purple'",
        "'orange'",
        "'pink'",
        "'gray'",
        "'mint'",
        "'red-orange'",
        "'deep purple'",
        "'brown'",
    )
    for phrase in expected_phrases:
        assert phrase in unescaped

    retired_phrases = (
        "brief description of yourself used in ai templates",
        " ✓ Valid",
        "Local transcription",
        "Free memory",
        "No actions logged",
        "All default activities attached",
        "Select pronouns...",
    )
    for phrase in retired_phrases:
        assert phrase not in unescaped

    assert "used in ai templates" not in text
    assert "lucide icon" in text
    assert "choose lucide icon" in text
    assert "Lucide" in text
