# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for the speakers-analyze startup installation invariant."""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from solstone.think import probe
from solstone.think import speakers_analyze_installation as installation


def _version_reader(dist_name: str) -> str:
    if dist_name in {
        installation.ROOT_DIST_NAME,
        installation.HELPER_DIST_NAME,
        installation.MODELS_DIST_NAME,
    }:
        return "1.0.18"
    raise PackageNotFoundError(dist_name)


def _platform_reader() -> probe.CorePlatform:
    return ("linux", "x86_64")


def _platform_tags() -> set[str]:
    return {"manylinux_2_27_x86_64"}


def _helper(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    executable = bin_dir / "python"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    helper = bin_dir / installation.HELPER_BINARY_NAME
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    return executable


def _asset_fixtures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wespeaker = tmp_path / "wespeaker.onnx"
    pyannote = tmp_path / "pyannote.onnx"
    wespeaker.write_bytes(b"wespeaker")
    pyannote.write_bytes(b"pyannote")
    wespeaker_sha256 = installation._sha256_file(wespeaker)
    pyannote_sha256 = installation._sha256_file(pyannote)
    monkeypatch.setattr(
        installation,
        "_required_assets",
        lambda: (
            ("wespeaker", wespeaker, wespeaker_sha256),
            ("pyannote", pyannote, pyannote_sha256),
        ),
    )


def test_coverage_gate_reads_helper_constants_not_core_constants(monkeypatch):
    core_platform = ("coreos", "core64")
    helper_platform = ("helperos", "helper64")
    monkeypatch.setattr(
        probe,
        "SOLSTONE_CORE_COVERED_PLATFORMS",
        (core_platform,),
    )
    monkeypatch.setattr(
        probe,
        "SOLSTONE_CORE_PLATFORM_TAGS",
        {core_platform: "core-tag"},
    )
    monkeypatch.setattr(
        probe,
        "SOLSTONE_CORE_SPEAKERS_ANALYZE_COVERED_PLATFORMS",
        (helper_platform,),
    )
    monkeypatch.setattr(
        probe,
        "SOLSTONE_CORE_SPEAKERS_ANALYZE_PLATFORM_TAGS",
        {helper_platform: "helper-tag"},
    )

    assert installation.runtime_has_speakers_analyze_wheel_coverage(
        platform_reader=lambda: helper_platform,
        platform_tag_reader=lambda: {"helper-tag"},
    )
    assert not installation.runtime_has_speakers_analyze_wheel_coverage(
        platform_reader=lambda: core_platform,
        platform_tag_reader=lambda: {"core-tag"},
    )


def test_helper_path_is_sibling_of_python_executable(tmp_path: Path):
    executable = tmp_path / "venv" / "bin" / "python"

    assert installation.speakers_analyze_path_for_executable(executable) == (
        executable.with_name(installation.HELPER_BINARY_NAME)
    )


def test_missing_helper_distribution_metadata(tmp_path: Path):
    def version_reader(dist_name: str) -> str:
        if dist_name == installation.ROOT_DIST_NAME:
            return "1.0.18"
        raise PackageNotFoundError(dist_name)

    result = installation.check_speakers_analyze_installation(
        executable=tmp_path / "bin" / "python",
        version_reader=version_reader,
        platform_reader=_platform_reader,
        platform_tag_reader=_platform_tags,
    )

    assert result.status == "metadata-missing"
    assert installation.HELPER_DIST_NAME in result.message


def test_missing_binary_is_distinct_from_missing_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _asset_fixtures(tmp_path, monkeypatch)

    result = installation.check_speakers_analyze_installation(
        executable=tmp_path / "bin" / "python",
        version_reader=_version_reader,
        platform_reader=_platform_reader,
        platform_tag_reader=_platform_tags,
    )

    assert result.status == "helper-missing"
    assert installation.HELPER_BINARY_NAME in result.message


def test_non_executable_binary_is_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _asset_fixtures(tmp_path, monkeypatch)
    executable = _helper(tmp_path)
    helper = executable.with_name(installation.HELPER_BINARY_NAME)
    helper.chmod(0o644)

    result = installation.check_speakers_analyze_installation(
        executable=executable,
        version_reader=_version_reader,
        platform_reader=_platform_reader,
        platform_tag_reader=_platform_tags,
        executable_predicate=lambda _path: False,
    )

    assert result.status == "helper-not-executable"


def test_version_mismatch_is_incompatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _asset_fixtures(tmp_path, monkeypatch)
    executable = _helper(tmp_path)

    def version_reader(dist_name: str) -> str:
        if dist_name == installation.ROOT_DIST_NAME:
            return "1.0.18"
        if dist_name == installation.HELPER_DIST_NAME:
            return "1.0.17"
        if dist_name == installation.MODELS_DIST_NAME:
            return "1.0.18"
        raise PackageNotFoundError(dist_name)

    result = installation.check_speakers_analyze_installation(
        executable=executable,
        version_reader=version_reader,
        platform_reader=_platform_reader,
        platform_tag_reader=_platform_tags,
        executable_predicate=lambda _path: True,
    )

    assert result.status == "metadata-version-mismatch"
    assert "1.0.17" in result.message


def test_uncovered_platform_is_unsupported(tmp_path: Path):
    result = installation.check_speakers_analyze_installation(
        executable=tmp_path / "bin" / "python",
        version_reader=_version_reader,
        platform_reader=lambda: ("unsupported", "machine"),
        platform_tag_reader=lambda: {"unsupported-tag"},
    )

    assert result.status == "platform-unsupported"


def test_asset_digest_mismatch_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = _helper(tmp_path)
    wespeaker = tmp_path / "wespeaker.onnx"
    pyannote = tmp_path / "pyannote.onnx"
    wespeaker.write_bytes(b"wespeaker")
    pyannote.write_bytes(b"pyannote")
    monkeypatch.setattr(
        installation,
        "_required_assets",
        lambda: (
            ("wespeaker", wespeaker, "0" * 64),
            ("pyannote", pyannote, installation._sha256_file(pyannote)),
        ),
    )

    result = installation.check_speakers_analyze_installation(
        executable=executable,
        version_reader=_version_reader,
        platform_reader=_platform_reader,
        platform_tag_reader=_platform_tags,
    )

    assert result.status == "asset-digest-mismatch"


def test_live_generation_record_reuses_digest_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = _helper(tmp_path)
    _asset_fixtures(tmp_path, monkeypatch)
    generation = installation.begin_speakers_analyze_generation(
        journal_path=tmp_path,
        executable=executable,
        version_reader=_version_reader,
        platform_reader=_platform_reader,
    )
    calls = 0

    def fail_digest(_path: Path) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("digest should be reused while generation lease is live")

    monkeypatch.setattr(installation, "_sha256_file", fail_digest)

    try:
        result = installation.check_speakers_analyze_installation(
            journal_path=tmp_path,
            executable=executable,
            version_reader=_version_reader,
            platform_reader=_platform_reader,
            platform_tag_reader=_platform_tags,
            generation_id=generation.generation_id,
        )
    finally:
        generation.release()
        os.environ.pop(installation.GENERATION_ENV_KEY, None)

    assert result.status == "ok"
    assert calls == 0


def test_stale_generation_record_degrades_to_full_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable = _helper(tmp_path)
    _asset_fixtures(tmp_path, monkeypatch)
    generation = installation.begin_speakers_analyze_generation(
        journal_path=tmp_path,
        executable=executable,
        version_reader=_version_reader,
        platform_reader=_platform_reader,
    )
    generation_id = generation.generation_id
    generation.release()
    os.environ.pop(installation.GENERATION_ENV_KEY, None)
    calls = 0
    original_digest = installation._sha256_file

    def counted_digest(path: Path) -> str:
        nonlocal calls
        calls += 1
        return original_digest(path)

    monkeypatch.setattr(installation, "_sha256_file", counted_digest)

    result = installation.check_speakers_analyze_installation(
        journal_path=tmp_path,
        executable=executable,
        version_reader=_version_reader,
        platform_reader=_platform_reader,
        platform_tag_reader=_platform_tags,
        generation_id=generation_id,
    )

    assert result.status == "ok"
    assert calls == 2
