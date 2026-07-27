# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for native speakers-analyze helper installation checks."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from pathlib import Path

from solstone.think import probe
from solstone.think.speakers_analyze_handshake import (
    HELPER_BINARY_NAME,
    HELPER_DIST_NAME,
    ROOT_DIST_NAME,
    check_speakers_analyze_handshake,
    runtime_has_speakers_analyze_wheel_coverage,
    speakers_analyze_path_for_executable,
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

    assert runtime_has_speakers_analyze_wheel_coverage(
        platform_reader=lambda: helper_platform,
        platform_tag_reader=lambda: {"helper-tag"},
    )
    assert not runtime_has_speakers_analyze_wheel_coverage(
        platform_reader=lambda: core_platform,
        platform_tag_reader=lambda: {"core-tag"},
    )


def test_helper_path_is_sibling_of_python_executable(tmp_path: Path):
    executable = tmp_path / "venv" / "bin" / "python"

    assert speakers_analyze_path_for_executable(executable) == (
        executable.with_name(HELPER_BINARY_NAME)
    )


def test_handshake_missing_helper_distribution_metadata(tmp_path: Path):
    def version_reader(dist_name: str) -> str:
        if dist_name == ROOT_DIST_NAME:
            return "1.0.18"
        raise PackageNotFoundError(dist_name)

    result = check_speakers_analyze_handshake(
        executable=tmp_path / "bin" / "python",
        version_reader=version_reader,
        platform_reader=lambda: ("linux", "x86_64"),
        platform_tag_reader=lambda: {"manylinux_2_27_x86_64"},
    )

    assert result.status == "missing"
    assert HELPER_DIST_NAME in str(result.message)


def test_handshake_missing_binary_is_distinct_from_missing_metadata(tmp_path: Path):
    def version_reader(_dist_name: str) -> str:
        return "1.0.18"

    result = check_speakers_analyze_handshake(
        executable=tmp_path / "bin" / "python",
        version_reader=version_reader,
        platform_reader=lambda: ("linux", "x86_64"),
        platform_tag_reader=lambda: {"manylinux_2_27_x86_64"},
    )

    assert result.status == "missing"
    assert "missing binary" in str(result.message)


def test_handshake_non_executable_binary_is_distinct(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "python"
    helper = bin_dir / HELPER_BINARY_NAME
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    result = check_speakers_analyze_handshake(
        executable=executable,
        version_reader=lambda _dist_name: "1.0.18",
        platform_reader=lambda: ("linux", "x86_64"),
        platform_tag_reader=lambda: {"manylinux_2_27_x86_64"},
        executable_predicate=lambda _path: False,
    )

    assert result.status == "non-executable"
    assert "not executable" in str(result.message)


def test_handshake_version_mismatch_is_incompatible(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    helper = bin_dir / HELPER_BINARY_NAME
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    def version_reader(dist_name: str) -> str:
        if dist_name == ROOT_DIST_NAME:
            return "1.0.18"
        if dist_name == HELPER_DIST_NAME:
            return "1.0.17"
        raise PackageNotFoundError(dist_name)

    result = check_speakers_analyze_handshake(
        executable=bin_dir / "python",
        version_reader=version_reader,
        platform_reader=lambda: ("linux", "x86_64"),
        platform_tag_reader=lambda: {"manylinux_2_27_x86_64"},
        executable_predicate=lambda _path: True,
    )

    assert result.status == "incompatible"
    assert "metadata is 1.0.17" in str(result.message)


def test_handshake_uncovered_platform_is_incompatible(tmp_path: Path):
    result = check_speakers_analyze_handshake(
        executable=tmp_path / "bin" / "python",
        version_reader=lambda _dist_name: "1.0.18",
        platform_reader=lambda: ("unsupported", "machine"),
        platform_tag_reader=lambda: {"unsupported-tag"},
    )

    assert result.status == "incompatible"
    assert "not covered" in str(result.message)
