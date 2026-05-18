# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import base64
import io

import pytest
from PIL import Image

from solstone.think.providers._image import encode_image_part, is_image_part


def _png_bytes(size: tuple[int, int] = (4, 3)) -> bytes:
    image = Image.new("RGB", size, color="red")
    buf = io.BytesIO()
    image.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def _decoded_image(b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(b64)))


def test_is_image_part_accepts_pil_bytes_and_bytearray():
    image = Image.new("RGB", (2, 2), color="blue")

    assert is_image_part(image) is True
    assert is_image_part(_png_bytes()) is True
    assert is_image_part(bytearray(_png_bytes())) is True
    assert is_image_part("not an image") is False


def test_encode_pil_without_format_defaults_to_png_round_trip():
    image = Image.new("RGB", (5, 4), color="green")

    media_type, b64 = encode_image_part(image)

    decoded = _decoded_image(b64)
    assert media_type == "image/png"
    assert decoded.size == image.size
    assert decoded.format == "PNG"


def test_encode_pil_jpeg_preserves_format_round_trip():
    source = Image.new("RGB", (6, 4), color="purple")
    buf = io.BytesIO()
    source.save(buf, format="JPEG")
    image = Image.open(io.BytesIO(buf.getvalue()))

    media_type, b64 = encode_image_part(image)

    decoded = _decoded_image(b64)
    assert media_type == "image/jpeg"
    assert decoded.size == image.size
    assert decoded.format == "JPEG"


@pytest.mark.parametrize("part_type", [bytes, bytearray])
def test_encode_png_bytes_sniffs_media_type_and_preserves_bytes(part_type):
    data = _png_bytes((7, 5))

    media_type, b64 = encode_image_part(part_type(data))

    assert media_type == "image/png"
    assert base64.b64decode(b64) == data
    decoded = _decoded_image(b64)
    assert decoded.size == (7, 5)
    assert decoded.format == "PNG"


def test_unknown_bytes_raise_with_part_type_and_repr():
    with pytest.raises(ValueError) as exc_info:
        encode_image_part(b"not-an-image")

    message = str(exc_info.value)
    assert "bytes" in message
    assert "not-an-image" in message


def test_cmyk_image_raises_with_part_type_and_repr():
    image = Image.new("CMYK", (2, 2))

    with pytest.raises(ValueError) as exc_info:
        encode_image_part(image)

    message = str(exc_info.value)
    assert "Image" in message
    assert "CMYK" in message
