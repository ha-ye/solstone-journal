# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from solstone.observe import describe as describe_module
from solstone.observe.exit_codes import EXIT_PROVIDER_BLOCKED
from solstone.observe.sense import FileSensor
from solstone.think.providers.state import ProviderState


def _video_path(tmp_path: Path) -> Path:
    segment_dir = tmp_path / "chronicle" / "20250101" / "default" / "143022_300"
    segment_dir.mkdir(parents=True)
    video_path = segment_dir / "screen.webm"
    video_path.write_text("video", encoding="utf-8")
    return video_path


def _blocked_state(context: str = describe_module.FRAME_CONTEXT) -> ProviderState:
    return ProviderState(
        provider="google",
        interface="generate",
        status="blocked",
        model="gemini-test",
        context=context,
        reason_code="provider_key_missing",
    )


def _ready_state(context: str = describe_module.FRAME_CONTEXT) -> ProviderState:
    return ProviderState(
        provider="google",
        interface="generate",
        status="ready",
        model="gemini-test",
        context=context,
    )


def test_preflight_blocked_emits_one_keyed_notification_and_writes_no_jsonl(
    tmp_path, monkeypatch
):
    video_path = _video_path(tmp_path)
    emitted = []
    readiness_calls = []

    monkeypatch.setattr(
        describe_module,
        "_dedup_readiness_contexts",
        lambda _contexts: [describe_module.FRAME_CONTEXT],
    )

    def fake_readiness(context: str, interface: str) -> ProviderState:
        readiness_calls.append((context, interface))
        return _blocked_state(context)

    monkeypatch.setattr(
        describe_module.provider_state,
        "readiness_for_context",
        fake_readiness,
    )
    monkeypatch.setattr(
        describe_module,
        "callosum_send",
        lambda tract, event, **kwargs: emitted.append((tract, event, kwargs)),
    )

    with pytest.raises(SystemExit) as exc_info:
        describe_module._preflight_provider_readiness(
            video_path,
            day="20250101",
            segment="143022_300",
        )

    assert exc_info.value.code == EXIT_PROVIDER_BLOCKED
    assert readiness_calls == [(describe_module.FRAME_CONTEXT, "generate")]
    assert len(emitted) == 1

    tract, event, payload = emitted[0]
    assert (tract, event) == ("notification", "show")
    assert payload["key"] == "provider_key_missing:google:"
    assert payload["work_key"] == "20250101/143022_300/screen"
    assert payload["reason_code"] == "provider_key_missing"
    assert payload["provider"] == "google"
    assert payload["context"] == describe_module.FRAME_CONTEXT
    assert payload["title"] == "Screen descriptions paused"
    assert payload["action"] == "/app/thinking/#main"
    assert not video_path.with_suffix(".jsonl").exists()


def test_blocked_then_ready_leaves_media_reeligible(tmp_path, monkeypatch):
    video_path = _video_path(tmp_path)
    states = [_blocked_state(), _ready_state()]

    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    monkeypatch.setattr(
        describe_module,
        "_dedup_readiness_contexts",
        lambda _contexts: [describe_module.FRAME_CONTEXT],
    )
    monkeypatch.setattr(
        describe_module.provider_state,
        "readiness_for_context",
        lambda _context, _interface: states.pop(0),
    )
    monkeypatch.setattr(
        describe_module, "callosum_send", lambda *_args, **_kwargs: None
    )

    with pytest.raises(SystemExit):
        describe_module._preflight_provider_readiness(
            video_path,
            day="20250101",
            segment="143022_300",
        )
    assert not video_path.with_suffix(".jsonl").exists()

    describe_module._preflight_provider_readiness(
        video_path,
        day="20250101",
        segment="143022_300",
    )
    assert not video_path.with_suffix(".jsonl").exists()

    sensor = FileSensor(tmp_path)
    sensor.register("*.webm", "describe", ["journal", "describe", "{file}"])
    to_process, _ = sensor.scan_unprocessed("20250101")

    assert [(path, handler) for path, handler, _command in to_process] == [
        (video_path, "describe")
    ]


def test_preflight_dedups_contexts_by_resolved_provider_model(monkeypatch):
    from solstone.think import models

    monkeypatch.setattr(
        models,
        "resolve_provider",
        lambda context, _interface: {
            "observe.describe.frame": ("google", "gemini-test"),
            "observe.describe.meeting": ("google", "gemini-test"),
            "observe.describe.terminal": ("anthropic", "claude-test"),
        }[context],
    )

    assert describe_module._dedup_readiness_contexts(
        [
            "observe.describe.frame",
            "observe.describe.meeting",
            "observe.describe.terminal",
        ]
    ) == ["observe.describe.frame", "observe.describe.terminal"]


@pytest.mark.asyncio
async def test_mid_run_blocker_unlinks_partial_output_and_does_not_retry(
    tmp_path, monkeypatch
):
    from solstone.think import batch as batch_module
    from solstone.think import models

    video_path = _video_path(tmp_path)
    output_path = video_path.with_suffix(".jsonl")
    emitted = []
    image_bytes = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(image_bytes, format="PNG")

    class FakeBatch:
        instances = []

        def __init__(self, max_concurrent=5, client=None):
            self.max_concurrent = max_concurrent
            self.client = client
            self.pending_tasks = set()
            self.requests = []
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
            if request not in self.requests:
                self.requests.append(request)

        def update(self, request, **kwargs):
            for key, value in kwargs.items():
                setattr(request, key, value)
            request.error = None
            request.reason_code = None
            self.add(request)

        async def drain_batch(self):
            for request in list(self.requests):
                request.error = "missing key"
                request.reason_code = "provider_key_missing"
                request.provider = "google"
                request.reset_at_ms = 12345
                yield request

    processor = describe_module.VideoProcessor.__new__(describe_module.VideoProcessor)
    processor.video_path = video_path
    processor.qualified_frames = []
    monkeypatch.setattr(
        processor,
        "process",
        lambda: [
            {
                "frame_id": 1,
                "timestamp": 0.0,
                "frame_bytes": image_bytes.getvalue(),
                "aruco": None,
            }
        ],
    )
    monkeypatch.setattr(batch_module, "Batch", FakeBatch)
    monkeypatch.setattr(
        models,
        "resolve_provider",
        lambda _context, _interface: ("google", "gemini-test"),
    )
    monkeypatch.setattr(
        describe_module,
        "callosum_send",
        lambda tract, event, **kwargs: emitted.append((tract, event, kwargs)),
    )

    with pytest.raises(SystemExit) as exc_info:
        await processor.process_with_vision(
            max_concurrent=1,
            output_path=output_path,
            work_key="20250101/143022_300/screen",
        )

    assert exc_info.value.code == EXIT_PROVIDER_BLOCKED
    assert not output_path.exists()
    assert not any(
        path.name.startswith(".describe_") or path.name.endswith(".tmp")
        for path in output_path.parent.iterdir()
    )
    assert FakeBatch.instances[0].add_count == 1
    assert len(emitted) == 1
    assert emitted[0][2]["key"] == "provider_key_missing:google:"
    assert emitted[0][2]["work_key"] == "20250101/143022_300/screen"
