# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import types
from pathlib import Path

import pytest

from solstone.observe import aruco as aruco_module
from solstone.observe import describe as describe_module
from solstone.observe.describe import _winnow_decision

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")


def _assert_metrics_reconcile(metrics: dict) -> None:
    assert metrics["raw"] >= metrics["dhash_qualified"] >= metrics["kept"]
    assert metrics["kept"] == metrics["dhash_qualified"] - metrics["stride_dropped"]
    assert metrics["scene_cut"] <= metrics["kept"]


def _fake_av(monkeypatch, frames: list[object]) -> None:
    class FakeStream:
        def __init__(self):
            self.width = 8
            self.height = 8
            self.thread_type = None
            self.codec_context = types.SimpleNamespace(thread_count=0)

    class FakeContainer:
        def __init__(self):
            self.streams = types.SimpleNamespace(video=[FakeStream()])

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def decode(self, video=0):
            yield from frames

    monkeypatch.setattr(av, "open", lambda _path: FakeContainer())
    monkeypatch.setattr(aruco_module, "detect_markers", lambda _img: None)


class _FakeFrame:
    def __init__(self, pts: int | None, time: float | None, arr):
        self.pts = pts
        self.time = time
        self._arr = arr

    def to_ndarray(self, format="rgb24"):
        return self._arr


def _arr():
    return np.zeros((8, 8, 3), dtype=np.uint8)


def _run(
    monkeypatch,
    tmp_path: Path,
    frame_specs: list[tuple[int | None, float | None]],
    injected_hashes: list[int],
    **config,
):
    # frame_specs: list of (pts, timestamp). injected_hashes: one per non-pts-None frame.
    frames = [_FakeFrame(pts, ts, _arr()) for pts, ts in frame_specs]
    _fake_av(monkeypatch, frames)
    video_path = tmp_path / "screen.webm"
    video_path.write_bytes(b"x")
    monkeypatch.setattr(
        describe_module, "get_config", lambda: {"describe": config} if config else {}
    )
    processor = describe_module.VideoProcessor(video_path)
    hashes = iter(injected_hashes)
    monkeypatch.setattr(processor, "_dhash", lambda _img: next(hashes))
    kept = processor.process()
    return processor, kept


def test_winnow_stride_drop_vs_keep():
    last_kept_hash = 0
    current_hash = (1 << describe_module.VideoProcessor.DHASH_THRESHOLD) - 1

    assert _winnow_decision(
        current_hash,
        describe_module.MIN_STRIDE_SECONDS - 0.1,
        last_kept_hash,
        0.0,
        describe_module.VideoProcessor.DHASH_THRESHOLD,
        describe_module.SCENE_CUT_THRESHOLD,
        describe_module.MIN_STRIDE_SECONDS,
    ) == (False, False, "stride_dropped")

    assert _winnow_decision(
        current_hash,
        describe_module.MIN_STRIDE_SECONDS,
        last_kept_hash,
        0.0,
        describe_module.VideoProcessor.DHASH_THRESHOLD,
        describe_module.SCENE_CUT_THRESHOLD,
        describe_module.MIN_STRIDE_SECONDS,
    ) == (True, False, "kept")


def test_video_processor_uses_config_overrides(monkeypatch, tmp_path):
    monkeypatch.setattr(
        describe_module,
        "get_config",
        lambda: {"describe": {"scene_cut_threshold": 30, "min_stride_seconds": 2.0}},
    )
    processor = describe_module.VideoProcessor(tmp_path / "x.webm")

    assert processor.scene_cut_threshold == 30
    assert processor.min_stride_seconds == 2.0


def test_video_processor_defaults_when_config_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(describe_module, "get_config", lambda: {})
    processor = describe_module.VideoProcessor(tmp_path / "x.webm")

    assert processor.scene_cut_threshold == describe_module.SCENE_CUT_THRESHOLD
    assert processor.min_stride_seconds == describe_module.MIN_STRIDE_SECONDS


def test_process_reference_stays_last_kept_for_stride_drop(monkeypatch, tmp_path):
    processor, kept = _run(
        monkeypatch,
        tmp_path,
        [(1, 0.0), (2, 1.0), (3, 6.0)],
        [0, 0x3FF, 0x3FF],
    )

    assert [frame["frame_id"] for frame in kept] == [1, 3]
    assert processor.winnow_metrics == {
        "raw": 3,
        "dhash_qualified": 3,
        "scene_cut": 0,
        "stride_dropped": 1,
        "kept": 2,
    }
    _assert_metrics_reconcile(processor.winnow_metrics)


def test_process_all_scene_cut_keeps_all_and_bypasses_stride(monkeypatch, tmp_path):
    # Timestamps far under min_stride (0.1s apart); every jump is a scene cut,
    # so the stride floor never fires and every frame is kept.
    processor, kept = _run(
        monkeypatch,
        tmp_path,
        [(1, 0.0), (2, 0.1), (3, 0.2)],
        [0, (1 << describe_module.SCENE_CUT_THRESHOLD) - 1, 0],
    )
    assert [frame["frame_id"] for frame in kept] == [1, 2, 3]
    assert processor.winnow_metrics == {
        "raw": 3,
        "dhash_qualified": 3,
        "scene_cut": 2,
        "stride_dropped": 0,
        "kept": 3,
    }
    _assert_metrics_reconcile(processor.winnow_metrics)


def test_process_quiet_single_frame_keeps_only_first(monkeypatch, tmp_path):
    processor, kept = _run(monkeypatch, tmp_path, [(1, 0.0)], [0])
    assert [frame["frame_id"] for frame in kept] == [1]
    assert processor.winnow_metrics == {
        "raw": 1,
        "dhash_qualified": 1,
        "scene_cut": 0,
        "stride_dropped": 0,
        "kept": 1,
    }
    _assert_metrics_reconcile(processor.winnow_metrics)


def test_process_no_decodable_frames_emits_zeroed_metrics(monkeypatch, tmp_path):
    processor, kept = _run(monkeypatch, tmp_path, [(None, None), (None, None)], [])
    assert kept == []
    assert processor.winnow_metrics == {
        "raw": 2,
        "dhash_qualified": 0,
        "scene_cut": 0,
        "stride_dropped": 0,
        "kept": 0,
    }
    _assert_metrics_reconcile(processor.winnow_metrics)


def test_process_honors_min_stride_override(monkeypatch, tmp_path):
    # Same sequence as the reference test, but min_stride lowered to 0.5 so the
    # 1.0s-later frame is no longer stride-dropped. Result diverges from the
    # default (which keeps [1, 3]); here f2 is kept and advances the reference,
    # making f3 a dHash-gate drop.
    processor, kept = _run(
        monkeypatch,
        tmp_path,
        [(1, 0.0), (2, 1.0), (3, 6.0)],
        [0, 0x3FF, 0x3FF],
        min_stride_seconds=0.5,
    )
    assert [frame["frame_id"] for frame in kept] == [1, 2]
    assert processor.winnow_metrics == {
        "raw": 3,
        "dhash_qualified": 2,
        "scene_cut": 0,
        "stride_dropped": 0,
        "kept": 2,
    }
    _assert_metrics_reconcile(processor.winnow_metrics)
