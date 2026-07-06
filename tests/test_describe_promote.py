# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from solstone.observe import describe as describe_module
from solstone.observe import detect as detect_module
from solstone.observe import processing_record as processing_record_module
from solstone.think.providers.rfdetr_install import RfdetrPaths


def _video_path(tmp_path: Path) -> Path:
    segment_dir = tmp_path / "chronicle" / "20250101" / "default" / "143022_300"
    segment_dir.mkdir(parents=True)
    video_path = segment_dir / "screen.webm"
    video_path.write_text("video", encoding="utf-8")
    return video_path


def _png_bytes(size: tuple[int, int] = (8, 8)) -> bytes:
    image_bytes = io.BytesIO()
    Image.new("RGB", size, "white").save(image_bytes, format="PNG")
    return image_bytes.getvalue()


def _frame(frame_id: int, timestamp: float, frame_bytes: bytes) -> dict:
    return {
        "frame_id": frame_id,
        "timestamp": timestamp,
        "frame_bytes": frame_bytes,
        "aruco": None,
    }


def _processor(video_path: Path, frames: list[dict], monkeypatch) -> object:
    processor = describe_module.VideoProcessor.__new__(describe_module.VideoProcessor)
    processor.video_path = video_path
    processor.first_hash = None
    processor.last_hash = None
    processor.qualified_count = len(frames)
    processor.qualified_frames = []
    monkeypatch.setattr(processor, "process", lambda: frames)
    return processor


def _assert_no_describe_temp(directory: Path) -> None:
    names = [path.name for path in directory.iterdir()]
    assert not any(
        name.startswith(".describe_") or name.endswith(".tmp") for name in names
    )


