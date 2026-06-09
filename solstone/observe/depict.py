# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Segment still-image description handler."""

import argparse
import json
import logging
import os
from pathlib import Path

from PIL import Image

from solstone.observe.utils import get_segment_key, resize_for_vlm
from solstone.think.journal_io import write_jsonl
from solstone.think.models import generate
from solstone.think.utils import require_solstone, setup_cli

logger = logging.getLogger(__name__)

_DESCRIBE_PROMPT = (
    "Describe this image in detail. Include any visible text, people, objects, "
    "setting, and notable context. Return a concise natural-language description."
)


def _build_header(raw_name: str, kind: str) -> dict:
    header = {"raw": raw_name, "kind": kind}

    observer = os.getenv("OBSERVER_NAME")
    if observer:
        header["observer"] = observer

    segment_meta_str = os.getenv("SEGMENT_META")
    if segment_meta_str:
        try:
            segment_meta = json.loads(segment_meta_str)
            for key, value in segment_meta.items():
                header[key] = value
        except json.JSONDecodeError:
            logger.warning("Invalid SEGMENT_META JSON: %s", segment_meta_str[:100])

    return header


def run(image_path: Path, *, redo: bool = False) -> Path | None:
    output_path = image_path.with_suffix(".jsonl")
    if output_path.exists() and not redo:
        logger.info("Already processed: %s", image_path)
        return None

    with Image.open(image_path) as img:
        img.load()
        prepared = resize_for_vlm(img)
        description = generate(
            contents=[_DESCRIBE_PROMPT, prepared], context="observe.depict"
        ).strip()

    header = _build_header(image_path.name, "image")
    entry = {"start": "00:00:00", "text": description}
    write_jsonl(output_path, [header, entry])
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Describe a segment image")
    parser.add_argument("image_path", help="Path to image in a segment directory")
    parser.add_argument("--redo", action="store_true", help="Reprocess existing output")
    args = setup_cli(parser)
    require_solstone()

    image_path = Path(args.image_path)
    if not image_path.exists():
        parser.error(f"Image not found: {image_path}")
    if get_segment_key(image_path) is None:
        parser.error(
            "Image must be in a segment directory (HHMMSS_LEN/), "
            f"but parent is: {image_path.parent.name}"
        )

    try:
        run(image_path, redo=args.redo)
    except Exception as exc:
        logger.error("Failed to process %s: %s", image_path, exc, exc_info=True)
        raise


if __name__ == "__main__":
    main()
