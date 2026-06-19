# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import datetime as dt
import os
from pathlib import Path

import pytest
from PIL import Image

import solstone.think.utils as think_utils
from solstone.think.importers.file_importer import (
    FILE_IMPORTER_REGISTRY,
    get_file_importer,
)


def _configure_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    think_utils._journal_path_cache = None


def _write_png(path: Path) -> None:
    Image.new("RGB", (8, 8), "red").save(path)


def test_detect_image_files(tmp_path):
    mod = __import__("solstone.think.importers.images", fromlist=["importer"])

    for suffix in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff"]:
        path = tmp_path / f"image{suffix}"
        path.write_bytes(b"placeholder")
        assert mod.importer.detect(path) is True

    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4")
    assert mod.importer.detect(tmp_path / "note.txt") is False
    assert mod.importer.detect(tmp_path / "doc.pdf") is False
    assert mod.importer.detect(tmp_path) is False
    assert mod.importer.detect(tmp_path / "missing.png") is False


def test_preview_readable_png(tmp_path):
    mod = __import__("solstone.think.importers.images", fromlist=["importer"])
    image_path = tmp_path / "shot.png"
    _write_png(image_path)

    preview = mod.importer.preview(image_path)

    assert preview.item_count == 1
    assert preview.entity_count == 0
    assert preview.summary == "1 image (PNG, 8×8)"


def test_process_image_success(tmp_path, monkeypatch):
    mod = __import__("solstone.think.importers.images", fromlist=["importer"])
    _configure_journal(tmp_path, monkeypatch)
    image_path = tmp_path / "shot.png"
    _write_png(image_path)
    ts = dt.datetime(2026, 1, 15, 12, 0, 0).timestamp()
    os.utime(image_path, (ts, ts))
    monkeypatch.setattr(
        mod,
        "_describe_image",
        lambda image: "A red square.\n\nVisible text: none.",
    )

    result = mod.importer.process(image_path, tmp_path, import_id="20260115_120000")

    day = dt.datetime.fromtimestamp(ts).strftime("%Y%m%d")
    segment_root = tmp_path / "chronicle" / day / "import.image"
    segment_dirs = [path for path in segment_root.iterdir() if path.is_dir()]
    assert len(segment_dirs) == 1
    segment_dir = segment_dirs[0]
    md_path = segment_dir / "image_transcript.md"
    assert md_path.exists()
    assert "A red square." in md_path.read_text(encoding="utf-8")
    assert (segment_dir / "original.png").exists()
    assert result.entries_written == 1
    assert result.entities_seeded == 0
    assert result.files_created == [str(md_path)]
    assert result.segments == [(day, "120000_0")]


def test_process_undecodable_image_raises_before_segment(tmp_path, monkeypatch):
    mod = __import__("solstone.think.importers.images", fromlist=["importer"])
    _configure_journal(tmp_path, monkeypatch)
    image_path = tmp_path / "bad.png"
    image_path.write_bytes(b"not an image")

    with pytest.raises(ValueError, match="Cannot decode image bad.png"):
        mod.importer.process(image_path, tmp_path, import_id="20260115_120000")

    assert not list((tmp_path / "chronicle").glob("**/import.image"))


def test_process_vision_failure_propagates_before_success_entry(tmp_path, monkeypatch):
    mod = __import__("solstone.think.importers.images", fromlist=["importer"])
    _configure_journal(tmp_path, monkeypatch)
    image_path = tmp_path / "shot.png"
    _write_png(image_path)

    def fail_description(image):
        raise RuntimeError("vision failed")

    monkeypatch.setattr(mod, "_describe_image", fail_description)

    with pytest.raises(RuntimeError, match="vision failed"):
        mod.importer.process(image_path, tmp_path, import_id="20260115_120000")

    assert not list((tmp_path / "chronicle").glob("**/import.image"))


def test_registry_entry():
    assert FILE_IMPORTER_REGISTRY["image"] == "solstone.think.importers.images"
    importer = get_file_importer("image")
    assert importer is not None
    assert importer.name == "image"
