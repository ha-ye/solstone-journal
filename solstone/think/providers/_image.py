# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import base64
import io
import logging
import reprlib
from typing import Any

from PIL import Image, UnidentifiedImageError

LOG = logging.getLogger(__name__)

# llama.cpp mtmd decodes image inputs through stb_image. mtmd documents
# jpeg/png/tga/bmp/gif support, and ggml-org/llama.cpp#15542 tells clients to
# convert unsupported formats to PNG before sending. This encoder only emits
# PNG/JPEG/GIF/WEBP, so WebP is excluded from the local/STB lane.
STB_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif"})
CLOUD_IMAGE_MEDIA_TYPES = STB_IMAGE_MEDIA_TYPES | frozenset({"image/webp"})

_PIL_FORMATS = {
    "PNG": ("PNG", "image/png"),
    "JPEG": ("JPEG", "image/jpeg"),
    "GIF": ("GIF", "image/gif"),
    "WEBP": ("WEBP", "image/webp"),
}
_MEDIA_TYPE_FORMATS = {
    media_type: save_format for save_format, media_type in _PIL_FORMATS.values()
}


def is_image_part(part: Any) -> bool:
    return isinstance(part, Image.Image) or isinstance(part, bytes | bytearray)


def encode_image_part(
    part: Any, *, accepts: frozenset[str] = STB_IMAGE_MEDIA_TYPES
) -> tuple[str, str]:
    if isinstance(part, Image.Image):
        return _encode_pil_image(part, accepts=accepts)
    if isinstance(part, bytes | bytearray):
        return _encode_image_bytes(part, accepts=accepts)
    raise _image_error("unsupported image part", part)


def _part_repr(part: Any) -> str:
    return f"{type(part).__name__} {reprlib.repr(part)}"


def _image_error(message: str, part: Any) -> ValueError:
    return ValueError(f"{message}: {_part_repr(part)}")


def _accepted_types(accepts: frozenset[str]) -> str:
    return ", ".join(sorted(accepts))


def _not_sent_message(
    action: str,
    source_format: str,
    accepts: frozenset[str],
    detail: str | None = None,
) -> str:
    message = (
        f"{action} image source format {source_format or 'UNKNOWN'} "
        f"for accepted media types [{_accepted_types(accepts)}]; "
        "abandoned without sending image"
    )
    if detail:
        message = f"{message} ({detail})"
    return message


def _select_image_encoding(
    source_format: str,
    accepts: frozenset[str],
    part: Any,
) -> tuple[str, str]:
    normalized = source_format.upper()
    preferred = _PIL_FORMATS.get(normalized)
    if preferred is not None:
        save_format, media_type = preferred
        if media_type in accepts:
            return save_format, media_type
    if "image/png" in accepts:
        return "PNG", "image/png"
    raise _image_error(
        _not_sent_message("cannot encode", normalized, accepts),
        part,
    )


def _encode_pil_image(
    image: Image.Image, *, accepts: frozenset[str]
) -> tuple[str, str]:
    if image.width <= 0 or image.height <= 0:
        raise _image_error("cannot encode zero-size image part", image)

    source_format = (image.format or "").upper()
    save_format, media_type = _select_image_encoding(
        source_format,
        accepts,
        image,
    )
    prepared = _prepare_pil_image(image, save_format)

    buf = io.BytesIO()
    save_kwargs: dict[str, Any] = {}
    if save_format == "PNG":
        save_kwargs["compress_level"] = 1
    try:
        prepared.save(buf, format=save_format, **save_kwargs)
    except Exception as exc:
        raise _image_error(f"failed to encode image part ({exc})", image) from exc
    return media_type, base64.b64encode(buf.getvalue()).decode("ascii")


def _prepare_pil_image(image: Image.Image, save_format: str) -> Image.Image:
    if save_format == "PNG":
        if image.mode in {"RGB", "RGBA", "L"}:
            return image
        if image.mode in {"P", "LA"}:
            return image.convert("RGBA")
        raise _image_error(f"unsupported PIL mode for PNG: {image.mode}", image)

    if save_format == "JPEG":
        if image.mode in {"RGB", "L"}:
            return image
        raise _image_error(f"unsupported PIL mode for JPEG: {image.mode}", image)

    if save_format == "GIF":
        if image.mode in {"P", "L"}:
            return image
        raise _image_error(f"unsupported PIL mode for GIF: {image.mode}", image)

    if save_format == "WEBP":
        if image.mode in {"RGB", "RGBA"}:
            return image
        raise _image_error(f"unsupported PIL mode for WEBP: {image.mode}", image)

    raise _image_error(f"unsupported PIL format: {save_format}", image)


def _encode_image_bytes(
    part: bytes | bytearray, *, accepts: frozenset[str]
) -> tuple[str, str]:
    data = bytes(part)
    media_type = _sniff_image_media_type(data, part)
    source_format = _MEDIA_TYPE_FORMATS[media_type]
    _, accepted_media_type = _select_image_encoding(source_format, accepts, part)
    if accepted_media_type == media_type:
        return media_type, base64.b64encode(data).decode("ascii")
    image = _decode_image_bytes(data, source_format, accepts, part)
    return _encode_pil_image(image, accepts=accepts)


def _decode_image_bytes(
    data: bytes,
    source_format: str,
    accepts: frozenset[str],
    part: bytes | bytearray,
) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
    ) as exc:
        raise _image_error(
            _not_sent_message("cannot decode", source_format, accepts, str(exc)),
            part,
        ) from exc
    return image


def _sniff_image_media_type(data: bytes, part: bytes | bytearray) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise _image_error("unrecognized image bytes", part)


__all__ = [
    "CLOUD_IMAGE_MEDIA_TYPES",
    "STB_IMAGE_MEDIA_TYPES",
    "is_image_part",
    "encode_image_part",
]
