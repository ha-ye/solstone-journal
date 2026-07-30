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
    is_failure_exhausted,
    should_reenter_analysis_output,
)
from solstone.observe.utils import SAMPLE_RATE, AudioDecodeError
from solstone.observe.vad import VadResult
from solstone.think.cluster import (
    cluster_segments,
    read_segment_data_state,
)
from solstone.think.data_state import DataState
from solstone.think.pipeline_health import (
    SegmentProgress,
    classify_segment_completion,
    read_segment_progress,
    segment_fully_sensed,
)

DAY = "20990501"
STREAM = "default"
SEGMENT = "120000_300"
FIXED_NOW = "2026-06-30T12:00:00Z"
TEST_PL_FINGERPRINT = "sha256:" + ("d" * 64)


def _generate_result(text: str, finish_reason: str = "stop") -> dict[str, Any]:
    return {"text": text, "finish_reason": finish_reason}


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
        from solstone.convey import root as convey_root
        from solstone.think.link.auth import AuthorizedClients
        from solstone.think.link.paths import authorized_clients_path

        app = create_app(journal=str(journal))
        authorized = AuthorizedClients(authorized_clients_path())
        authorized.add(
            TEST_PL_FINGERPRINT,
            "bad-media-observer",
            "instance-1",
            paired_at="2026-05-20T00:00:00Z",
        )
        monkeypatch.setattr(convey_root, "get_authorized_clients", lambda: authorized)

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


def _build_truncated_webm(path: Path) -> None:
    pytest.importorskip("av")
    import av

    buf = io.BytesIO()
    with av.open(buf, "w", format="webm") as container:
        stream = container.add_stream("libvpx", rate=1)
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
    path.write_bytes(buf.getvalue()[:36])


def _build_audio_only_mp4(path: Path) -> None:
    pytest.importorskip("av")
    import av

    with av.open(str(path), "w", format="mp4") as container:
        stream = container.add_stream("aac", rate=48000)
        stream.layout = "mono"
        frame = av.AudioFrame.from_ndarray(
            np.zeros((1, 4800), dtype=np.float32),
            format="flt",
            layout="mono",
        )
        frame.sample_rate = 48000
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
    agenerate_finish_reason: str = "stop",
    expect_runtime_error: bool = False,
    provider_result: tuple[str, str] = ("google", "gemini-test"),
    expect_record: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None, AsyncMock]:
    from solstone.observe import describe, processing_record

    agenerate = AsyncMock(
        return_value=_generate_result(agenerate_response, agenerate_finish_reason)
    )
    monkeypatch.setattr(
        "solstone.think.models.resolve_provider",
        lambda _interface: provider_result,
    )
    monkeypatch.setattr(describe, "callosum_send", lambda *args, **kwargs: None)
    monkeypatch.setattr(describe, "select_frames_for_extraction", lambda *a, **k: [])
    monkeypatch.setattr(processing_record, "now_iso_utc", lambda: FIXED_NOW)
    monkeypatch.setattr("solstone.think.batch.agenerate_with_result", agenerate)

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
    record = header.get("_solstone_processing")
    if expect_record:
        assert isinstance(record, dict)
    else:
        assert record is None
    return header, record, agenerate


