# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _pdf_literal(value: str) -> bytes:
    out = bytearray()
    for char in value:
        if char == "\\":
            out += b"\\\\"
        elif char == "(":
            out += b"\\("
        elif char == ")":
            out += b"\\)"
        else:
            out += char.encode("ascii")
    return b"(" + bytes(out) + b")"


def _write_text_pdf(path: Path, text: str) -> Path:
    content = b"BT /F1 12 Tf 72 720 Td " + _pdf_literal(text) + b" Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [ 4 0 R ] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
    ]
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
    out += (
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode("ascii")
        + b"\n%%EOF\n"
    )
    path.write_bytes(bytes(out))
    return path


@pytest.fixture
def client(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    (journal / "config").mkdir(parents=True)
    (journal / "config" / "journal.json").write_text(
        json.dumps({"setup": {"completed_at": 1700000000000}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(journal))

    from solstone.convey import create_app

    app = create_app(journal=str(journal))
    app.config.update(TESTING=True)
    return app.test_client()


def test_import_index_serves_injected_spa_shell(client):
    response = client.get("/app/import/")

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_import_detail_serves_spa_shell_even_when_missing(client):
    response = client.get("/app/import/missing-import")

    assert response.status_code == 200
    assert b'data-solstone-shell="spa"' in response.data


def test_import_missing_detail_api_still_returns_not_found(client):
    response = client.get("/app/import/api/missing-import")

    assert response.status_code == 404
    assert response.get_json()["reason_code"] == "import_not_found"


def test_import_detail_api_path_resolves(client):
    adapter = client.application.url_map.bind("localhost")

    endpoint, _args = adapter.match("/app/import/api/missing-import", method="GET")

    assert endpoint == "app:import.import_detail_api"


def test_document_upload_stages_emits_command_and_imports_new_shape_segment(
    client, tmp_path, monkeypatch
):
    import importlib

    import_routes = importlib.import_module("solstone.apps.import.routes")
    cli_mod = importlib.import_module("solstone.think.importers.cli")
    doc_mod = importlib.import_module("solstone.think.importers.documents")
    journal = Path(import_routes.state.journal_root)
    source_pdf = _write_text_pdf(
        tmp_path / "upload.pdf",
        (
            "Route upload document has enough extractable text for the worker-backed "
            "document importer to create a new transcript."
        ),
    )
    emitted: list[dict] = []

    monkeypatch.setattr(
        import_routes,
        "emit",
        lambda tract, event, **kwargs: emitted.append(
            {"tract": tract, "event": event, **kwargs}
        ),
    )
    monkeypatch.setattr(cli_mod, "CallosumConnection", lambda **kwargs: MagicMock())
    monkeypatch.setattr(cli_mod, "_status_emitter", lambda: None)
    monkeypatch.setattr(cli_mod, "index_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        doc_mod,
        "generate",
        lambda *, contents, context, **kwargs: pytest.fail("unexpected model call"),
    )

    save_response = client.post(
        "/app/import/api/save",
        data={
            "file": (BytesIO(source_pdf.read_bytes()), "contract.pdf"),
            "client_item_id": "document-upload",
            "facet": "work",
            "setting": "review",
            "source_hint": "document",
        },
        content_type="multipart/form-data",
    )

    assert save_response.status_code == 200
    saved = save_response.get_json()
    timestamp = saved["timestamp"]
    staged_pdf = journal / "imports" / timestamp / "contract.pdf"
    import_json = journal / "imports" / timestamp / "import.json"
    assert staged_pdf.read_bytes() == source_pdf.read_bytes()
    assert json.loads(import_json.read_text(encoding="utf-8"))["source_hint"] == (
        "document"
    )

    start_response = client.post(
        "/app/import/api/start",
        json={"path": str(staged_pdf), "timestamp": timestamp, "force": True},
    )

    assert start_response.status_code == 200
    assert emitted
    cmd = emitted[-1]["cmd"]
    assert cmd == [
        "journal",
        "importer",
        str(staged_pdf),
        timestamp,
        "--facet",
        "work",
        "--setting",
        "review",
        "--source",
        "document",
        "--force",
    ]

    cli_argv = [cmd[0], cmd[2], "--timestamp", cmd[3], *cmd[4:]]
    monkeypatch.setattr(sys, "argv", cli_argv)
    monkeypatch.setenv("SOL_SKIP_SUPERVISOR_CHECK", "1")
    cli_mod.main()

    segments = list((journal / "chronicle").glob("*/import.document/*"))
    assert len(segments) == 1
    segment_dir = segments[0]
    transcript = segment_dir / "document_transcript.md"
    assert (segment_dir / "original.pdf").read_bytes() == source_pdf.read_bytes()
    assert transcript.is_file()
    text = transcript.read_text(encoding="utf-8")
    assert "**Type:** Document" in text
    assert "**Extraction:" in text
    assert "Route upload document has enough extractable text" in text
    assert doc_mod.MARKER_MODEL_EXTRACTED.split("{NNNN}", 1)[0] not in text
