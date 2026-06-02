# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import json

import pytest
from PIL import Image

from solstone.observe import depict


def _segment_image(tmp_path):
    segment_dir = tmp_path / "chronicle" / "20240101" / "default" / "123456_300"
    segment_dir.mkdir(parents=True)
    image_path = segment_dir / "photo.png"
    Image.new("RGB", (4, 4), color="red").save(image_path)
    return image_path


def test_run_writes_image_jsonl_with_header_metadata(tmp_path, monkeypatch):
    image_path = _segment_image(tmp_path)
    monkeypatch.setenv("OBSERVER_NAME", "camera")
    monkeypatch.setenv(
        "SEGMENT_META", json.dumps({"stream": "default", "facet": "personal"})
    )
    calls = []

    def fake_generate(*, contents, context):
        calls.append((contents[0], contents[1].size, context))
        return "A concise image description"

    monkeypatch.setattr(depict, "generate", fake_generate)

    output_path = depict.run(image_path)

    assert output_path == image_path.with_suffix(".jsonl")
    assert len(calls) == 1
    assert calls[0][0] == depict._DESCRIBE_PROMPT
    assert calls[0][1] == (4, 4)
    assert calls[0][2] == "observe.depict"
    lines = output_path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    entry = json.loads(lines[1])
    assert header["raw"] == "photo.png"
    assert header["kind"] == "image"
    assert header["observer"] == "camera"
    assert header["stream"] == "default"
    assert header["facet"] == "personal"
    assert entry == {"start": "00:00:00", "text": "A concise image description"}


def test_run_skips_existing_output_unless_redo(tmp_path, monkeypatch):
    image_path = _segment_image(tmp_path)
    output_path = image_path.with_suffix(".jsonl")
    output_path.write_text("existing\n", encoding="utf-8")
    calls = []

    def fake_generate(*, contents, context):
        calls.append((contents, context))
        return "Replacement description"

    monkeypatch.setattr(depict, "generate", fake_generate)

    assert depict.run(image_path) is None
    assert output_path.read_text(encoding="utf-8") == "existing\n"
    assert calls == []

    assert depict.run(image_path, redo=True) == output_path
    assert len(calls) == 1
    assert "Replacement description" in output_path.read_text(encoding="utf-8")


def test_run_generate_failure_leaves_no_output(tmp_path, monkeypatch):
    image_path = _segment_image(tmp_path)

    def fail_generate(*, contents, context):
        raise RuntimeError("model failed")

    monkeypatch.setattr(depict, "generate", fail_generate)

    with pytest.raises(RuntimeError, match="model failed"):
        depict.run(image_path)

    assert not image_path.with_suffix(".jsonl").exists()
