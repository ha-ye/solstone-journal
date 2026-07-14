# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader, PdfWriter

TEXT_SENTINEL = "SOLPDF_SENTINEL_PAGE_2"
TEXT_RICH_SENTINEL = "SOLSTONE_TEXT_RICH_PAGE"
MIXED_TEXT_SENTINEL = "SOLSTONE_MIXED_TEXT_LAYER"
IMAGE_TEXT_SENTINEL = "SOLSTONE_TEXT_WITH_IMAGE_LAYER"
PAGE_WIDTH_PT = 612
PAGE_HEIGHT_PT = 792


def _pdf_literal(value: str) -> bytes:
    out = bytearray()
    for char in value:
        ordinal = ord(char)
        if char == "\\":
            out += b"\\\\"
        elif char == "(":
            out += b"\\("
        elif char == ")":
            out += b"\\)"
        elif char == "\n":
            out += b"\\n"
        elif char == "\r":
            out += b"\\r"
        elif char == "\t":
            out += b"\\t"
        elif ordinal < 32 or ordinal > 126:
            out += f"\\{ordinal:03o}".encode("ascii")
        else:
            out.append(ordinal)
    return b"(" + bytes(out) + b")"


def write_pdf(
    path: Path,
    pages: list[dict],
    metadata: dict[str, str] | None = None,
) -> Path:
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"PAGES_PLACEHOLDER",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    image_obj_id = None
    if any(page.get("image") for page in pages):
        image_obj_id = len(objects) + 1
        objects.append(
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length 3 >>\n"
            b"stream\n\xff\x00\x00\nendstream"
        )

    page_ids: list[int] = []
    pending_pages: list[tuple[int, int, bytes, bool]] = []
    for page in pages:
        content = bytearray()
        text = page.get("text")
        if text is not None:
            content += b"BT /F1 12 Tf 72 720 Td " + _pdf_literal(text) + b" Tj ET\n"
        image = page.get("image")
        if image:
            x, y, width, height = image
            content += f"q {width:g} 0 0 {height:g} {x:g} {y:g} cm /Im1 Do Q\n".encode(
                "ascii"
            )
        content_obj = (
            b"<< /Length %d >>\nstream\n" % len(content) + bytes(content) + b"endstream"
        )
        page_id = len(objects) + 1
        content_id = len(objects) + 2
        page_ids.append(page_id)
        pending_pages.append((page_id, content_id, content_obj, bool(image)))
        objects.append(b"PAGE_PLACEHOLDER")
        objects.append(content_obj)

    info_id = None
    if metadata:
        entries = [
            b"/" + key.encode("ascii") + b" " + _pdf_literal(value)
            for key, value in metadata.items()
        ]
        info_id = len(objects) + 1
        objects.append(b"<< " + b" ".join(entries) + b" >>")

    kids = b" ".join(f"{page_id} 0 R".encode("ascii") for page_id in page_ids)
    objects[1] = b"<< /Type /Pages /Kids [ " + kids + b" ] /Count %d >>" % len(page_ids)
    for page_id, content_id, _content_obj, has_image in pending_pages:
        resources = b"<< /Font << /F1 3 0 R >>"
        if has_image:
            resources += b" /XObject << /Im1 %d 0 R >>" % image_obj_id
        resources += b" >>"
        objects[page_id - 1] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            + b"/Resources "
            + resources
            + b" /Contents %d 0 R >>" % content_id
        )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    trailer = b"<< /Size %d /Root 1 0 R" % (len(objects) + 1)
    if info_id:
        trailer += b" /Info %d 0 R" % info_id
    trailer += b" >>"
    out += (
        b"trailer\n"
        + trailer
        + b"\nstartxref\n"
        + str(xref_offset).encode("ascii")
        + b"\n%%EOF\n"
    )
    path.write_bytes(bytes(out))
    return path


def write_text_fixture(path: Path) -> Path:
    return write_pdf(
        path,
        [
            {"text": "First page has ordinary extractable text."},
            {"text": f"Second page carries {TEXT_SENTINEL} for assertion."},
        ],
    )


def write_text_rich_fixture(path: Path) -> Path:
    return write_pdf(
        path,
        [
            {
                "text": (
                    f"{TEXT_RICH_SENTINEL} first page has more than fifty "
                    "non-whitespace characters for text-layer importer coverage."
                )
            },
            {
                "text": (
                    "Second rich text page also stays above the threshold so "
                    "the document importer makes no model calls."
                )
            },
        ],
    )


