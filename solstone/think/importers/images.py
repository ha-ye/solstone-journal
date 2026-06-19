# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Image importer with vision description."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Callable

from PIL import Image, UnidentifiedImageError

from solstone.think.importers.file_importer import ImportPreview, ImportResult
from solstone.think.importers.shared import install_source_file, write_content_manifest
from solstone.think.journal_io import write_text
from solstone.think.models import generate
from solstone.think.utils import day_path

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tiff"}
_VISION_PROMPT = (
    "Describe what is in this image faithfully and concisely. "
    "Transcribe any legible text verbatim. Return clean markdown."
)


def _describe_image(image: Image.Image) -> str:
    """Describe an image using the vision model."""
    description = generate(
        contents=[_VISION_PROMPT, image], context="import.image.vision"
    ).strip()
    if not description:
        raise RuntimeError("Vision produced no description for image")
    return description


def _render_image_markdown(title: str, description: str, meta: dict) -> str:
    """Render image description as markdown."""
    lines = [f"# {title}", "", "**Type:** Image"]
    if meta.get("format"):
        lines.append(f"**Format:** {meta['format']}")
    if meta.get("width") is not None and meta.get("height") is not None:
        lines.append(f"**Dimensions:** {meta['width']}×{meta['height']}")
    if meta.get("date"):
        lines.append(f"**Date:** {meta['date']}")
    lines.extend(["", "---", "", description.strip()])
    return "\n".join(lines).rstrip() + "\n"


class ImageImporter:
    name = "image"
    display_name = "Image"
    file_patterns = ["*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif", "*.tiff"]
    description = "Import a single image and describe its contents with vision"

    def detect(self, path: Path) -> bool:
        return path.is_file() and path.suffix.lower() in _IMAGE_EXTS

    def preview(self, path: Path) -> ImportPreview:
        try:
            with Image.open(path) as image:
                fmt = image.format or path.suffix.lower().lstrip(".").upper()
                width, height = image.size
            ts = path.stat().st_mtime
        except Exception:
            return ImportPreview(
                date_range=("", ""),
                item_count=0,
                entity_count=0,
                summary="No readable image found",
            )

        day = dt.datetime.fromtimestamp(ts).strftime("%Y%m%d")
        return ImportPreview(
            date_range=(day, day),
            item_count=1,
            entity_count=0,
            summary=f"1 image ({fmt}, {width}×{height})",
        )

    def process(
        self,
        path: Path,
        journal_root: Path,
        *,
        facet: str | None = None,
        import_id: str | None = None,
        progress_callback: Callable[..., None] | None = None,
        dry_run: bool = False,
    ) -> ImportResult:
        import_id = import_id or dt.datetime.now().strftime("%Y%m%d_%H%M%S")

        try:
            image = Image.open(path)
            image.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError(f"Cannot decode image {path.name}: {exc}") from exc

        try:
            ts = path.stat().st_mtime
        except OSError:
            ts = dt.datetime.now().timestamp()

        seg_dt = dt.datetime.fromtimestamp(ts)
        day = seg_dt.strftime("%Y%m%d")
        seg_key = f"{seg_dt.strftime('%H%M%S')}_0"
        title = path.stem
        fmt = image.format or path.suffix.lower().lstrip(".").upper()
        width = image.width
        height = image.height
        date_str = seg_dt.strftime("%Y-%m-%d")

        description = _describe_image(image)

        segment_dir = day_path(day) / "import.image" / seg_key
        segment_dir.mkdir(parents=True, exist_ok=True)

        install_source_file(path, segment_dir / f"original{path.suffix.lower()}")
        md_path = segment_dir / "image_transcript.md"
        write_text(
            md_path,
            _render_image_markdown(
                title,
                description,
                {"format": fmt, "width": width, "height": height, "date": date_str},
            ),
        )

        entry = {
            "id": "image-0",
            "title": title,
            "date": day,
            "type": "image",
            "preview": description[:200],
            "meta": {"format": fmt, "width": width, "height": height},
            "segments": [{"day": day, "key": seg_key}],
        }
        write_content_manifest(import_id, [entry])

        if progress_callback:
            progress_callback(
                1,
                1,
                earliest_date=day,
                latest_date=day,
                entities_found=0,
            )

        return ImportResult(
            entries_written=1,
            entities_seeded=0,
            files_created=[str(md_path)],
            errors=[],
            summary="Imported 1 image into 1 segment",
            segments=[(day, seg_key)],
            date_range=(day, day),
        )


importer = ImageImporter()