def _jsonl_rows(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _canned_detection() -> dict:
    return {
        "image": {"width": 8, "height": 8},
        "detections": [
            {
                "class_id": 42,
                "class_name": "cup",
                "score": 0.7,
                "bbox": [1, 2, 3, 4],
            }
        ],
    }


def _install_fakes(monkeypatch, outcomes: dict[int, dict]) -> list[tuple]:
    from solstone.think import batch as batch_module
    from solstone.think import models

    FakeBatch.instances = []
    FakeBatch.outcomes = outcomes
    monkeypatch.delenv("OBSERVER_NAME", raising=False)
    monkeypatch.delenv("SEGMENT_META", raising=False)
    monkeypatch.setattr(batch_module, "Batch", FakeBatch)
    monkeypatch.setattr(
        models,
        "resolve_provider",
        lambda _context, _interface: ("google", "gemini-test"),
    )
    monkeypatch.setattr(
        processing_record_module, "now_iso_utc", lambda: "2026-06-30T12:00:00Z"
    )
    emitted = []
    monkeypatch.setattr(
        describe_module,
        "callosum_send",
        lambda tract, event, **kwargs: emitted.append((tract, event, kwargs)),
    )
    return emitted


def test_build_metadata_header_includes_static_single_frame_hash(tmp_path, monkeypatch):
    video_path = _video_path(tmp_path)
    processor = describe_module.VideoProcessor.__new__(describe_module.VideoProcessor)
    processor.video_path = video_path
    processor.first_hash = 0x1234
    processor.last_hash = 0x1234
    processor.qualified_count = 1
    monkeypatch.setenv("OBSERVER_NAME", "desk")
    monkeypatch.setenv("SEGMENT_META", json.dumps({"stream": "default"}))

    assert processor._build_metadata_header() == {
        "raw": video_path.name,
        "observer": "desk",
        "stream": "default",
        "first_hash": "0000000000001234",
        "last_hash": "0000000000001234",
        "qualified_count": 1,
    }


def test_describe_header_raw_is_producer_invariant(tmp_path, monkeypatch):
    video_path = _video_path(tmp_path)
    processor = describe_module.VideoProcessor.__new__(describe_module.VideoProcessor)
    processor.video_path = video_path
    processor.first_hash = None
    processor.last_hash = None
    processor.qualified_count = 1
    monkeypatch.delenv("OBSERVER_NAME", raising=False)
    monkeypatch.delenv("SEGMENT_META", raising=False)

    header = processor._build_metadata_header()

    # raw is the producer's invariant (relaxed from the shared floor), so the
    # describer must keep emitting it.
    assert "raw" in header
    assert header["raw"] == video_path.name


class FakeBatch:
    instances = []
    outcomes = {}

    def __init__(self, max_concurrent=5, client=None):
        self.max_concurrent = max_concurrent
        self.pending_tasks = set()
        self.queue = []
        self.add_count = 0
        FakeBatch.instances.append(self)

    def create(self, **kwargs):
        return SimpleNamespace(
            **kwargs,
            response=None,
            error=None,
            duration=0.01,
            model_used=kwargs.get("model") or "",
            provider=None,
            reason_code=None,
            reset_at_ms=None,
        )

    def add(self, request):
        self.add_count += 1
        self.queue.append(request)

    def update(self, request, **kwargs):
        for key, value in kwargs.items():
            setattr(request, key, value)
        request.error = None
        request.reason_code = None
        self.add(request)

    async def drain_batch(self):
        pending = self.queue
        self.queue = []
        for request in pending:
            outcome = FakeBatch.outcomes.get(request.frame_id, {})
            if outcome.get("fail"):
                request.error = outcome.get("error", "boom")
                request.reason_code = outcome.get("reason_code")
                request.retry_count = 4
            else:
                request.error = None
                request.response = outcome.get(
                    "response",
                    json.dumps(
                        {"primary": "code", "secondary": "none", "overlap": True}
                    ),
                )
            yield request


@pytest.mark.asyncio
async def test_success_with_mixed_results_promotes_byte_identical_jsonl(
    tmp_path, monkeypatch
):
    video_path = _video_path(tmp_path)
    output_path = video_path.with_suffix(".jsonl")
    frame_bytes = _png_bytes()
    processor = _processor(
        video_path,
        [
            _frame(1, 0.0, frame_bytes),
            _frame(2, 1.25, frame_bytes),
        ],
        monkeypatch,
    )
    _install_fakes(
        monkeypatch,
        {
            1: {},
            2: {"fail": True, "error": "boom"},
        },
    )
    monkeypatch.setattr(
        describe_module,
        "select_frames_for_extraction",
        lambda *_args, **_kwargs: [],
    )

    await processor.process_with_vision(
        max_concurrent=1,
        output_path=output_path,
        work_key="20250101/143022_300/screen",
    )

    request_type = describe_module.RequestType.DESCRIBE.value
    header = json.dumps(
        {
            "raw": video_path.name,
            "first_hash": None,
            "last_hash": None,
            "qualified_count": 2,
            "_solstone_processing": {
                "schema": "solstone.processing.v1",
                "state": "analyzed",
                "reason_code": "ok",
                "handler": "describe",
                "attempted_at": "2026-06-30T12:00:00Z",
                "input_size": 5,
            },
        }
    )
    frame1 = json.dumps(
        {
            "frame_id": 1,
            "timestamp": 0.0,
            "requests": [
                {"type": request_type, "model": "gemini-test", "duration": 0.01}
            ],
            "analysis": {"primary": "code", "secondary": "none", "overlap": True},
            "enhanced": False,
        }
    )
    frame2 = json.dumps(
        {
            "frame_id": 2,
            "timestamp": 1.25,
            "requests": [
                {
                    "type": request_type,
                    "model": "gemini-test",
                    "duration": 0.01,
                    "retries": 4,
                }
            ],
            "error": "boom",
            "enhanced": False,
        }
    )
    expected = "".join(line + "\n" for line in [header, frame1, frame2])

    assert output_path.read_text() == expected
    assert output_path.name in [path.name for path in output_path.parent.iterdir()]
    _assert_no_describe_temp(output_path.parent)


@pytest.mark.asyncio
async def test_detection_blocks_attach_to_media_and_social_frames(
    tmp_path, monkeypatch
):
    video_path = _video_path(tmp_path)
    output_path = video_path.with_suffix(".jsonl")
    frame_bytes = _png_bytes()
    processor = _processor(
        video_path,
        [
            _frame(1, 0.0, frame_bytes),
            _frame(2, 1.25, frame_bytes),
        ],
        monkeypatch,
    )
    canned = _canned_detection()
    calls = []
    _install_fakes(
        monkeypatch,
        {
            1: {
                "response": json.dumps(
                    {"primary": "media", "secondary": "none", "overlap": True}
                )
            },
            2: {
                "response": json.dumps(
                    {"primary": "code", "secondary": "social", "overlap": True}
                )
            },
        },
    )
    monkeypatch.setattr(
        describe_module,
        "select_frames_for_extraction",
        lambda *_args, **_kwargs: [],
    )

    def fake_detect(image_bytes):
        calls.append(image_bytes)
        return canned

    monkeypatch.setattr(describe_module, "detect_objects", fake_detect)

    await processor.process_with_vision(
        max_concurrent=1,
        output_path=output_path,
        work_key="20250101/143022_300/screen",
    )

    frame1, frame2 = _jsonl_rows(output_path)[1:]
    assert frame1["detections"] == {
        "engine": "rf-detr.cpp",
        "engine_ref": "65c0ffcc",
        "model": "rfdetr-nano-f16",
        "threshold": 0.25,
        "source": "screen",
        "gate": "primary:media",
        "image": canned["image"],
        "objects": canned["detections"],
    }
    assert frame2["detections"] == {
        "engine": "rf-detr.cpp",
        "engine_ref": "65c0ffcc",
        "model": "rfdetr-nano-f16",
        "threshold": 0.25,
        "source": "screen",
        "gate": "secondary:social",
        "image": canned["image"],
        "objects": canned["detections"],
    }
    assert calls == [frame_bytes, frame_bytes]


@pytest.mark.asyncio
async def test_detection_gate_off_never_invokes_detector(tmp_path, monkeypatch):
    video_path = _video_path(tmp_path)
    output_path = video_path.with_suffix(".jsonl")
    frame_bytes = _png_bytes()
    processor = _processor(
        video_path,
        [
            _frame(1, 0.0, frame_bytes),
            _frame(2, 1.25, frame_bytes),
            _frame(3, 2.5, frame_bytes),
        ],
        monkeypatch,
    )
    calls = 0
    _install_fakes(
        monkeypatch,
        {
            1: {
                "response": json.dumps(
                    {"primary": "code", "secondary": "none", "overlap": True}
                )
            },
            2: {
                "response": json.dumps(
                    {"primary": "terminal", "secondary": "none", "overlap": True}
                )
            },
            3: {
                "response": json.dumps(
                    {"primary": "browsing", "secondary": "none", "overlap": True}
                )
            },
        },
    )
    monkeypatch.setattr(
        describe_module,
        "select_frames_for_extraction",
        lambda *_args, **_kwargs: [],
    )

    def fake_detect(_image_bytes):
        nonlocal calls
        calls += 1
        return _canned_detection()

    monkeypatch.setattr(describe_module, "detect_objects", fake_detect)

    await processor.process_with_vision(
        max_concurrent=1,
        output_path=output_path,
        work_key="20250101/143022_300/screen",
    )

    rows = _jsonl_rows(output_path)[1:]
    assert all("detections" not in row for row in rows)
    assert calls == 0


@pytest.mark.asyncio
async def test_detection_skips_categorization_failed_frame(tmp_path, monkeypatch):
    video_path = _video_path(tmp_path)
    output_path = video_path.with_suffix(".jsonl")
    gated_frame_bytes = _png_bytes((8, 8))
    failed_frame_bytes = _png_bytes((10, 10))
    processor = _processor(
        video_path,
        [
            _frame(1, 0.0, gated_frame_bytes),
            _frame(2, 1.25, failed_frame_bytes),
        ],
        monkeypatch,
    )
    calls = []
    _install_fakes(
        monkeypatch,
        {
            1: {
                "response": json.dumps(
                    {"primary": "media", "secondary": "none", "overlap": True}
                )
            },
            2: {"fail": True, "error": "boom"},
        },
    )
    monkeypatch.setattr(
        describe_module,
        "select_frames_for_extraction",
        lambda *_args, **_kwargs: [],
    )

    def fake_detect(image_bytes):
        calls.append(image_bytes)
        return _canned_detection()

    monkeypatch.setattr(describe_module, "detect_objects", fake_detect)

    await processor.process_with_vision(
        max_concurrent=1,
        output_path=output_path,
        work_key="20250101/143022_300/screen",
    )

    frame1, frame2 = _jsonl_rows(output_path)[1:]
    assert "detections" in frame1
    assert "detections" not in frame2
    assert calls == [gated_frame_bytes]


@pytest.mark.asyncio
async def test_detection_provider_absence_latches_across_gated_frames(
    tmp_path, monkeypatch, caplog
):
    video_path = _video_path(tmp_path)
    output_path = video_path.with_suffix(".jsonl")
    frame_bytes = _png_bytes()
    processor = _processor(
        video_path,
        [
            _frame(1, 0.0, frame_bytes),
            _frame(2, 1.25, frame_bytes),
            _frame(3, 2.5, frame_bytes),
        ],
        monkeypatch,
    )
    calls = 0
    monkeypatch.setattr(detect_module, "_disabled", False)
    monkeypatch.setattr(describe_module, "detect_objects", detect_module.detect_objects)

    def fake_paths():
        nonlocal calls
        calls += 1
        return RfdetrPaths(status="not_installed")

    monkeypatch.setattr(detect_module, "rfdetr_paths", fake_paths)
    caplog.set_level(logging.WARNING, logger=detect_module.LOG.name)
    _install_fakes(
        monkeypatch,
        {
            1: {
                "response": json.dumps(
                    {"primary": "media", "secondary": "none", "overlap": True}
                )
            },
            2: {
                "response": json.dumps(
                    {"primary": "social", "secondary": "none", "overlap": True}
                )
            },
            3: {
                "response": json.dumps(
                    {"primary": "code", "secondary": "social", "overlap": True}
                )
            },
        },
    )
    monkeypatch.setattr(
        describe_module,
        "select_frames_for_extraction",
        lambda *_args, **_kwargs: [],
    )

    await processor.process_with_vision(
        max_concurrent=1,
        output_path=output_path,
        work_key="20250101/143022_300/screen",
    )

    rows = _jsonl_rows(output_path)[1:]
    warnings = [
        record
        for record in caplog.records
        if record.name == detect_module.LOG.name and record.levelno == logging.WARNING
    ]
    assert all("detections" not in row for row in rows)
    assert len(warnings) == 1
    assert warnings[0].getMessage() == (
        "object detection disabled: rf-detr provider not_installed"
    )
    assert calls == 1


@pytest.mark.asyncio
async def test_detection_empty_result_stores_empty_objects(tmp_path, monkeypatch):
    video_path = _video_path(tmp_path)
    output_path = video_path.with_suffix(".jsonl")
    frame_bytes = _png_bytes()
    processor = _processor(
        video_path,
        [_frame(1, 0.0, frame_bytes)],
        monkeypatch,
    )
    _install_fakes(
        monkeypatch,
        {
            1: {
                "response": json.dumps(
                    {"primary": "media", "secondary": "none", "overlap": True}
                )
            },
        },
    )
    monkeypatch.setattr(
        describe_module,
        "select_frames_for_extraction",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        describe_module,
        "detect_objects",
        lambda _image_bytes: {"image": {"width": 8, "height": 8}, "detections": []},
    )

    await processor.process_with_vision(
        max_concurrent=1,
        output_path=output_path,
        work_key="20250101/143022_300/screen",
    )

    frame = _jsonl_rows(output_path)[1]
    assert frame["detections"]["objects"] == []


@pytest.mark.asyncio
async def test_detection_uses_full_resolution_frame_bytes(tmp_path, monkeypatch):
    video_path = _video_path(tmp_path)
    output_path = video_path.with_suffix(".jsonl")
    frame_bytes = _png_bytes((2100, 20))
    processor = _processor(
        video_path,
        [_frame(1, 0.0, frame_bytes)],
        monkeypatch,
    )
    observed_sizes = []
    _install_fakes(
        monkeypatch,
        {
            1: {
                "response": json.dumps(
                    {"primary": "media", "secondary": "none", "overlap": True}
                )
            },
        },
    )
    monkeypatch.setattr(
        describe_module,
        "select_frames_for_extraction",
        lambda *_args, **_kwargs: [],
    )

    def fake_detect(image_bytes):
        with Image.open(io.BytesIO(image_bytes)) as img:
            observed_sizes.append(img.size)
        return _canned_detection()

    monkeypatch.setattr(describe_module, "detect_objects", fake_detect)

    await processor.process_with_vision(
        max_concurrent=1,
        output_path=output_path,
        work_key="20250101/143022_300/screen",
    )

    assert observed_sizes == [(2100, 20)]
    assert observed_sizes[0][0] > 1920


@pytest.mark.asyncio
async def test_empty_run_promotes_header_only_file_for_event_precondition(
    tmp_path, monkeypatch
):
    video_path = _video_path(tmp_path)
    output_path = video_path.with_suffix(".jsonl")
    processor = _processor(video_path, [], monkeypatch)
    _install_fakes(monkeypatch, {})
    monkeypatch.setattr(
        describe_module,
        "select_frames_for_extraction",
        lambda *_args, **_kwargs: [],
    )

    await processor.process_with_vision(
        max_concurrent=1,
        output_path=output_path,
        work_key="20250101/143022_300/screen",
    )

    assert output_path.exists()
    # async_main's completion event branch is unchanged and gated on this exists().
    assert output_path.read_text() == (
        json.dumps(
            {
                "raw": video_path.name,
                "first_hash": None,
                "last_hash": None,
                "qualified_count": 0,
                "_solstone_processing": {
                    "schema": "solstone.processing.v1",
                    "state": "empty",
                    "reason_code": "no_decodable_frames",
                    "handler": "describe",
                    "attempted_at": "2026-06-30T12:00:00Z",
                    "input_size": 5,
                },
            }
        )
        + "\n"
    )
    _assert_no_describe_temp(output_path.parent)


@pytest.mark.asyncio
async def test_all_frames_failed_promotes_header_only_then_raises(
    tmp_path, monkeypatch
):
    video_path = _video_path(tmp_path)
    output_path = video_path.with_suffix(".jsonl")
    processor = _processor(video_path, [_frame(1, 0.0, _png_bytes())], monkeypatch)
    _install_fakes(monkeypatch, {1: {"fail": True, "error": "boom"}})

    with pytest.raises(RuntimeError):
        await processor.process_with_vision(
            max_concurrent=1,
            output_path=output_path,
            work_key="20250101/143022_300/screen",
        )

    assert output_path.exists()
    assert output_path.read_text() == (
        json.dumps(
            {
                "raw": video_path.name,
                "first_hash": None,
                "last_hash": None,
                "qualified_count": 1,
                "_solstone_processing": {
                    "schema": "solstone.processing.v1",
                    "state": "failed",
                    "reason_code": "analysis_failed",
                    "handler": "describe",
                    "attempted_at": "2026-06-30T12:00:00Z",
                    "input_size": 5,
                },
            }
        )
        + "\n"
    )
    _assert_no_describe_temp(output_path.parent)


@pytest.mark.asyncio
async def test_all_frames_failed_output_is_terminal_until_redo(tmp_path, monkeypatch):
    video_path = _video_path(tmp_path)
    output_path = video_path.with_suffix(".jsonl")
    processor = _processor(video_path, [_frame(1, 0.0, _png_bytes())], monkeypatch)
    _install_fakes(monkeypatch, {1: {"fail": True, "error": "boom"}})

    with pytest.raises(RuntimeError):
        await processor.process_with_vision(
            max_concurrent=1,
            output_path=output_path,
            work_key="20250101/143022_300/screen",
        )

    original = output_path.read_bytes()
    constructed = []

    def fail_if_constructed(*args, **kwargs):
        constructed.append((args, kwargs))
        raise AssertionError("VideoProcessor should not reprocess existing output")

    monkeypatch.setattr(describe_module, "VideoProcessor", fail_if_constructed)
    monkeypatch.setattr(describe_module, "require_solstone", lambda: None)
    monkeypatch.setattr("sys.argv", ["journal describe", str(video_path)])

    await describe_module.async_main()

    assert constructed == []
    assert output_path.read_bytes() == original


@pytest.mark.asyncio
async def test_unexpected_mid_job_exception_removes_temp_without_promoting(
    tmp_path, monkeypatch
):
    video_path = _video_path(tmp_path)
    output_path = video_path.with_suffix(".jsonl")
    processor = _processor(video_path, [_frame(1, 0.0, _png_bytes())], monkeypatch)
    _install_fakes(monkeypatch, {1: {}})

    def raise_inject(*_args, **_kwargs):
        raise ValueError("inject")

    monkeypatch.setattr(
        describe_module,
        "select_frames_for_extraction",
        raise_inject,
    )

    with pytest.raises(ValueError):
        await processor.process_with_vision(
            max_concurrent=1,
            output_path=output_path,
            work_key="20250101/143022_300/screen",
        )

    assert not output_path.exists()
    _assert_no_describe_temp(output_path.parent)


@pytest.mark.asyncio
async def test_provider_blocked_promotes_nothing_and_records_nothing(
    tmp_path, monkeypatch
):
    import solstone.convey.provider_readiness as provider_readiness
    from solstone.observe.exit_codes import EXIT_PROVIDER_BLOCKED

    video_path = _video_path(tmp_path)
    output_path = video_path.with_suffix(".jsonl")
    processor = _processor(video_path, [_frame(1, 0.0, _png_bytes())], monkeypatch)
    _install_fakes(
        monkeypatch,
        {1: {"fail": True, "error": "blocked", "reason_code": "rate_limited"}},
    )
    monkeypatch.setattr(provider_readiness, "is_blocking_reason", lambda _code: True)
    monkeypatch.setattr(provider_readiness, "present_for_reason", lambda *a, **k: {})
    monkeypatch.setattr(
        describe_module, "_emit_blocked_notification", lambda _view: None
    )

    with pytest.raises(SystemExit) as exc:
        await processor.process_with_vision(
            max_concurrent=1,
            output_path=output_path,
            work_key="20250101/143022_300/screen",
        )

    assert exc.value.code == EXIT_PROVIDER_BLOCKED
    assert not output_path.exists()
    _assert_no_describe_temp(output_path.parent)


@pytest.mark.asyncio
async def test_corrupt_input_records_failed_distinct_from_empty(tmp_path, monkeypatch):
    pytest.importorskip("av")

    seg = tmp_path / "chronicle" / "20250101" / "default" / "143022_300"
    seg.mkdir(parents=True)
    bad = seg / "screen.mp4"
    bad.write_bytes(b"not a real mp4 file at all")
    output_path = bad.with_suffix(".jsonl")
    processor = describe_module.VideoProcessor(bad)
    _install_fakes(monkeypatch, {})
    monkeypatch.setattr(
        describe_module,
        "select_frames_for_extraction",
        lambda *a, **k: [],
    )

    await processor.process_with_vision(
        max_concurrent=1,
        output_path=output_path,
        work_key="20250101/143022_300/screen",
    )

    corrupt_meta = json.loads(output_path.read_text().splitlines()[0])[
        "_solstone_processing"
    ]
    assert (corrupt_meta["state"], corrupt_meta["reason_code"]) == (
        "failed",
        "corrupt_input",
    )

    empty_video = _video_path(tmp_path / "empty")
    empty_output_path = empty_video.with_suffix(".jsonl")
    empty_processor = _processor(empty_video, [], monkeypatch)

    await empty_processor.process_with_vision(
        max_concurrent=1,
        output_path=empty_output_path,
        work_key="20250101/143022_300/screen",
    )

    empty_meta = json.loads(empty_output_path.read_text().splitlines()[0])[
        "_solstone_processing"
    ]
    assert (empty_meta["state"], empty_meta["reason_code"]) == (
        "empty",
        "no_decodable_frames",
    )
    assert (corrupt_meta["state"], corrupt_meta["reason_code"]) != (
        empty_meta["state"],
        empty_meta["reason_code"],
    )
