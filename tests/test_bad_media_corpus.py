# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Offline fuzz corpus for structurally bad media.

These tests drive real ingest, handler, state, completion, and idle-gate paths
on synthesized pathological media, asserting terminal classes without server,
sandbox, network, or model calls. AC10 is enforced as a whole-suite property.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import io
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from solstone.observe.processing_record import (
    HANDLER_DESCRIBE,
    HANDLER_TRANSCRIBE,
    REASON_ANALYSIS_FAILED,
    REASON_CORRUPT_INPUT,
    REASON_NO_DECODABLE_AUDIO,
    REASON_NO_DECODABLE_FRAMES,
    STATE_ANALYZED,
    STATE_EMPTY,
    STATE_FAILED,
)
from solstone.observe.utils import SAMPLE_RATE
from solstone.think.cluster import (
    cluster_segments,
    read_segment_data_state,
)
from solstone.think.data_state import DataState
from solstone.think.pipeline_health import (
    classify_segment_completion,
    read_segment_progress,
)

DAY = "20990501"
STREAM = "default"
SEGMENT = "120000_300"
FIXED_NOW = "2026-06-30T12:00:00Z"


@pytest.fixture
def observer_env(tmp_path, monkeypatch):
    """Temp journal + Flask test client factory.

    Self-contained mirror of the observer app-test fixture. Defined inline
    rather than reused via ``pytest_plugins`` pointing at
    ``solstone/apps/observer/tests/conftest.py``: that conftest is also
    auto-registered by path during a full-suite run, so naming it as a plugin
    double-registers the same module under two names and aborts collection
    (passes in isolation, fails the full ``make ci``).
    """

    def _create():
        journal = tmp_path / "journal"
        journal.mkdir()

        config_dir = journal / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "journal.json").write_text(
            json.dumps({"setup": {"completed_at": 1700000000000}}, indent=2)
        )

        monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

        from solstone.convey import create_app

        app = create_app(journal=str(journal))

        class Env:
            def __init__(self):
                self.journal = journal
                self.client = app.test_client()
                self.app = app

        return Env()

    return _create


