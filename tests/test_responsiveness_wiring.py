# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ast
import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

import solstone.think.models as models_module
from solstone.think.responsiveness import (
    NON_RESPONSIVE_OUTPUT_MESSAGE,
    NON_RESPONSIVE_REASON_CODE,
    NonResponsiveOutputError,
)

_REFUSAL = "I cannot describe this screen."
_USAGE = {
    "input_tokens": 1,
    "output_tokens": 1,
    "total_tokens": 2,
}


def _png_bytes() -> bytes:
    image_bytes = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(image_bytes, format="PNG")
    return image_bytes.getvalue()


def _describe_frame(frame_id: int, frame_bytes: bytes) -> dict:
    return {
        "frame_id": frame_id,
        "timestamp": float(frame_id),
        "frame_bytes": frame_bytes,
        "aruco": None,
    }


def _describe_processor(video_path: Path, frames: list[dict], monkeypatch) -> object:
    from solstone.observe import describe as describe_module

    processor = describe_module.VideoProcessor.__new__(describe_module.VideoProcessor)
    processor.video_path = video_path
    processor.first_hash = None
    processor.last_hash = None
    processor.qualified_count = len(frames)
    processor.qualified_frames = []
    monkeypatch.setattr(processor, "process", lambda: frames)
    return processor


def _declining_result() -> dict:
    return {
        "text": _REFUSAL,
        "model": "provider-model",
        "finish_reason": "stop",
        "usage": dict(_USAGE),
    }


def _install_declining_provider(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import solstone.think.providers as providers_package
    from solstone.think import batch as batch_module

    provider_module = SimpleNamespace(
        run_generate=MagicMock(return_value=_declining_result()),
        run_agenerate=AsyncMock(return_value=_declining_result()),
    )
    monkeypatch.setattr(
        models_module,
        "resolve_provider",
        lambda _interface: ("fake", "provider-model"),
    )
    monkeypatch.setattr(
        providers_package,
        "get_provider_module",
        lambda _provider: provider_module,
    )
    monkeypatch.setattr(
        batch_module,
        "resolve_provider",
        lambda _interface: ("fake", "provider-model"),
    )
    return provider_module


def _collect_provider_interface_call_sites() -> set[tuple[str, str, str]]:
    root_paths = [Path("solstone"), Path("core"), Path("packages"), Path("observers")]
    call_sites: set[tuple[str, str, str]] = set()
    for root in root_paths:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents: dict[ast.AST, ast.AST] = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                call_name = None
                if isinstance(func, ast.Attribute):
                    call_name = func.attr
                elif isinstance(func, ast.Name):
                    call_name = func.id
                if call_name is None or not call_name.endswith(
                    ("run_generate", "run_agenerate")
                ):
                    continue
                owner = "module"
                current = node
                while current in parents:
                    current = parents[current]
                    if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
                        owner = current.name
                        break
                if path == Path("solstone/think/models.py"):
                    continue
                if owner in {"run_generate", "run_agenerate"}:
                    continue
                call_sites.add((str(path), owner, call_name))
    return call_sites


def test_generate_provider_calls_outside_gate_are_only_known_probes():
    assert _collect_provider_interface_call_sites() == {
        ("solstone/think/providers/openhands.py", "_probe", "_run_generate"),
        ("solstone/think/providers/local.py", "validate_key", "run_generate"),
    }


def test_no_second_non_responsive_signal_table():
    identifiers = [
        "_NON_RESPONSIVE_" + suffix for suffix in ("NEGATION_HEADS", "LEAD_INS")
    ]
    matches: set[Path] = set()
    for path in Path("solstone").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(identifier in text for identifier in identifiers):
            matches.add(path)

    assert matches == {Path("solstone/think/responsiveness.py")}


def test_no_per_path_guard_non_responsive_property(tmp_path, monkeypatch):
    _install_declining_provider(monkeypatch)

    import solstone.think.providers as providers_package
    from solstone.observe import describe as describe_module
    from solstone.think import talents
    from solstone.think.importers import documents, images

    provider_module = providers_package.get_provider_module("fake")

    with pytest.raises(NonResponsiveOutputError):
        images._describe_image(Image.new("RGB", (8, 8), "red"))

    raster_path = tmp_path / "page.png"
    Image.new("RGB", (8, 8), "white").save(raster_path)
    document_outcome = documents._generate_for_page(
        prompt="describe this page",
        raster_path=raster_path,
        context="import.document.describe",
        stats=documents._RenderStats(),
    )
    assert document_outcome.text is None
    assert document_outcome.reason

    monkeypatch.setattr(talents, "_read_runtime_fingerprint", lambda: None)
    events: list[dict] = []
    config = {
        "name": "responsiveness-property",
        "output": "md",
        "output_path": str(tmp_path / "talent.md"),
        "prompt": "describe the screen",
    }
    asyncio.run(talents._execute_generate(config, events.append))
    assert not (tmp_path / "talent.md").exists()
    assert [event.get("event") for event in events] == ["error"]
    assert events[0]["reason_code"] == NON_RESPONSIVE_REASON_CODE

    provider_module.run_agenerate = AsyncMock(
        side_effect=[
            _declining_result(),
            {
                "text": (
                    '{"visual_description":"A code editor is open.",'
                    '"primary":"code","secondary":"none","overlap":true}'
                ),
                "model": "provider-model",
                "finish_reason": "stop",
            },
        ]
    )
    video_path = (
        tmp_path / "chronicle" / "20250101" / "default" / "143022_300" / "screen.webm"
    )
    video_path.parent.mkdir(parents=True)
    video_path.write_text("video", encoding="utf-8")
    frame_bytes = _png_bytes()
    processor = _describe_processor(
        video_path,
        [_describe_frame(1, frame_bytes), _describe_frame(2, frame_bytes)],
        monkeypatch,
    )
    monkeypatch.setattr(describe_module, "callosum_send", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        describe_module,
        "select_frames_for_extraction",
        lambda *_args, **_kwargs: [],
    )
    output_path = video_path.with_suffix(".jsonl")

    asyncio.run(
        processor.process_with_vision(
            max_concurrent=1,
            output_path=output_path,
            work_key="20250101/143022_300/screen",
        )
    )

    rows = [
        line for line in output_path.read_text(encoding="utf-8").splitlines() if line
    ]
    assert provider_module.run_agenerate.await_count == 2
    assert any(NON_RESPONSIVE_OUTPUT_MESSAGE in row for row in rows[1:])
