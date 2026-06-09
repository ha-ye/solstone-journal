# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from solstone.observe import describe as describe_module


def _video_path(tmp_path: Path) -> Path:
    segment_dir = tmp_path / "chronicle" / "20250101" / "default" / "143022_300"
    segment_dir.mkdir(parents=True)
    video_path = segment_dir / "screen.webm"
    video_path.write_text("video", encoding="utf-8")
    return video_path


def _png_bytes() -> bytes:
    image_bytes = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(image_bytes, format="PNG")
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
    processor.qualified_frames = []
    monkeypatch.setattr(processor, "process", lambda: frames)
    return processor


def _assert_no_describe_temp(directory: Path) -> None:
    names = [path.name for path in directory.iterdir()]
    assert not any(
        name.startswith(".describe_") or name.endswith(".tmp") for name in names
    )


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
    emitted = []
    monkeypatch.setattr(
        describe_module,
        "callosum_send",
        lambda tract, event, **kwargs: emitted.append((tract, event, kwargs)),
    )
    return emitted


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
                request.reason_code = None
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
    header = json.dumps({"raw": video_path.name})
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
    assert output_path.read_text() == json.dumps({"raw": video_path.name}) + "\n"
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
    assert output_path.read_text() == json.dumps({"raw": video_path.name}) + "\n"
    _assert_no_describe_temp(output_path.parent)


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