@pytest.fixture
def segment_journal(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    journal.mkdir()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")
    return journal


def _segment_dir(
    journal: Path,
    day: str = DAY,
    segment: str = SEGMENT,
    stream: str = STREAM,
) -> Path:
    path = journal / "chronicle" / day / stream / segment
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _read_header(path: Path) -> dict[str, Any]:
    return _read_jsonl(path)[0]


def _read_processing_record(path: Path) -> dict[str, Any]:
    record = _read_header(path)["_solstone_processing"]
    assert isinstance(record, dict)
    return record


def _assert_processing_record(
    record: dict[str, Any],
    *,
    state: str,
    reason_code: str,
    handler: str,
) -> None:
    assert record["state"] == state
    assert record["reason_code"] == reason_code
    assert record["handler"] == handler
    assert record["attempted_at"] == FIXED_NOW


def _write_silent_flac(path: Path, seconds: float = 0.5) -> None:
    sf.write(
        path,
        np.zeros(int(seconds * SAMPLE_RATE), np.float32),
        SAMPLE_RATE,
        format="FLAC",
    )


def _silent_flac_bytes(seconds: float = 0.5) -> bytes:
    buf = io.BytesIO()
    sf.write(
        buf,
        np.zeros(int(seconds * SAMPLE_RATE), np.float32),
        SAMPLE_RATE,
        format="FLAC",
    )
    return buf.getvalue()


def _require_real_cv2(monkeypatch):
    module = sys.modules.get("cv2")
    if module is not None and not hasattr(module, "aruco"):
        # Root tests/conftest.py installs an aruco-less cv2 stub when cv2 has
        # not been imported yet. Evict it via monkeypatch so the real OpenCV
        # is restored (and the stub reinstated) at test teardown — no leak
        # into other tests in a full run.
        monkeypatch.delitem(sys.modules, "cv2", raising=False)

    cv2 = pytest.importorskip("cv2")
    if not hasattr(cv2, "aruco"):
        pytest.skip("OpenCV ArUco support is unavailable")
    return cv2


def _build_convey_covered_mp4(monkeypatch, path: Path) -> None:
    pytest.importorskip("av")
    cv2 = _require_real_cv2(monkeypatch)
    import av

    from solstone.observe.aruco import CORNER_TAG_IDS

    # PyAV 16.1.0 does not mux a true zero-frame MP4: closing without
    # encoding frames leaves no output file. This valid one-frame MP4 instead
    # drives the real decode loop and is skipped by the production Convey-mask
    # gate, producing zero qualified frames.
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    size = 512
    marker_size = 80
    margin = 4
    positions_by_id = {
        6: (margin, margin),
        7: (size - marker_size - margin, margin),
        4: (margin, size - marker_size - margin),
        2: (size - marker_size - margin, size - marker_size - margin),
    }
    assert set(positions_by_id) == CORNER_TAG_IDS

    image = Image.new("RGB", (size, size), "white")
    for tag_id, pos in positions_by_id.items():
        marker = cv2.aruco.generateImageMarker(dictionary, tag_id, marker_size)
        image.paste(Image.fromarray(marker).convert("RGB"), pos)

    with av.open(str(path), "w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=1)
        stream.width = size
        stream.height = size
        stream.pix_fmt = "yuv420p"
        frame = av.VideoFrame.from_ndarray(np.asarray(image), format="rgb24")
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def _build_one_frame_mp4(path: Path) -> None:
    pytest.importorskip("av")
    import av

    with av.open(str(path), "w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=1)
        stream.width = 64
        stream.height = 64
        stream.pix_fmt = "yuv420p"
        frame = av.VideoFrame.from_ndarray(
            np.zeros((64, 64, 3), dtype=np.uint8),
            format="rgb24",
        )
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def _drive_describe(
    monkeypatch,
    video_path: Path,
    output_path: Path,
    *,
    agenerate_response: str = "{}",
    expect_runtime_error: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], AsyncMock]:
    from solstone.observe import describe, processing_record

    agenerate = AsyncMock(return_value=agenerate_response)
    monkeypatch.setattr(
        "solstone.think.models.resolve_provider",
        lambda _context, _interface: ("google", "gemini-test"),
    )
    monkeypatch.setattr(describe, "callosum_send", lambda *args, **kwargs: None)
    monkeypatch.setattr(describe, "select_frames_for_extraction", lambda *a, **k: [])
    monkeypatch.setattr(processing_record, "now_iso_utc", lambda: FIXED_NOW)
    monkeypatch.setattr("solstone.think.batch.agenerate", agenerate)

    processor = describe.VideoProcessor(video_path)
    if expect_runtime_error:
        with pytest.raises(RuntimeError):
            asyncio.run(
                processor.process_with_vision(
                    max_concurrent=1,
                    output_path=output_path,
                    work_key=f"{DAY}/{video_path.parent.name}/{video_path.stem}",
                )
            )
    else:
        asyncio.run(
            processor.process_with_vision(
                max_concurrent=1,
                output_path=output_path,
                work_key=f"{DAY}/{video_path.parent.name}/{video_path.stem}",
            )
        )

    header = _read_header(output_path)
    record = header["_solstone_processing"]
    assert isinstance(record, dict)
    return header, record, agenerate


def _drive_transcribe(
    monkeypatch,
    audio_path: Path,
    *,
    preserve_all: bool,
) -> tuple[dict[str, Any] | None, Mock, Path]:
    from solstone.observe import processing_record

    transcribe_main = importlib.import_module("solstone.observe.transcribe.main")
    stt_spy = Mock(return_value=[])
    monkeypatch.setattr(processing_record, "now_iso_utc", lambda: FIXED_NOW)
    monkeypatch.setattr(
        transcribe_main,
        "callosum_send",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(transcribe_main, "stt_transcribe", stt_spy)

    transcribe_main._process_one(
        audio_path,
        argparse.Namespace(backend=None, cpu=False, model=None, redo=False),
        {"preserve_all": preserve_all},
        "whisper",
        [],
    )

    jsonl_path = audio_path.with_suffix(".jsonl")
    record = _read_processing_record(jsonl_path) if jsonl_path.exists() else None
    return record, stt_spy, jsonl_path


def _sense_config_with_load() -> dict[str, dict[str, Any]]:
    return {
        "sense": {
            "priority": 10,
            "type": "generate",
            "output": "json",
            "schedule": "segment",
            "load": {
                "transcripts": True,
                "percepts": True,
                "talents": False,
            },
        },
        "documents": {
            "priority": 20,
            "type": "cogitate",
            "schedule": "segment",
        },
        "screen": {
            "priority": 20,
            "type": "generate",
            "output": "md",
            "schedule": "segment",
        },
    }


def _run_idle_gate(
    monkeypatch,
    journal: Path,
    day: str,
    segment: str,
    *,
    agenerate_spy: AsyncMock | None = None,
) -> tuple[tuple[int, int, list[str]], list[str], list[dict[str, Any]], AsyncMock]:
    from solstone.think import thinking as think
    from solstone.think.thinking import ThinkingJSONLWriter

    spawned: list[str] = []
    writer_path = journal / "chronicle" / day / "health" / f"idle_{segment}.jsonl"
    writer = ThinkingJSONLWriter(str(writer_path))
    agenerate = agenerate_spy or AsyncMock(return_value="{}")
    original_callosum = think._callosum
    original_jsonl = think._jsonl
    try:
        monkeypatch.setattr(
            think,
            "get_talent_configs",
            lambda schedule=None, **kwargs: _sense_config_with_load(),
        )
        monkeypatch.setattr(
            think,
            "cortex_request",
            lambda prompt, name, config=None: spawned.append(name) or f"agent-{name}",
        )
        monkeypatch.setattr(
            think,
            "wait_for_uses",
            lambda agent_ids, timeout=600: ({aid: "finish" for aid in agent_ids}, []),
        )
        monkeypatch.setattr("solstone.think.batch.agenerate", agenerate)
        think._callosum = None
        think._jsonl = writer
        result = think.run_segment_sense(
            day,
            segment,
            refresh=False,
            verbose=False,
            stream=STREAM,
        )
    finally:
        writer.close()
        think._callosum = original_callosum
        think._jsonl = original_jsonl

    return result, spawned, _read_jsonl(writer_path), agenerate


def _completion(day: str):
    return classify_segment_completion(
        cluster_segments(day),
        read_segment_progress(day),
    )


def _create_observer(env, name: str) -> str:
    response = env.client.post(
        "/app/observer/api/create",
        json={"name": name},
        content_type="application/json",
    )
    assert response.status_code == 200
    return response.get_json()["key"]


def test_ac1_ingest_drops_zero_byte_keeps_valid_media(observer_env):
    # Cross-ref:
    # solstone/apps/observer/tests/test_routes.py::test_ingest_zero_byte_file_rejected
    # solstone/apps/observer/tests/test_routes.py::test_ingest_mixed_zero_byte_files
    env = observer_env()
    key = _create_observer(env, "bad-media-observer")
    valid_data = _silent_flac_bytes()

    response = env.client.post(
        "/app/observer/ingest",
        headers={"Authorization": f"Bearer {key}"},
        data={
            "day": DAY,
            "segment": SEGMENT,
            "files": [
                (io.BytesIO(b""), "empty.flac"),
                (io.BytesIO(valid_data), "audio.flac"),
            ],
        },
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["files"] == ["audio.flac"]
    assert data["bytes"] == len(valid_data)
    segment = env.journal / "chronicle" / DAY / "bad-media-observer" / SEGMENT
    assert (segment / "audio.flac").read_bytes() == valid_data
    assert not (segment / "empty.flac").exists()


def test_ac2_empty_screen_terminalizes_to_idle_quietly(segment_journal, monkeypatch):
    segment = _segment_dir(segment_journal)
    video_path = segment / "screen.mp4"
    output_path = segment / "screen.jsonl"
    _build_convey_covered_mp4(monkeypatch, video_path)

    header, record, agenerate = _drive_describe(monkeypatch, video_path, output_path)

    _assert_processing_record(
        record,
        state=STATE_EMPTY,
        reason_code=REASON_NO_DECODABLE_FRAMES,
        handler=HANDLER_DESCRIBE,
    )
    assert header["qualified_count"] == 0
    assert agenerate.call_count == 0
    assert read_segment_data_state(DAY, SEGMENT) == {"screen": DataState.EMPTY.value}

    result, spawned, _events, idle_agenerate = _run_idle_gate(
        monkeypatch,
        segment_journal,
        DAY,
        SEGMENT,
        agenerate_spy=agenerate,
    )

    assert result == (0, 0, [])
    assert spawned == []
    assert idle_agenerate.call_count == 0
    assert _completion(DAY).blockers == []


def test_ac3_silent_audio_records_empty_no_stt(segment_journal, monkeypatch):
    segment = _segment_dir(segment_journal)
    preserve_audio = segment / "audio.flac"
    _write_silent_flac(preserve_audio)

    record, stt_spy, jsonl_path = _drive_transcribe(
        monkeypatch,
        preserve_audio,
        preserve_all=True,
    )

    assert record is not None
    _assert_processing_record(
        record,
        state=STATE_EMPTY,
        reason_code=REASON_NO_DECODABLE_AUDIO,
        handler=HANDLER_TRANSCRIBE,
    )
    assert stt_spy.call_count == 0
    assert len(jsonl_path.read_text(encoding="utf-8").splitlines()) == 1

    filter_audio = segment / "filtered.flac"
    _write_silent_flac(filter_audio)
    filtered_record, filtered_stt, filtered_jsonl = _drive_transcribe(
        monkeypatch,
        filter_audio,
        preserve_all=False,
    )

    assert filtered_record is None
    assert filtered_stt.call_count == 0
    assert not filter_audio.exists()
    assert not filtered_jsonl.exists()


def test_ac4_corrupt_screen_is_failed_distinct_from_empty(segment_journal, monkeypatch):
    empty_segment = _segment_dir(segment_journal, segment="121000_300")
    empty_video = empty_segment / "screen.mp4"
    _build_convey_covered_mp4(monkeypatch, empty_video)
    _empty_header, empty_record, _empty_agenerate = _drive_describe(
        monkeypatch,
        empty_video,
        empty_segment / "screen.jsonl",
    )

    corrupt_segment = _segment_dir(segment_journal, segment="122000_300")
    corrupt_video = corrupt_segment / "screen.mp4"
    corrupt_video.write_bytes(b"not a real mp4 file at all")
    _corrupt_header, corrupt_record, corrupt_agenerate = _drive_describe(
        monkeypatch,
        corrupt_video,
        corrupt_segment / "screen.jsonl",
    )

    empty_pair = (empty_record["state"], empty_record["reason_code"])
    corrupt_pair = (corrupt_record["state"], corrupt_record["reason_code"])
    assert empty_pair == (STATE_EMPTY, REASON_NO_DECODABLE_FRAMES)
    assert corrupt_pair == (STATE_FAILED, REASON_CORRUPT_INPUT)
    assert corrupt_pair != empty_pair
    assert corrupt_agenerate.call_count == 0
    assert read_segment_data_state(DAY, "121000_300") == {
        "screen": DataState.EMPTY.value
    }
    assert read_segment_data_state(DAY, "122000_300") == {
        "screen": DataState.FAILED.value
    }
    assert DataState.FAILED != DataState.EMPTY


def test_ac5_all_frames_fail_is_analysis_failed_distinct(
    segment_journal,
    monkeypatch,
):
    segment = _segment_dir(segment_journal)
    video_path = segment / "screen.mp4"
    output_path = segment / "screen.jsonl"
    _build_one_frame_mp4(video_path)

    _header, record, agenerate = _drive_describe(
        monkeypatch,
        video_path,
        output_path,
        agenerate_response="not json",
        expect_runtime_error=True,
    )

    _assert_processing_record(
        record,
        state=STATE_FAILED,
        reason_code=REASON_ANALYSIS_FAILED,
        handler=HANDLER_DESCRIBE,
    )
    assert agenerate.call_count == 5  # 1 qualified frame + 4 retries.
    assert record["reason_code"] != REASON_CORRUPT_INPUT
    assert record["reason_code"] != REASON_NO_DECODABLE_FRAMES
    assert record["state"] != STATE_ANALYZED
    # Both corrupt_input and analysis_failed derive DataState.FAILED; the
    # distinction survives only in the processing-record reason_code.
    assert read_segment_data_state(DAY, SEGMENT) == {"screen": DataState.FAILED.value}


def test_ac6_no_model_calls_on_all_empty_segment(segment_journal, monkeypatch):
    segment = _segment_dir(segment_journal)
    screen_path = segment / "screen.mp4"
    audio_path = segment / "audio.flac"
    _build_convey_covered_mp4(monkeypatch, screen_path)
    _write_silent_flac(audio_path)

    _screen_header, screen_record, agenerate = _drive_describe(
        monkeypatch,
        screen_path,
        segment / "screen.jsonl",
    )
    audio_record, stt_spy, _audio_jsonl = _drive_transcribe(
        monkeypatch,
        audio_path,
        preserve_all=True,
    )
    result, spawned, _events, idle_agenerate = _run_idle_gate(
        monkeypatch,
        segment_journal,
        DAY,
        SEGMENT,
        agenerate_spy=agenerate,
    )

    _assert_processing_record(
        screen_record,
        state=STATE_EMPTY,
        reason_code=REASON_NO_DECODABLE_FRAMES,
        handler=HANDLER_DESCRIBE,
    )
    assert audio_record is not None
    _assert_processing_record(
        audio_record,
        state=STATE_EMPTY,
        reason_code=REASON_NO_DECODABLE_AUDIO,
        handler=HANDLER_TRANSCRIBE,
    )
    assert result == (0, 0, [])
    assert spawned == []
    assert idle_agenerate.call_count == 0
    assert stt_spy.call_count == 0
    assert _completion(DAY).blockers == []


def test_ac7_terminal_empty_day_has_no_churn(segment_journal, monkeypatch):
    segment = _segment_dir(segment_journal)
    video_path = segment / "screen.mp4"
    _build_convey_covered_mp4(monkeypatch, video_path)
    _header, _record, agenerate = _drive_describe(
        monkeypatch,
        video_path,
        segment / "screen.jsonl",
    )
    result, _spawned, _events, idle_agenerate = _run_idle_gate(
        monkeypatch,
        segment_journal,
        DAY,
        SEGMENT,
        agenerate_spy=agenerate,
    )
    assert result == (0, 0, [])

    for _ in range(3):
        assert read_segment_data_state(DAY, SEGMENT) == {
            "screen": DataState.EMPTY.value
        }
        assert _completion(DAY).blockers == []
        assert idle_agenerate.call_count == 0


def test_ac8_reprocess_terminalized_is_idempotent(segment_journal, monkeypatch):
    segment = _segment_dir(segment_journal)
    screen_path = segment / "screen.mp4"
    audio_path = segment / "audio.flac"
    screen_jsonl = segment / "screen.jsonl"
    _build_convey_covered_mp4(monkeypatch, screen_path)
    _write_silent_flac(audio_path)

    _header, _screen_record, agenerate = _drive_describe(
        monkeypatch,
        screen_path,
        screen_jsonl,
    )
    _audio_record, stt_spy, audio_jsonl = _drive_transcribe(
        monkeypatch,
        audio_path,
        preserve_all=True,
    )
    result, _spawned, _events, idle_agenerate = _run_idle_gate(
        monkeypatch,
        segment_journal,
        DAY,
        SEGMENT,
        agenerate_spy=agenerate,
    )
    assert result == (0, 0, [])
    screen_lines = len(screen_jsonl.read_text(encoding="utf-8").splitlines())
    audio_lines = len(audio_jsonl.read_text(encoding="utf-8").splitlines())

    rerun_result, rerun_spawned, _rerun_events, rerun_agenerate = _run_idle_gate(
        monkeypatch,
        segment_journal,
        DAY,
        SEGMENT,
        agenerate_spy=idle_agenerate,
    )
    assert rerun_result == (0, 0, [])
    assert rerun_spawned == []
    assert rerun_agenerate.call_count == 0
    assert len(screen_jsonl.read_text(encoding="utf-8").splitlines()) == screen_lines
    assert read_segment_data_state(DAY, SEGMENT) == {
        "audio": DataState.EMPTY.value,
        "screen": DataState.EMPTY.value,
    }
    assert _completion(DAY).blockers == []

    _second_record, second_stt_spy, _second_audio_jsonl = _drive_transcribe(
        monkeypatch,
        audio_path,
        preserve_all=True,
    )
    assert second_stt_spy.call_count == 0
    assert len(audio_jsonl.read_text(encoding="utf-8").splitlines()) == audio_lines
    assert stt_spy.call_count == 0


def test_ac9_corrupt_output_jsonl_derives_pending(segment_journal):
    segment = _segment_dir(segment_journal)
    (segment / "screen.jsonl").write_bytes(b"\x00\x01 not json\n")

    assert read_segment_data_state(DAY, SEGMENT) == {"screen": DataState.PENDING.value}
    completion = _completion(DAY)
    assert completion.blockers == [
        {
            "segment": SEGMENT,
            "dimension": "not_sensed",
            "detail": f"screen={DataState.PENDING.value}",
        }
    ]
