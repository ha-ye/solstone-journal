# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json
import logging
import subprocess
from pathlib import Path

import pytest

from solstone.observe import detect
from solstone.think.providers.rfdetr_install import (
    ENGINE_REF,
    MODEL_NAME,
    RfdetrPaths,
)


@pytest.fixture(autouse=True)
def _reset_detect_state():
    detect._disabled = False
    yield
    detect._disabled = False


def _canned_cli_json() -> dict:
    return {
        "image": {"width": 100, "height": 50},
        "detections": [
            {
                "class_id": 1,
                "class_name": "cup",
                "score": 0.72,
                "bbox": [1, 2, 3, 4],
            }
        ],
    }


def test_detect_objects_missing_provider_latches_without_reattempt(monkeypatch, caplog):
    calls = 0

    def fake_paths():
        nonlocal calls
        calls += 1
        return RfdetrPaths(status="not_installed")

    monkeypatch.setattr(detect, "rfdetr_paths", fake_paths)
    caplog.set_level(logging.WARNING, logger=detect.LOG.name)

    assert detect.detect_objects(b"x") is None
    assert detect.detect_objects(b"x") is None

    warnings = [
        record
        for record in caplog.records
        if record.name == detect.LOG.name and record.levelno == logging.WARNING
    ]
    assert detect._disabled is True
    assert len(warnings) == 1
    assert warnings[0].getMessage() == (
        "object detection disabled: rf-detr provider not_installed"
    )
    assert calls == 1


def test_detect_objects_returns_parsed_cli_json(monkeypatch, tmp_path):
    canned = _canned_cli_json()
    argv_seen = []

    monkeypatch.setattr(
        detect,
        "rfdetr_paths",
        lambda: RfdetrPaths(
            status="installed",
            binary_path=tmp_path / "rfdetr-cli",
            model_path=tmp_path / "model.gguf",
        ),
    )

    def fake_run(argv, **kwargs):
        argv_seen.append((argv, kwargs))
        output_path = Path(argv[argv.index("--output") + 1])
        output_path.write_text(json.dumps(canned), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(detect.subprocess, "run", fake_run)

    assert detect.detect_objects(b"png-bytes") == canned
    assert len(argv_seen) == 1
    argv, kwargs = argv_seen[0]
    assert argv[1] == "detect"
    assert argv[argv.index("--threshold") + 1] == str(detect.THRESHOLD)
    assert kwargs["timeout"] == detect._TIMEOUT_S
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is True


def test_detect_objects_subprocess_failure_latches(monkeypatch, tmp_path):
    monkeypatch.setattr(
        detect,
        "rfdetr_paths",
        lambda: RfdetrPaths(
            status="installed",
            binary_path=tmp_path / "rfdetr-cli",
            model_path=tmp_path / "model.gguf",
        ),
    )

    def fake_run(argv, **_kwargs):
        raise subprocess.CalledProcessError(1, argv, stderr=b"bad")

    monkeypatch.setattr(detect.subprocess, "run", fake_run)

    assert detect.detect_objects(b"png-bytes") is None
    assert detect._disabled is True


def test_detections_block_renames_and_preserves_provenance():
    canned = _canned_cli_json()

    assert detect.detections_block(canned, source="screen", gate="primary:media") == {
        "engine": detect.ENGINE_NAME,
        "engine_ref": ENGINE_REF,
        "model": MODEL_NAME,
        "threshold": detect.THRESHOLD,
        "source": "screen",
        "gate": "primary:media",
        "image": canned["image"],
        "objects": canned["detections"],
    }


def test_screen_gate_prefers_primary_gate():
    assert detect.screen_gate({"primary": "media", "secondary": "none"}) == (
        "primary:media"
    )
    assert detect.screen_gate({"primary": "code", "secondary": "social"}) == (
        "secondary:social"
    )
    assert detect.screen_gate({"primary": "media", "secondary": "social"}) == (
        "primary:media"
    )
    assert detect.screen_gate({"primary": "code", "secondary": "terminal"}) is None


def test_qualified_objects_filters_at_read_time_only():
    tv = {"class_name": "tv", "score": 0.7}
    weak_person = {"class_name": "person", "score": 0.39}
    person = {"class_name": "person", "score": 0.41}
    cup = {"class_name": "cup", "score": 0.41}
    sandwich = {"class_name": "sandwich", "score": 0.9}

    assert detect.qualified_objects({"source": "screen", "objects": [tv]}) == []
    assert detect.qualified_objects({"source": "still", "objects": [tv]}) == [tv]
    assert detect.qualified_objects(
        {"source": "still", "objects": [weak_person, person, cup, sandwich]}
    ) == [person, cup]