def _drive_transcribe(
    monkeypatch,
    audio_path: Path,
    *,
    preserve_all: bool,
) -> tuple[dict[str, Any] | None, Mock, Path]:
    from solstone.observe import processing_record

    transcribe_main = importlib.import_module("solstone.observe.transcribe.main")
    vad_module = importlib.import_module("solstone.observe.vad")
    stt_spy = Mock(return_value=[])
    monkeypatch.setattr(processing_record, "now_iso_utc", lambda: FIXED_NOW)
    monkeypatch.setattr(
        transcribe_main,
        "callosum_send",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(transcribe_main, "stt_transcribe", stt_spy)
    monkeypatch.setattr(
        vad_module,
        "run_vad",
        lambda _audio, **_kwargs: VadResult(
            duration=0.5,
            speech_duration=0.0,
            has_speech=False,
        ),
    )
    monkeypatch.setattr(transcribe_main, "tag_audio", lambda *_args, **_kwargs: None)

    transcribe_main._process_one(
        audio_path,
        argparse.Namespace(backend=None, cpu=False, model=None, redo=False),
        {"preserve_all": preserve_all},
        "parakeet",
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
    agenerate = agenerate_spy or AsyncMock(return_value=_generate_result("{}"))
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
            lambda prompt, name, config=None, **kwargs: (
                spawned.append(name) or f"agent-{name}"
            ),
        )
        monkeypatch.setattr(
            think,
            "wait_for_uses",
            lambda agent_ids, timeout=600: ({aid: "finish" for aid in agent_ids}, []),
        )
        monkeypatch.setattr("solstone.think.batch.agenerate_with_result", agenerate)
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
    key = response.get_json()["key"]

    from solstone.apps.observer.utils import load_observer, save_observer

    observer = load_observer(key)
    assert observer is not None
    observer["device_binding"] = {
        "device": TEST_PL_FINGERPRINT,
        "kind": "cert",
    }
    assert save_observer(observer)
    return key


def _pl_identity():
    from solstone.convey.secure_listener import ConveyIdentity

    return ConveyIdentity(
        mode="pl-via-spl",
        fingerprint=TEST_PL_FINGERPRINT,
        device_label="bad-media-observer",
        paired_at="2026-05-20T00:00:00Z",
        session_id="session-1",
    )


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
        environ_overrides={"pl.identity": _pl_identity()},
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

    assert filtered_record is not None
    _assert_processing_record(
        filtered_record,
        state=STATE_EMPTY,
        reason_code=REASON_NO_DECODABLE_AUDIO,
        handler=HANDLER_TRANSCRIBE,
    )
    assert filtered_stt.call_count == 0
    assert not filter_audio.exists()
    assert filtered_jsonl.exists()
    assert len(filtered_jsonl.read_text(encoding="utf-8").splitlines()) == 1


def test_corrupt_audio_decode_records_failed_without_vad_or_stt(
    segment_journal,
    monkeypatch,
):
    from solstone.observe import processing_record

    transcribe_main = importlib.import_module("solstone.observe.transcribe.main")
    vad_module = importlib.import_module("solstone.observe.vad")
    segment = _segment_dir(segment_journal)
    audio_path = segment / "audio.m4a"
    audio_path.write_bytes(b"truncated")

    stt_spy = Mock(return_value=[])
    vad_spy = Mock()
    monkeypatch.setattr(processing_record, "now_iso_utc", lambda: FIXED_NOW)
    monkeypatch.setattr(
        transcribe_main,
        "callosum_send",
        lambda *args, **kwargs: None,
    )
    load_audio_spy = Mock(side_effect=AudioDecodeError("worker exited from signal 11"))
    monkeypatch.setattr(transcribe_main, "load_audio", load_audio_spy)
    monkeypatch.setattr(transcribe_main, "stt_transcribe", stt_spy)
    monkeypatch.setattr(vad_module, "run_vad", vad_spy)

    transcribe_main._process_one(
        audio_path,
        argparse.Namespace(backend=None, cpu=False, model=None, redo=False),
        {"preserve_all": True},
        "parakeet",
    )

    jsonl_path = audio_path.with_suffix(".jsonl")
    record = _read_processing_record(jsonl_path)
    _assert_processing_record(
        record,
        state=STATE_FAILED,
        reason_code=REASON_CORRUPT_INPUT,
        handler=HANDLER_TRANSCRIBE,
    )
    assert len(jsonl_path.read_text(encoding="utf-8").splitlines()) == 1
    assert stt_spy.call_count == 0
    assert vad_spy.call_count == 0
    assert load_audio_spy.call_count == 1
    assert read_segment_data_state(DAY, SEGMENT) == {
        "audio": DataState.FAILED_FINAL.value
    }

    load_audio_spy.reset_mock()
    stt_spy.reset_mock()
    vad_spy.reset_mock()

    transcribe_main._process_one(
        audio_path,
        argparse.Namespace(backend=None, cpu=False, model=None, redo=False),
        {"preserve_all": True},
        "parakeet",
    )

    assert load_audio_spy.call_count == 0
    assert stt_spy.call_count == 0
    assert vad_spy.call_count == 0


def test_eof_truncated_screen_terminalizes_corrupt_input(
    segment_journal,
    monkeypatch,
):
    av = pytest.importorskip("av")

    segment = _segment_dir(segment_journal)
    video_path = segment / "screen.webm"
    output_path = segment / "screen.jsonl"
    _build_truncated_webm(video_path)

    with pytest.raises(av.error.EOFError):
        with av.open(str(video_path)) as c:
            for _ in c.decode(video=0):
                pass

    _header, record, agenerate = _drive_describe(
        monkeypatch,
        video_path,
        output_path,
    )

    _assert_processing_record(
        record,
        state=STATE_FAILED,
        reason_code=REASON_CORRUPT_INPUT,
        handler=HANDLER_DESCRIBE,
    )
    assert record["attempts"] == 1
    assert record["schema"] == "solstone.processing.v1"
    assert agenerate.call_count == 0
    assert read_segment_data_state(DAY, SEGMENT) == {
        "screen": DataState.FAILED_FINAL.value
    }
    assert (
        should_reenter_analysis_output(
            record=record,
            output_path=output_path,
            handler=HANDLER_DESCRIBE,
        )
        is False
    )


def test_ac4_no_provider_truncated_recordless_screen_converges_corrupt_input(
    segment_journal,
    monkeypatch,
):
    from solstone.think.models import NO_BRAIN_PROVIDER

    segment = _segment_dir(segment_journal, segment="123500_300")
    video_path = segment / "screen.webm"
    output_path = segment / "screen.jsonl"
    _build_truncated_webm(video_path)
    output_path.write_text(
        json.dumps({"raw": video_path.name}) + "\n",
        encoding="utf-8",
    )

    _header, record, agenerate = _drive_describe(
        monkeypatch,
        video_path,
        output_path,
        provider_result=(NO_BRAIN_PROVIDER, ""),
    )

    _assert_processing_record(
        record,
        state=STATE_FAILED,
        reason_code=REASON_CORRUPT_INPUT,
        handler=HANDLER_DESCRIBE,
    )
    assert agenerate.call_count == 0
    assert is_failure_exhausted(record) is True
    assert (
        should_reenter_analysis_output(
            record=record,
            output_path=output_path,
            handler=HANDLER_DESCRIBE,
        )
        is False
    )


def test_ac6_failed_final_screen_record_unblocks_day_with_failed_marker(
    segment_journal,
    monkeypatch,
):
    from solstone.think.models import NO_BRAIN_PROVIDER

    segment_key = "123600_300"
    segment = _segment_dir(segment_journal, segment=segment_key)
    video_path = segment / "screen.webm"
    output_path = segment / "screen.jsonl"
    _build_truncated_webm(video_path)
    output_path.write_text(
        json.dumps({"raw": video_path.name}) + "\n",
        encoding="utf-8",
    )

    _header, record, agenerate = _drive_describe(
        monkeypatch,
        video_path,
        output_path,
        provider_result=(NO_BRAIN_PROVIDER, ""),
    )
    (segment / ".analyze_failed_screen").write_text("{}\n", encoding="utf-8")

    _assert_processing_record(
        record,
        state=STATE_FAILED,
        reason_code=REASON_CORRUPT_INPUT,
        handler=HANDLER_DESCRIBE,
    )
    assert agenerate.call_count == 0
    data_state = read_segment_data_state(DAY, segment_key)
    assert data_state == {"screen": DataState.FAILED_FINAL.value}
    assert segment_fully_sensed(data_state) is True

    progress = {
        (STREAM, segment_key): SegmentProgress(
            sensed=True,
            density="idle",
            change_class=None,
            dispatched=frozenset(),
            completed=frozenset(),
            unconfigured=frozenset(),
            capped=frozenset(),
        )
    }
    completion = classify_segment_completion(cluster_segments(DAY), progress)
    assert completion.blockers == []
    assert completion.exhausted == (segment_key,)


def test_no_video_stream_screen_terminalizes_corrupt_input(
    segment_journal,
    monkeypatch,
):
    av = pytest.importorskip("av")
    segment_key = "123000_300"
    segment = _segment_dir(segment_journal, segment=segment_key)
    video_path = segment / "screen.mp4"
    output_path = segment / "screen.jsonl"
    _build_audio_only_mp4(video_path)

    with av.open(str(video_path)) as c:
        assert len(c.streams.video) == 0

    _header, record, agenerate = _drive_describe(
        monkeypatch,
        video_path,
        output_path,
    )

    _assert_processing_record(
        record,
        state=STATE_FAILED,
        reason_code=REASON_CORRUPT_INPUT,
        handler=HANDLER_DESCRIBE,
    )
    assert record["attempts"] == 1
    assert agenerate.call_count == 0
    assert read_segment_data_state(DAY, segment_key) == {
        "screen": DataState.FAILED_FINAL.value
    }


def _install_partial_decode_failure(monkeypatch, video_path: Path) -> None:
    from fractions import Fraction

    av = pytest.importorskip("av")
    from solstone.observe import aruco

    decode_error = av.error.InvalidDataError(
        1094995529,
        "partial decode failure",
    )
    assert isinstance(decode_error, av.error.FFmpegError)

    frame = av.VideoFrame.from_ndarray(
        np.zeros((64, 64, 3), dtype=np.uint8),
        format="rgb24",
    )
    frame.pts = 1
    frame.time_base = Fraction(1, 1)

    class FakeCodecContext:
        thread_count = 0

    class FakeStream:
        def __init__(self):
            self.thread_type = None
            self.codec_context = FakeCodecContext()
            self.width = 64
            self.height = 64

    class FakeStreams:
        video = (FakeStream(),)

    class FakeContainer:
        streams = FakeStreams()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def decode(self, video=0):
            assert video == 0
            yield frame
            raise decode_error

    video_path.write_bytes(b"fake container bytes")
    monkeypatch.setattr(av, "open", lambda *args, **kwargs: FakeContainer())
    monkeypatch.setattr(aruco, "detect_markers", lambda _image: None)


def test_partial_decode_failure_preserves_qualified_count(
    segment_journal,
    monkeypatch,
):
    segment_key = "125000_300"
    segment = _segment_dir(segment_journal, segment=segment_key)
    video_path = segment / "screen.mp4"
    output_path = segment / "screen.jsonl"
    _install_partial_decode_failure(monkeypatch, video_path)

    header, record, agenerate = _drive_describe(
        monkeypatch,
        video_path,
        output_path,
    )

    _assert_processing_record(
        record,
        state=STATE_FAILED,
        reason_code=REASON_CORRUPT_INPUT,
        handler=HANDLER_DESCRIBE,
    )
    assert record["attempts"] == 1
    assert header["qualified_count"] == 1
    assert agenerate.call_count == 1
    rows = _read_jsonl(output_path)
    assert len(rows) == 2
    assert rows[1]["frame_id"] == 1


def test_partial_decode_failure_without_provider_reenters_recordless_output(
    segment_journal,
    monkeypatch,
):
    from solstone.think.models import NO_BRAIN_PROVIDER

    segment_key = "125500_300"
    segment = _segment_dir(segment_journal, segment=segment_key)
    video_path = segment / "screen.mp4"
    output_path = segment / "screen.jsonl"
    _install_partial_decode_failure(monkeypatch, video_path)
    output_path.write_text(
        json.dumps({"raw": video_path.name}) + "\n",
        encoding="utf-8",
    )
    original_output = output_path.read_bytes()

    header, record, agenerate = _drive_describe(
        monkeypatch,
        video_path,
        output_path,
        provider_result=(NO_BRAIN_PROVIDER, ""),
        expect_record=False,
    )

    assert header == {"raw": video_path.name}
    assert record is None
    assert output_path.read_bytes() == original_output
    assert agenerate.call_count == 0
    assert read_segment_data_state(DAY, segment_key) == {
        "screen": DataState.PENDING.value
    }
    assert (
        should_reenter_analysis_output(
            record=record,
            output_path=output_path,
            handler=HANDLER_DESCRIBE,
        )
        is True
    )


def test_aruco_frame_body_index_error_still_propagates(
    segment_journal,
    monkeypatch,
):
    from solstone.observe import aruco, describe

    segment = _segment_dir(segment_journal, segment="124000_300")
    video_path = segment / "screen.mp4"
    output_path = segment / "screen.jsonl"
    _build_one_frame_mp4(video_path)

    def raise_index_error(_image):
        raise IndexError("frame body boom")

    monkeypatch.setattr(aruco, "detect_markers", raise_index_error)

    processor = describe.VideoProcessor(video_path)
    with pytest.raises(IndexError, match="frame body boom"):
        processor.process()
    assert not output_path.exists()


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
        "screen": DataState.FAILED_FINAL.value
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
    # analysis_failed without attempts remains retryable; corrupt_input derives
    # DataState.FAILED_FINAL from the processing-record reason_code.
    assert read_segment_data_state(DAY, SEGMENT) == {"screen": DataState.FAILED.value}


def test_truncated_frame_categorization_retries_and_promotes_no_frame_artifact(
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
        agenerate_response='{"visual_description":"partial"',
        agenerate_finish_reason="max_tokens",
        expect_runtime_error=True,
    )

    _assert_processing_record(
        record,
        state=STATE_FAILED,
        reason_code=REASON_ANALYSIS_FAILED,
        handler=HANDLER_DESCRIBE,
    )
    assert agenerate.call_count == 5
    assert len(_read_jsonl(output_path)) == 1


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
