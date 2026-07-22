# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import base64
import io

import pytest
from PIL import Image

from solstone.think.providers._image import (
    CLOUD_IMAGE_MEDIA_TYPES,
    STB_IMAGE_MEDIA_TYPES,
    encode_image_part,
    is_image_part,
)


def _png_bytes(size: tuple[int, int] = (4, 3)) -> bytes:
    image = Image.new("RGB", size, color="red")
    buf = io.BytesIO()
    image.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def _image_bytes(source_format: str, size: tuple[int, int] = (4, 3)) -> bytes:
    image = Image.new("RGB", size, color="red")
    buf = io.BytesIO()
    image.save(buf, format=source_format)
    return buf.getvalue()


def _pil_image(source_format: str, size: tuple[int, int] = (4, 3)) -> Image.Image:
    image = Image.open(io.BytesIO(_image_bytes(source_format, size)))
    image.load()
    return image


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


def test_default_webp_pil_transcodes_to_png():
    image = _pil_image("WEBP", (5, 4))

    media_type, b64 = encode_image_part(image)

    decoded = _decoded_image(b64)
    assert media_type == "image/png"
    assert decoded.size == image.size
    assert decoded.format == "PNG"


def test_stb_webp_pil_transcodes_to_png():
    image = _pil_image("WEBP", (5, 4))

    media_type, b64 = encode_image_part(image, accepts=STB_IMAGE_MEDIA_TYPES)

    decoded = _decoded_image(b64)
    assert media_type == "image/png"
    assert decoded.size == image.size
    assert decoded.format == "PNG"


def test_cloud_webp_pil_stays_webp():
    image = _pil_image("WEBP", (5, 4))

    media_type, b64 = encode_image_part(image, accepts=CLOUD_IMAGE_MEDIA_TYPES)

    decoded = _decoded_image(b64)
    assert media_type == "image/webp"
    assert decoded.size == image.size
    assert decoded.format == "WEBP"


def test_webp_bytes_stb_transcodes_and_cloud_preserves_bytes():
    data = _image_bytes("WEBP", (5, 4))

    stb_media_type, stb_b64 = encode_image_part(data, accepts=STB_IMAGE_MEDIA_TYPES)
    cloud_media_type, cloud_b64 = encode_image_part(
        data,
        accepts=CLOUD_IMAGE_MEDIA_TYPES,
    )

    stb_decoded = _decoded_image(stb_b64)
    assert stb_media_type == "image/png"
    assert stb_decoded.size == (5, 4)
    assert stb_decoded.format == "PNG"
    assert cloud_media_type == "image/webp"
    assert base64.b64decode(cloud_b64) == data


@pytest.mark.parametrize(
    ("source_format", "media_type"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg")],
)
@pytest.mark.parametrize("accepts", [STB_IMAGE_MEDIA_TYPES, CLOUD_IMAGE_MEDIA_TYPES])
def test_png_and_jpeg_bytes_preserved_byte_for_byte_under_accept_sets(
    source_format: str,
    media_type: str,
    accepts: frozenset[str],
):
    data = _image_bytes(source_format, (5, 4))

    encoded_media_type, b64 = encode_image_part(data, accepts=accepts)

    assert encoded_media_type == media_type
    assert base64.b64decode(b64) == data


def test_tiff_pil_transcodes_to_png_and_tiff_bytes_still_raise():
    image = _pil_image("TIFF", (5, 4))

    for accepts in [STB_IMAGE_MEDIA_TYPES, CLOUD_IMAGE_MEDIA_TYPES]:
        media_type, b64 = encode_image_part(image, accepts=accepts)
        decoded = _decoded_image(b64)
        assert media_type == "image/png"
        assert decoded.size == image.size
        assert decoded.format == "PNG"

    with pytest.raises(ValueError, match="unrecognized image bytes"):
        encode_image_part(_image_bytes("TIFF", (5, 4)))


@pytest.mark.parametrize("mode", ["CMYK", "I;16"])
def test_unsupported_pil_modes_still_raise(mode: str):
    image = Image.new(mode, (2, 2))

    with pytest.raises(ValueError) as exc_info:
        encode_image_part(image)

    message = str(exc_info.value)
    assert f"unsupported PIL mode for PNG: {mode}" in message


def test_accepts_constants_include_png_and_successes_stay_inside_accepts():
    assert "image/png" in STB_IMAGE_MEDIA_TYPES
    assert "image/png" in CLOUD_IMAGE_MEDIA_TYPES
    parts = [
        _image_bytes("PNG", (3, 2)),
        _image_bytes("JPEG", (3, 2)),
        _image_bytes("GIF", (3, 2)),
        _image_bytes("WEBP", (3, 2)),
        _pil_image("PNG", (3, 2)),
        _pil_image("JPEG", (3, 2)),
        _pil_image("GIF", (3, 2)),
        _pil_image("WEBP", (3, 2)),
        _pil_image("TIFF", (3, 2)),
    ]

    for accepts in [
        STB_IMAGE_MEDIA_TYPES,
        CLOUD_IMAGE_MEDIA_TYPES,
        frozenset({"image/png"}),
    ]:
        for part in parts:
            media_type, _ = encode_image_part(part, accepts=accepts)
            assert media_type in accepts


def test_selection_failure_message_names_format_accepts_and_not_sent():
    image = _pil_image("WEBP", (3, 2))

    with pytest.raises(ValueError) as exc_info:
        encode_image_part(image, accepts=frozenset({"image/jpeg"}))

    message = str(exc_info.value)
    assert "cannot encode image source format WEBP" in message
    assert "accepted media types [image/jpeg]" in message
    assert "abandoned without sending image" in message


def test_corrupt_webp_decode_failure_message_names_format_accepts_and_not_sent():
    corrupt_webp = b"RIFF\x00\x00\x00\x00WEBPnot-a-real-webp"

    with pytest.raises(ValueError) as exc_info:
        encode_image_part(corrupt_webp)

    message = str(exc_info.value)
    assert "cannot decode image source format WEBP" in message
    assert "accepted media types [image/gif, image/jpeg, image/png]" in message
    assert "abandoned without sending image" in message
