# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import shutil
import subprocess

import pytest

import scripts.check_release_preflight as preflight
import scripts.release_tool_pins as pins

pytestmark = [pytest.mark.integration, pytest.mark.release]


def test_real_uv_banner_parses_and_compares_strictly() -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")

    result = subprocess.run(
        [uv, "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    banner = result.stdout.strip() or result.stderr.strip()
    version = pins.parse_host_variant_tool_banner("uv", banner)

    assert version is not None
    assert pins.check_host_variant_tool_pin("uv", f"uv {version}", banner)
    assert not pins.check_host_variant_tool_pin("uv", f"uv {version}.mismatch", banner)
    if version == pins.UV_VERSION:
        evidence = pins.fixture_lane_tool_evidence("source")
        evidence["uv"] = banner
        uv_failures = [
            failure
            for failure in preflight.check_lane_tool_evidence("source", evidence)
            if failure.error == "release lane tool uv is not pinned"
        ]
        assert uv_failures == []
