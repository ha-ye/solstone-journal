# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import verify_browser as vb  # noqa: E402


def _png(color: tuple[int, int, int, int], size: tuple[int, int] = (4, 4)) -> bytes:
    image = Image.new("RGBA", size, color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_parse_remote_debugging_port_from_arg_list() -> None:
    assert (
        vb.parse_remote_debugging_port(
            ["chrome", "--headless", "--remote-debugging-port=9869"]
        )
        == 9869
    )


def test_parse_remote_debugging_port_from_proc_cmdline() -> None:
    cmdline = "chrome\0--remote-debugging-port=19871\0--other-flag\0"
    assert vb.parse_remote_debugging_port(cmdline) == 19871


def test_parse_remote_debugging_port_rejects_absent_or_invalid_values() -> None:
    assert vb.parse_remote_debugging_port(["chrome"]) is None
    assert vb.parse_remote_debugging_port(["--remote-debugging-port=0"]) is None
    assert vb.parse_remote_debugging_port(["--remote-debugging-port=99999"]) is None
    assert vb.parse_remote_debugging_port(["--remote-debugging-port=abc"]) is None


def test_build_device_metrics_payload() -> None:
    assert vb.build_device_metrics_payload(1265, 500) == {
        "width": 1265,
        "height": 500,
        "deviceScaleFactor": 1,
        "mobile": False,
    }


def test_baseline_path_uses_jpg_for_human_review_and_png_for_diff() -> None:
    assert vb.baseline_path({"app": "transcripts", "name": "smoke"}) == Path(
        "tests/baselines/visual/transcripts/smoke.jpg"
    )
    assert vb.baseline_path(
        {"app": "transcripts", "name": "day-short", "diff": True}
    ) == Path("tests/baselines/visual/transcripts/day-short.png")


def test_select_scenarios_rejects_unknown_filter() -> None:
    try:
        vb._select_scenarios(["transcripts/missing"])
    except ValueError as exc:
        assert "unknown browser scenario filter" in str(exc)
        assert "transcripts/day-short" in str(exc)
    else:
        raise AssertionError("expected unknown scenario filter to fail")


def test_compare_png_passes_identical_images() -> None:
    png = _png((255, 255, 255, 255))
    result = vb.compare_png(
        png,
        png,
        channel_delta_threshold=0,
        changed_pixels_pct_threshold=0.0,
    )
    assert result.passed
    assert result.changed_pixels == 0
    assert result.max_channel_delta == 0
    assert result.diff_image_bytes


def test_compare_png_fails_dimension_mismatch() -> None:
    actual = _png((255, 255, 255, 255), size=(4, 4))
    baseline = _png((255, 255, 255, 255), size=(3, 4))
    result = vb.compare_png(actual, baseline)
    assert not result.passed
    assert "dimension mismatch" in result.message
    assert result.diff_image_bytes


def test_compare_png_fails_over_tolerance_and_emits_diff() -> None:
    actual = _png((255, 0, 0, 255))
    baseline = _png((255, 255, 255, 255))
    result = vb.compare_png(
        actual,
        baseline,
        channel_delta_threshold=1,
        changed_pixels_pct_threshold=0.0,
    )
    assert not result.passed
    assert result.changed_pixels == 16
    assert result.max_channel_delta == 255
    assert result.diff_image_bytes.startswith(b"\x89PNG")


def test_visual_artifact_paths() -> None:
    actual, diff = vb.visual_artifact_paths(
        Path("tests/baselines/visual/transcripts/day-short.png")
    )
    assert actual == Path("tests/baselines/visual/transcripts/day-short.actual.png")
    assert diff == Path("tests/baselines/visual/transcripts/day-short.diff.png")
