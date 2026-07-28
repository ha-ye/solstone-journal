# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.markers import Marker

import solstone.think.probe as probe
from solstone.think.probe import (
    SOLSTONE_CORE_COVERED_PLATFORMS,
    SOLSTONE_CORE_PLATFORM_MARKERS,
    SOLSTONE_CORE_PLATFORM_TAGS,
    SOLSTONE_CORE_SPEAKERS_ANALYZE_COVERED_PLATFORMS,
    SOLSTONE_CORE_SPEAKERS_ANALYZE_PLATFORM_MARKERS,
    SOLSTONE_CORE_SPEAKERS_ANALYZE_PLATFORM_TAGS,
    SOLSTONE_CORE_UNSUPPORTED_PLATFORM_MARKER,
    is_solstone_core_covered_platform,
    solstone_core_speakers_analyze_marker_pins,
)

ROOT = Path(__file__).resolve().parents[1]
MARKER_PLATFORM_RE = re.compile(
    r"sys_platform == '(?P<system>[^']+)' and platform_machine == '(?P<machine>[^']+)'"
)


def _core_pin_markers() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    return [
        dep.split(";", 1)[1].strip()
        for dep in deps
        if dep.startswith("solstone-core==")
    ]


def _unsupported_pin_marker() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    matches = [
        dep.split(";", 1)[1].strip()
        for dep in deps
        if dep.startswith("solstone-core-unsupported-platform==")
    ]
    assert len(matches) == 1
    return matches[0]


def _root_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _leaf_speakers_analyze_pins(package_name: str) -> list[str]:
    data = tomllib.loads(
        (ROOT / "packages" / package_name / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    return sorted(
        dep
        for dep in data["project"]["dependencies"]
        if dep.startswith("solstone-core-speakers-analyze==")
    )


def _marker_platform_tuple(marker_text: str) -> tuple[str, str]:
    match = MARKER_PLATFORM_RE.fullmatch(marker_text)
    assert match is not None
    return (match.group("system"), match.group("machine"))


def test_core_pin_markers_match_probe_covered_platforms() -> None:
    marker_texts = _core_pin_markers()
    assert sorted(marker_texts) == sorted(SOLSTONE_CORE_PLATFORM_MARKERS)
    unsupported_marker_text = _unsupported_pin_marker()
    assert unsupported_marker_text == SOLSTONE_CORE_UNSUPPORTED_PLATFORM_MARKER
    markers = [Marker(text) for text in marker_texts]
    unsupported_marker = Marker(unsupported_marker_text)
    platform_tuples = sorted(
        {
            *SOLSTONE_CORE_COVERED_PLATFORMS,
            *(_marker_platform_tuple(text) for text in marker_texts),
            ("linux", "riscv64"),
            ("darwin", "x86_64"),
        }
    )

    for system, machine in platform_tuples:
        marker_matches = [
            marker.evaluate(
                {
                    "sys_platform": system,
                    "platform_machine": machine,
                }
            )
            for marker in markers
        ]
        assert sum(marker_matches) == int(
            is_solstone_core_covered_platform(system, machine)
        )
        assert unsupported_marker.evaluate(
            {
                "sys_platform": system,
                "platform_machine": machine,
            }
        ) != is_solstone_core_covered_platform(system, machine)


def test_speakers_analyze_platform_tags_are_probe_declared_once() -> None:
    # The covered-platform tuples ARE the declaration, so pinning them is the
    # point of the test. The tags are not pinned as literals: a test that
    # restates the string the code produced would pass whether or not the tag
    # is honest. Assert the properties that would actually catch a defect.
    assert SOLSTONE_CORE_SPEAKERS_ANALYZE_COVERED_PLATFORMS == (
        ("linux", "x86_64"),
        ("linux", "aarch64"),
        ("darwin", "arm64"),
    )

    # The dict cannot drift from the derivation function it is built from.
    assert set(SOLSTONE_CORE_SPEAKERS_ANALYZE_PLATFORM_TAGS) == set(
        SOLSTONE_CORE_SPEAKERS_ANALYZE_COVERED_PLATFORMS
    )
    for platform_tuple, tag in SOLSTONE_CORE_SPEAKERS_ANALYZE_PLATFORM_TAGS.items():
        assert tag == probe._solstone_core_speakers_analyze_platform_tag(platform_tuple)

    # Copying the core's Linux tag is the specific defect this coverage set
    # exists to prevent: the core is static musl with no glibc floor, the
    # helper dynamically links a glibc-only ONNX Runtime.
    for platform_tuple in SOLSTONE_CORE_SPEAKERS_ANALYZE_COVERED_PLATFORMS:
        system, _machine = platform_tuple
        if system != "linux":
            continue
        assert (
            SOLSTONE_CORE_SPEAKERS_ANALYZE_PLATFORM_TAGS[platform_tuple]
            != SOLSTONE_CORE_PLATFORM_TAGS[platform_tuple]
        )

    # The Linux floor must not understate the measured GLIBC_2.27 requirement
    # of the bundled ONNX Runtime library. Parsed, not string-compared, so a
    # future floor rise stays green while a regression fails.
    for platform_tuple, tag in SOLSTONE_CORE_SPEAKERS_ANALYZE_PLATFORM_TAGS.items():
        system, machine = platform_tuple
        if system != "linux":
            continue
        match = re.fullmatch(rf"manylinux_(\d+)_(\d+)_{re.escape(machine)}", tag)
        assert match is not None, tag
        assert (int(match.group(1)), int(match.group(2))) >= (2, 27)

    expected_markers = tuple(
        probe._solstone_core_platform_marker(platform_tuple)
        for platform_tuple in SOLSTONE_CORE_SPEAKERS_ANALYZE_COVERED_PLATFORMS
    )
    assert sorted(SOLSTONE_CORE_SPEAKERS_ANALYZE_PLATFORM_MARKERS) == sorted(
        expected_markers
    )
    assert sorted(
        _marker_platform_tuple(text)
        for text in SOLSTONE_CORE_SPEAKERS_ANALYZE_PLATFORM_MARKERS
    ) == sorted(SOLSTONE_CORE_SPEAKERS_ANALYZE_COVERED_PLATFORMS)
    assert solstone_core_speakers_analyze_marker_pins("9.8.7") == tuple(
        f"solstone-core-speakers-analyze==9.8.7; {marker}"
        for marker in SOLSTONE_CORE_SPEAKERS_ANALYZE_PLATFORM_MARKERS
    )
    assert not hasattr(
        probe, "SOLSTONE_CORE_SPEAKERS_ANALYZE_UNSUPPORTED_PLATFORM_MARKER"
    )


def test_speakers_analyze_leaf_pins_match_probe_covered_platforms() -> None:
    expected = sorted(solstone_core_speakers_analyze_marker_pins(_root_version()))
    assert _leaf_speakers_analyze_pins("solstone-journal") == expected
    assert _leaf_speakers_analyze_pins("solstone-journal-cuda") == expected
