# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from PIL import Image

from solstone.observe import describe as describe_module
from solstone.observe.describe import _winnow_decision


def test_winnow_scene_cut_bypasses_stride():
    last_kept_hash = 0
    current_hash = (1 << describe_module.SCENE_CUT_THRESHOLD) - 1

    assert _winnow_decision(
        current_hash,
        0.1,
        last_kept_hash,
        0.0,
        describe_module.VideoProcessor.DHASH_THRESHOLD,
        describe_module.SCENE_CUT_THRESHOLD,
        describe_module.MIN_STRIDE_SECONDS,
    ) == (True, True, "scene_cut")


def test_winnow_below_threshold():
    last_kept_hash = 0
    current_hash = (1 << (describe_module.VideoProcessor.DHASH_THRESHOLD - 1)) - 1

    assert _winnow_decision(
        current_hash,
        describe_module.MIN_STRIDE_SECONDS,
        last_kept_hash,
        0.0,
        describe_module.VideoProcessor.DHASH_THRESHOLD,
        describe_module.SCENE_CUT_THRESHOLD,
        describe_module.MIN_STRIDE_SECONDS,
    ) == (False, False, "below_threshold")


def test_dhash_identical_images_have_zero_distance():
    processor = describe_module.VideoProcessor.__new__(describe_module.VideoProcessor)
    image = Image.new("RGB", (9, 8))

    assert bin(processor._dhash(image) ^ processor._dhash(image.copy())).count("1") == 0


def test_dhash_reversed_horizontal_ramps_have_full_distance():
    processor = describe_module.VideoProcessor.__new__(describe_module.VideoProcessor)
    ramp = [col * 28 for col in range(9)]
    reversed_ramp = [col * 28 for col in reversed(range(9))]

    image = Image.new("RGB", (9, 8))
    image.putdata([(v, v, v) for _row in range(8) for v in ramp])
    reversed_image = Image.new("RGB", (9, 8))
    reversed_image.putdata([(v, v, v) for _row in range(8) for v in reversed_ramp])

    assert (
        bin(processor._dhash(image) ^ processor._dhash(reversed_image)).count("1") == 64
    )
