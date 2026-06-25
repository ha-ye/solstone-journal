# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for media source inference helpers."""

import pytest

from solstone.think.media import canonical_source, canonical_source_signal


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("recording.m4a", "audio"),
        ("recording.mp3", "audio"),
        ("recording.wav", "audio"),
        ("clip.mp4", "audio"),
        ("clip.mov", "audio"),
        ("clip.webm", "audio"),
        ("photo.png", "image"),
        ("photo.jpg", "image"),
        ("photo.heic", "image"),
        ("doc.pdf", "document"),
        ("note.txt", "text"),
        ("note.md", "text"),
        ("", "text"),
    ],
)
def test_canonical_source_from_filename(filename, expected):
    assert canonical_source(filename=filename) == expected


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("audio/mp4", "audio"),
        ("image/png", "image"),
        ("application/pdf", "document"),
        ("text/plain", "text"),
    ],
)
def test_canonical_source_from_content_type(content_type, expected):
    assert canonical_source(content_type=content_type) == expected


def test_canonical_source_prefers_extension_over_content_type():
    assert canonical_source(filename="doc.pdf", content_type="text/plain") == "document"


@pytest.mark.parametrize(
    ("filename", "content_type", "expected"),
    [
        ("recording.m4a", None, "extension"),
        ("photo.png", "text/plain", "extension"),
        (None, "audio/mp4", "content_type"),
        (None, "image/png", "content_type"),
        (None, "application/pdf", "content_type"),
        ("note.txt", "text/plain", "default"),
        (None, "text/plain", "default"),
    ],
)
def test_canonical_source_signal(filename, content_type, expected):
    assert (
        canonical_source_signal(filename=filename, content_type=content_type)
        == expected
    )
