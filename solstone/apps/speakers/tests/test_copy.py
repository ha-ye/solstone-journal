# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for speakers owner-facing copy discipline."""

from __future__ import annotations

import re
from pathlib import Path

from solstone.apps.speakers.copy import (
    NEEDS_YOU_RECURRING_MANY,
    NEEDS_YOU_RECURRING_ONE,
    TR_NOT_IN_NEW_VOICES,
    speaker_copy_payload,
    speaker_copy_values,
)

WHO_IS_THIS_COPY = {
    "SPK_SHEET_TITLE": "someone keeps showing up",
    "SPK_SHEET_LEDE_MANY": "sol has heard this voice in {count} conversations, all kept in your journal. tell sol who this is and it will recognize them from now on.",
    "SPK_SHEET_LEDE_ONE": "sol has heard this voice in 1 conversation, kept in your journal. tell sol who this is and it will recognize them from now on.",
    "SPK_SHELF_CANDIDATES": "sol noticed these people around this voice:",
    "SPK_SHELF_NO_EVIDENCE": "sol didn't notice anyone around this voice. search your people:",
    "SPK_EVIDENCE_SCREEN_MANY": "on screen in {count} of these conversations",
    "SPK_EVIDENCE_SCREEN_ONE": "on screen in 1 of these conversations",
    "SPK_EVIDENCE_MEETING_MANY": "in your meeting notes on {count} of those days",
    "SPK_EVIDENCE_MEETING_ONE": "in your meeting notes that day",
    "SPK_SHELF_MENTIONS": "also nearby, from names that come up around this voice:",
    "SPK_ANCHOR": "in your journal",
    "SPK_ANCHOR_HAS_VOICE": "in your journal · sol already knows their voice",
    "SPK_SEARCH_LABEL": "someone else",
    "SPK_SEARCH_PLACEHOLDER": "search your people",
    "SPK_THIS_IS_ME": "this is me",
    "SPK_THIS_IS_ME_GUIDANCE": "sol learns your voice separately. teach it in your voice settings.",
    "SPK_SEARCH_NO_RESULTS": 'no one in your journal matches "{query}"',
    "SPK_CREATE_ROW": 'add "{query}" as someone new',
    "SPK_NEAR_MATCH_BAND": "already in your journal?",
    "SPK_KEEP_SEPARATE_TITLE": "different from {name}?",
    "SPK_KEEP_SEPARATE_BODY": "{name} is already in your journal. if this is someone else, sol will keep them separate and never suggest mixing the two.",
    "SPK_KEEP_SEPARATE_CONFIRM": "yes, someone new",
    "SPK_KEEP_SEPARATE_DECLINE": "no, that's {name}",
    "SPK_PREVIEW_TITLE": "this voice becomes {name}",
    "SPK_PREVIEW_BODY_FRESH": "everywhere sol has heard it, in every conversation, now and from here on.",
    "SPK_PREVIEW_BODY_HAS_VOICE": "{name} already has a voice in your journal. sol will treat this as another way they sound and keep them together.",
    "SPK_PREVIEW_FACTS": "{statements} statements across {conversations} conversations, all in your journal. you can undo this.",
    "SPK_PREVIEW_CONFIRM": "yes, this is {first_name}",
    "SPK_PREVIEW_BACK": "back",
    "SPK_RECEIPT_TITLE": "sol knows {name}'s voice now",
    "SPK_RECEIPT_BODY": "every conversation where this voice was heard now reads {name}.",
    "SPK_RECEIPT_UNDO": "undo",
    "SPK_UNDO_DONE": "undone. this voice is back in new voices.",
    "SPK_UNDO_PARTIAL": "undo restored part of this voice. {restored} changed back; {skipped} stayed as they are. try again.",
    "SPK_EXIT_NOT_PERSON": "not a person",
    "SPK_EXIT_NOT_NOW": "not now",
    "SPK_NOT_PERSON_DONE": "got it, not a person. sol will leave this one alone and will not ask about it again.",
    "SPK_NOT_NOW_DONE": "no rush. it waits quietly in new voices.",
    "SPK_ACTION_WHO_IS_THIS": "who is this?",
    "SPK_LOAD_ERROR": "sol couldn't load this voice. try again.",
    "SPK_SEARCH_ERROR": "sol couldn't search your people. try again.",
    "SPK_CHECK_NAME_ERROR": "sol couldn't check this name. try again.",
    "SPK_SAMPLE_UNAVAILABLE": "this sample isn't available",
    "SPK_ACTION_RETRY": "try again",
}

LOCKED_NEEDS_YOU_COPY = {
    "NEEDS_YOU_RECURRING_MANY": "a voice keeps showing up. sol has heard it in {count} conversations, kept in your journal.",
    "NEEDS_YOU_RECURRING_ONE": "a voice keeps showing up. sol has heard it in 1 conversation, kept in your journal.",
    "TR_NOT_IN_NEW_VOICES": "this voice isn't in new voices yet. sol needs to hear it a few more times.",
}


def _render_surface_paths(root: Path) -> list[Path]:
    return [
        *sorted(root.rglob("*.html")),
        *sorted(root.rglob("*.js")),
    ]


def test_no_literal_copy_in_templates():
    """Templates reference copy constants; Python protocol keys are out of scope.

    Several short copy values are also API keys or status values in Python
    sources. This check intentionally covers the render surfaces where copy is
    owner-visible and avoids treating those protocol tokens as copy literals.
    """

    root = Path("solstone/apps/speakers")
    protocol_literals = {"built", "confirmed", "pause"}

    hits: list[tuple[Path, str]] = []
    for path in _render_surface_paths(root):
        text = path.read_text(encoding="utf-8")
        for value in speaker_copy_values():
            if value in protocol_literals:
                continue
            literal_patterns = (
                re.compile(rf">\s*{re.escape(value)}\s*<"),
                re.compile(rf"(?<!=)['\"`]{re.escape(value)}['\"`]"),
            )
            if any(pattern.search(text) for pattern in literal_patterns):
                hits.append((path, value))

    assert hits == []


def test_all_copy_constants_referenced_by_render_surface():
    html = "\n".join(
        path.read_text(encoding="utf-8")
        for path in _render_surface_paths(Path("solstone/apps/speakers"))
    )

    missing = [name for name in speaker_copy_payload() if name not in html]

    assert missing == []


def test_who_is_this_copy_matches_locked_block():
    payload = speaker_copy_payload()

    actual = {name: payload.get(name) for name in WHO_IS_THIS_COPY}

    assert actual == WHO_IS_THIS_COPY


def test_voice_confirm_copy_matches_locked_bytes():
    assert {
        "NEEDS_YOU_RECURRING_MANY": NEEDS_YOU_RECURRING_MANY,
        "NEEDS_YOU_RECURRING_ONE": NEEDS_YOU_RECURRING_ONE,
        "TR_NOT_IN_NEW_VOICES": TR_NOT_IN_NEW_VOICES,
    } == LOCKED_NEEDS_YOU_COPY


def test_owner_teach_banned_substrings_absent():
    root = Path("solstone/apps/speakers")
    banned = (
        "we're tagging " + "audio segments",
        "to recognize " + "you",
        "solstone " + "needs",
    )
    hits: list[tuple[Path, str]] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".html", ".js"}:
            continue
        text = path.read_text(encoding="utf-8")
        for value in banned:
            if value in text:
                hits.append((path, value))

    assert hits == []
