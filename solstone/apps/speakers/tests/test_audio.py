# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for speakers audio file resolution."""

from __future__ import annotations

from solstone.apps.speakers.audio import (
    _ORDERED_AUDIO_EXTENSIONS,
    audio_serve_url,
    resolve_audio_file,
    resolve_audio_url,
)
from solstone.think.media import AUDIO_EXTENSIONS


def test_ordered_audio_extensions_match_media_registry() -> None:
    assert frozenset(_ORDERED_AUDIO_EXTENSIONS) == AUDIO_EXTENSIONS
    assert _ORDERED_AUDIO_EXTENSIONS[0] == ".flac"


def test_resolve_audio_file_prefers_media_format_order(tmp_path) -> None:
    opus_path = tmp_path / "mic_audio.opus"
    flac_path = tmp_path / "mic_audio.flac"
    opus_path.write_bytes(b"opus")
    flac_path.write_bytes(b"flac")

    assert resolve_audio_file(tmp_path, "mic_audio") == flac_path


def test_resolve_audio_file_ignores_missing_and_unregistered_audio(tmp_path) -> None:
    (tmp_path / "mic_audio.aac").write_bytes(b"aac")

    assert resolve_audio_file(tmp_path, "mic_audio") is None


def test_audio_serve_url_builds_expected_route() -> None:
    assert (
        audio_serve_url(
            "20240101",
            "test",
            "143022_300",
            "mic_audio.m4a",
        )
        == "/app/speakers/api/serve_audio/20240101/test/143022_300/mic_audio.m4a"
    )


def test_resolve_audio_url_uses_registered_audio_file(speakers_env) -> None:
    env = speakers_env()
    env.create_segment(
        "20240101",
        "143022_300",
        ["mic_audio"],
        audio_extension=".m4a",
    )

    assert (
        resolve_audio_url(
            "20240101",
            "test",
            "143022_300",
            "mic_audio",
        )
        == "/app/speakers/api/serve_audio/20240101/test/143022_300/mic_audio.m4a"
    )


def test_resolve_audio_url_returns_none_without_registered_audio(speakers_env) -> None:
    env = speakers_env()
    _flat_dir, chronicle_dir = env._segment_dirs("20240101", "143022_300")
    (chronicle_dir / "mic_audio.aac").write_bytes(b"aac")

    assert resolve_audio_url("20240101", "test", "143022_300", "mic_audio") is None