def write_image_only_fixture(path: Path) -> Path:
    return write_pdf(
        path,
        [
            {"image": (0, 0, PAGE_WIDTH_PT, PAGE_HEIGHT_PT)},
            {"image": (0, 0, PAGE_WIDTH_PT, PAGE_HEIGHT_PT)},
        ],
    )


def write_text_with_image_rich_fixture(path: Path) -> Path:
    return write_pdf(
        path,
        [
            {
                "text": (
                    f"{IMAGE_TEXT_SENTINEL} has a healthy text layer and a "
                    "large embedded image area that requires an overlay description."
                ),
                "image": (72, 144, 420, 420),
            },
            {
                "text": (
                    "Pure text companion page remains above the threshold and "
                    "must not trigger a model description."
                )
            },
        ],
    )


def write_importer_mixed_fixture(path: Path) -> Path:
    return write_pdf(
        path,
        [
            {
                "text": (
                    f"{MIXED_TEXT_SENTINEL} page has enough extractable text "
                    "to be emitted verbatim without model assistance."
                )
            },
            {"image": (0, 0, PAGE_WIDTH_PT, PAGE_HEIGHT_PT)},
            {
                "text": (
                    f"{IMAGE_TEXT_SENTINEL} mixed page has text plus a large "
                    "embedded image so the importer emits a description overlay."
                ),
                "image": (72, 144, 420, 420),
            },
        ],
    )


def write_mixed_fixture(path: Path) -> Path:
    return write_pdf(
        path,
        [
            {"text": "Pure text page with enough words to count."},
            {"image": (0, 0, PAGE_WIDTH_PT, PAGE_HEIGHT_PT)},
            {"text": "Text page with embedded image.", "image": (72, 144, 420, 420)},
        ],
    )


def write_whitespace_fixture(path: Path) -> Path:
    return write_pdf(path, [{"text": "   \t \n   "}])


def write_dates_fixture(path: Path) -> Path:
    return write_pdf(
        path,
        [{"text": "Date metadata fixture."}],
        {
            "Title": "Dated Fixture",
            "Author": "sol",
            "CreationDate": "D:20260304110200-07'00'",
            "ModDate": "D:20260304122233+02'30'",
            "Producer": "fixture",
        },
    )


def write_missing_dates_fixture(path: Path) -> Path:
    return write_pdf(
        path, [{"text": "No date metadata fixture."}], {"Title": "No Dates"}
    )


def write_garbled_dates_fixture(path: Path) -> Path:
    return write_pdf(
        path,
        [{"text": "Garbled date metadata fixture."}],
        {
            "Title": "Garbled Dates",
            "CreationDate": "D:20260304110200+99'00'",
            "ModDate": "D:20260304110200-07'99'",
        },
    )


def write_encrypted_fixture_pair(src: Path, user_path: Path, owner_path: Path) -> None:
    _encrypt_pdf(src, user_path, user_password="userpass", owner_password="ownerpass")
    _encrypt_pdf(src, owner_path, user_password="", owner_password="ownerpass")


def _encrypt_pdf(
    src: Path,
    dst: Path,
    *,
    user_password: str,
    owner_password: str,
) -> None:
    reader = PdfReader(str(src))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    if reader.metadata:
        writer.add_metadata(dict(reader.metadata))
    writer.encrypt(user_password=user_password, owner_password=owner_password)
    with dst.open("wb") as fh:
        writer.write(fh)


def write_truncation_fixtures(
    clean_path: Path,
    deep_path: Path,
    drop_startxref_path: Path,
    drop_eof_path: Path,
) -> None:
    write_pdf(
        clean_path,
        [{"text": f"Truncation sentinel page {index}"} for index in range(1, 6)],
    )
    clean_bytes = clean_path.read_bytes()
    xref_index = clean_bytes.find(b"\nxref\n")
    startxref_index = clean_bytes.rfind(b"startxref")
    eof_index = clean_bytes.rfind(b"%%EOF")
    deep_path.write_bytes(clean_bytes[: xref_index + 1])
    drop_startxref_path.write_bytes(clean_bytes[:startxref_index])
    drop_eof_path.write_bytes(clean_bytes[:eof_index])
