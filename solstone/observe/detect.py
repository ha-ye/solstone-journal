# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""local rf-detr.cpp object detection + read-side qualification policy."""

import json
import logging
import subprocess
import tempfile
from pathlib import Path

from solstone.think.providers.rfdetr_install import (
    ENGINE_REF,
    MODEL_NAME,
    rfdetr_paths,
)

LOG = logging.getLogger(__name__)

ENGINE_NAME = "rf-detr.cpp"
THRESHOLD = 0.25
_TIMEOUT_S = 120.0
_GATE_CATEGORIES = frozenset({"media", "social"})

_disabled = False


def _disable(reason: str) -> None:
    global _disabled
    if _disabled:
        return
    _disabled = True
    LOG.warning("object detection disabled: %s", reason)


def detect_objects(image_bytes: bytes, *, threads: int = 4) -> dict | None:
    if _disabled:
        return None
    try:
        paths = rfdetr_paths()
        if paths.status != "installed":
            _disable(f"rf-detr provider {paths.status}")
            return None
        with tempfile.TemporaryDirectory(prefix="rfdetr_") as td:
            input_png = Path(td) / "input.png"
            input_png.write_bytes(image_bytes)
            output_json = Path(td) / "output.json"
            subprocess.run(
                [
                    str(paths.binary_path),
                    "detect",
                    "--model",
                    str(paths.model_path),
                    "--input",
                    str(input_png),
                    "--output",
                    str(output_json),
                    "--threshold",
                    str(THRESHOLD),
                    "--threads",
                    str(threads),
                ],
                timeout=_TIMEOUT_S,
                capture_output=True,
                check=True,
            )
            parsed = json.loads(output_json.read_text(encoding="utf-8"))
            return parsed
    except Exception as exc:
        _disable(str(exc))
        return None


def detections_block(result: dict, *, source: str, gate: str) -> dict:
    return {
        "engine": ENGINE_NAME,
        "engine_ref": ENGINE_REF,
        "model": MODEL_NAME,
        "threshold": THRESHOLD,
        "source": source,
        "gate": gate,
        "image": result["image"],
        "objects": result["detections"],
    }


def screen_gate(analysis: dict) -> str | None:
    primary = analysis.get("primary")
    secondary = analysis.get("secondary")
    if primary in _GATE_CATEGORIES:
        return f"primary:{primary}"
    if secondary in _GATE_CATEGORIES:
        return f"secondary:{secondary}"
    return None


# Stored `detections` rows are UNFILTERED — raw CLI output at THRESHOLD.
# These constants filter at READ time only; the write side stores everything.
_DEVICE_CLASSES = frozenset({"laptop", "tv", "cell phone"})
_PERSON_CLASS = "person"
_PERSON_MIN_SCORE = 0.4
_ALLOWED_CLASSES = frozenset({"car", "bird", "tie", "bottle", "cup", "truck", "bowl"})
_ALLOWED_MIN_SCORE = 0.4


def qualified_objects(block: dict) -> list[dict]:
    source = block.get("source")
    kept: list[dict] = []
    for obj in block.get("objects", []):
        name = obj.get("class_name")
        score = obj.get("score", 0.0)
        if source == "screen" and name in _DEVICE_CLASSES:
            continue
        if name == _PERSON_CLASS:
            if score >= _PERSON_MIN_SCORE:
                kept.append(obj)
            continue
        if name in _ALLOWED_CLASSES or name in _DEVICE_CLASSES:
            if score >= _ALLOWED_MIN_SCORE:
                kept.append(obj)
            continue
    return kept
