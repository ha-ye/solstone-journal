# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from solstone.observe.pdf_worker import (
    ENV_RLIMIT_AS_MB,
    PdfWorkerCorruptError,
    PdfWorkerEncryptedError,
    PdfWorkerEngineError,
    PdfWorkerRenderIOError,
    PdfWorkerTimeoutError,
    run_pdf_worker,
)
from tests.pdf_worker_fixtures import (
    PAGE_HEIGHT_PT,
    PAGE_WIDTH_PT,
    TEXT_SENTINEL,
    write_dates_fixture,
    write_encrypted_fixture_pair,
    write_garbled_dates_fixture,
    write_image_only_fixture,
    write_missing_dates_fixture,
    write_mixed_fixture,
    write_text_fixture,
    write_truncation_fixtures,
    write_whitespace_fixture,
)


def _run_worker(*args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "solstone.observe.pdf_worker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _single_json(result: subprocess.CompletedProcess[str]) -> dict:
    lines = result.stdout.splitlines()
    assert len(lines) == 1, (
        f"expected exactly one stdout JSON line\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    payload = json.loads(lines[0])
    assert payload["schema"] == "sol-pdf/1"
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_extract_text_fixture_emits_contract_json(tmp_path):
    pdf = write_text_fixture(tmp_path / "text.pdf")

    result = _run_worker("extract", str(pdf))

    assert result.returncode == 0, result.stderr
    payload = _single_json(result)
    assert payload["sha256"] == _sha256(pdf)
    assert payload["page_count"] == 2
    assert payload["warnings"] == []
    assert payload["render"] is None
    assert payload["metadata"]["title"] is None
    assert payload["pages"][0]["width_pt"] == 612.0
    assert payload["pages"][0]["height_pt"] == 792.0
    assert payload["pages"][1]["index"] == 2
    assert TEXT_SENTINEL in payload["pages"][1]["text"]
    assert payload["pages"][1]["chars"] > len(TEXT_SENTINEL)
    assert result.stderr == ""


def test_inspect_omits_text_and_writes_no_files(tmp_path):
    pdf = write_text_fixture(tmp_path / "text.pdf")

    result = _run_worker("inspect", str(pdf))

    assert result.returncode == 0, result.stderr
    payload = _single_json(result)
    assert payload["render"] is None
    assert all("text" not in page for page in payload["pages"])
    assert sorted(path.name for path in tmp_path.iterdir()) == ["text.pdf"]


def test_image_only_render_below_chars_writes_exact_png_dimensions(tmp_path):
    pdf = write_image_only_fixture(tmp_path / "image-only.pdf")
    render_dir = tmp_path / "renders"

    result = _run_worker(
        "extract",
        str(pdf),
        "--render-below-chars",
        "50",
        "--render-dir",
        str(render_dir),
        "--dpi",
        "150",
    )

    assert result.returncode == 0, result.stderr
    payload = _single_json(result)
    assert payload["render"] == {"dpi": 150, "dir": str(render_dir.resolve())}
    assert [page["chars"] for page in payload["pages"]] == [0, 0]
    assert [page["rendered"] for page in payload["pages"]] == [
        "page-0001.png",
        "page-0002.png",
    ]
    expected_size = (
        round(PAGE_WIDTH_PT * 150 / 72),
        round(PAGE_HEIGHT_PT * 150 / 72),
    )
    for rendered in ("page-0001.png", "page-0002.png"):
        image_path = render_dir / rendered
        assert image_path.is_file()
        with Image.open(image_path) as image:
            assert image.size == expected_size


def test_render_selectors_use_union_and_image_area_fraction(tmp_path):
    pdf = write_mixed_fixture(tmp_path / "mixed.pdf")
    render_dir = tmp_path / "renders"

    result = _run_worker(
        "extract",
        str(pdf),
        "--render-pages",
        "1",
        "--render-above-image-fraction",
        "0.30",
        "--render-dir",
        str(render_dir),
    )

    assert result.returncode == 0, result.stderr
    payload = _single_json(result)
    page1, page2, page3 = payload["pages"]
    assert page1["image_area_fraction"] < 0.05
    assert page2["image_area_fraction"] == 1.0
    assert page3["image_area_fraction"] >= 0.3
    assert [page["rendered"] for page in payload["pages"]] == [
        "page-0001.png",
        "page-0002.png",
        "page-0003.png",
    ]


def test_render_pages_selector_renders_exact_pages(tmp_path):
    pdf = write_mixed_fixture(tmp_path / "mixed.pdf")
    render_dir = tmp_path / "renders"

    result = _run_worker(
        "extract",
        str(pdf),
        "--render-pages",
        "1,3",
        "--render-dir",
        str(render_dir),
    )

    assert result.returncode == 0, result.stderr
    payload = _single_json(result)
    assert [page["rendered"] for page in payload["pages"]] == [
        "page-0001.png",
        None,
        "page-0003.png",
    ]


def test_whitespace_text_layer_counts_zero_non_whitespace_chars(tmp_path):
    pdf = write_whitespace_fixture(tmp_path / "whitespace.pdf")

    result = _run_worker("extract", str(pdf))

    assert result.returncode == 0, result.stderr
    payload = _single_json(result)
    assert payload["pages"][0]["text"].strip() == ""
    assert payload["pages"][0]["chars"] == 0


def test_metadata_dates_preserve_offsets_and_missing_dates_are_null(tmp_path):
    dated = write_dates_fixture(tmp_path / "dated.pdf")
    missing = write_missing_dates_fixture(tmp_path / "missing.pdf")
    garbled = write_garbled_dates_fixture(tmp_path / "garbled.pdf")

    dated_payload = _single_json(_run_worker("inspect", str(dated)))
    missing_payload = _single_json(_run_worker("inspect", str(missing)))
    garbled_payload = _single_json(_run_worker("inspect", str(garbled)))

    assert dated_payload["metadata"] == {
        "title": "Dated Fixture",
        "author": "sol",
        "creation_date": "2026-03-04T11:02:00-07:00",
        "mod_date": "2026-03-04T12:22:33+02:30",
        "producer": "fixture",
    }
    assert missing_payload["metadata"]["title"] == "No Dates"
    assert missing_payload["metadata"]["creation_date"] is None
    assert missing_payload["metadata"]["mod_date"] is None
    assert garbled_payload["metadata"]["title"] == "Garbled Dates"
    assert garbled_payload["metadata"]["creation_date"] is None
    assert garbled_payload["metadata"]["mod_date"] is None


def test_encrypted_documents_map_exit_code_and_encrypted_fact(tmp_path):
    clear = write_text_fixture(tmp_path / "clear.pdf")
    user_pdf = tmp_path / "user.pdf"
    owner_pdf = tmp_path / "owner.pdf"
    write_encrypted_fixture_pair(clear, user_pdf, owner_pdf)

    no_password = _run_worker("inspect", str(user_pdf))
    assert no_password.returncode == 3
    assert _single_json(no_password) == {"schema": "sol-pdf/1", "error": "encrypted"}

    correct_password = _run_worker(
        "extract",
        str(user_pdf),
        "--password",
        "userpass",
    )
    assert correct_password.returncode == 0, correct_password.stderr
    assert _single_json(correct_password)["encrypted"] is True

    owner_only = _run_worker("extract", str(owner_pdf))
    assert owner_only.returncode == 0, owner_only.stderr
    assert _single_json(owner_only)["encrypted"] is True


def test_encrypted_fixture_pair_is_deterministic(tmp_path):
    clear = write_text_fixture(tmp_path / "clear.pdf")
    first_user = tmp_path / "first-user.pdf"
    first_owner = tmp_path / "first-owner.pdf"
    second_user = tmp_path / "second-user.pdf"
    second_owner = tmp_path / "second-owner.pdf"

    write_encrypted_fixture_pair(clear, first_user, first_owner)
    write_encrypted_fixture_pair(clear, second_user, second_owner)

    assert first_user.read_bytes() == second_user.read_bytes()
    assert first_owner.read_bytes() == second_owner.read_bytes()


def test_garbage_and_zero_byte_inputs_exit_corrupt(tmp_path):
    garbage = tmp_path / "garbage.pdf"
    garbage.write_bytes(b"this is not a pdf")
    zero = tmp_path / "zero.pdf"
    zero.write_bytes(b"")

    for path in (garbage, zero):
        result = _run_worker("inspect", str(path))
        assert result.returncode == 4
        payload = _single_json(result)
        assert payload["error"] == "corrupt"
        assert "detail" in payload


def test_truncation_behavior_is_pinned_to_pdfium_observations(tmp_path):
    clean = tmp_path / "clean.pdf"
    deep = tmp_path / "deep.pdf"
    drop_startxref = tmp_path / "drop-startxref.pdf"
    drop_eof = tmp_path / "drop-eof.pdf"
    write_truncation_fixtures(clean, deep, drop_startxref, drop_eof)

    deep_result = _run_worker("inspect", str(deep))
    assert deep_result.returncode == 4
    assert _single_json(deep_result)["error"] == "corrupt"

    clean_payload = _single_json(_run_worker("extract", str(clean)))
    for mild_truncation in (drop_startxref, drop_eof):
        # Documented PDFium limit: these tail-only truncations are repaired
        # without any Python-level warning, page error, or page-count change.
        result = _run_worker("extract", str(mild_truncation))
        assert result.returncode == 0, result.stderr
        payload = _single_json(result)
        assert payload["page_count"] == clean_payload["page_count"]
        assert payload["warnings"] == []
        assert [page["text"] for page in payload["pages"]] == [
            page["text"] for page in clean_payload["pages"]
        ]
        assert all(page["error"] is None for page in payload["pages"])


def test_run_pdf_worker_success_and_typed_failures(tmp_path):
    clear = write_text_fixture(tmp_path / "clear.pdf")
    user_pdf = tmp_path / "user.pdf"
    owner_pdf = tmp_path / "owner.pdf"
    write_encrypted_fixture_pair(clear, user_pdf, owner_pdf)
    garbage = tmp_path / "garbage.pdf"
    garbage.write_bytes(b"not a pdf")
    bad_render_parent = tmp_path / "not-a-dir"
    bad_render_parent.write_text("x", encoding="utf-8")

    success = run_pdf_worker("inspect", clear)
    assert success.warnings == ()
    assert success.payload["page_count"] == 2

    with pytest.raises(PdfWorkerEncryptedError) as encrypted:
        run_pdf_worker("inspect", user_pdf)
    assert encrypted.value.payload == {"schema": "sol-pdf/1", "error": "encrypted"}

    with pytest.raises(PdfWorkerCorruptError):
        run_pdf_worker("inspect", garbage)

    with pytest.raises(PdfWorkerRenderIOError):
        run_pdf_worker(
            "extract",
            clear,
            render_below_chars=10_000,
            render_dir=bad_render_parent / "child",
        )


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (0, ""),
        (0, "not json"),
        (4, "not json"),
        (-9, ""),
    ],
)
def test_run_pdf_worker_unparseable_or_crashed_worker_maps_to_engine_failure(
    monkeypatch,
    returncode,
    stdout,
):
    class FakeCompleted:
        def __init__(self) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = "diagnostic"

    def fake_run(*_args, **_kwargs):
        return FakeCompleted()

    monkeypatch.setattr("solstone.observe.pdf_worker.subprocess.run", fake_run)

    with pytest.raises(PdfWorkerEngineError) as exc:
        run_pdf_worker("inspect", "unused.pdf")

    assert exc.value.returncode == returncode


@pytest.mark.timeout(60)
def test_run_pdf_worker_tiny_address_limit_maps_to_engine_failure(tmp_path):
    pdf = write_text_fixture(tmp_path / "clear.pdf")

    with pytest.raises(PdfWorkerEngineError):
        run_pdf_worker(
            "inspect",
            pdf,
            env={ENV_RLIMIT_AS_MB: "64"},
            timeout_seconds=20,
        )


def test_run_pdf_worker_timeout_maps_to_typed_timeout(monkeypatch, tmp_path):
    pdf = write_text_fixture(tmp_path / "clear.pdf")

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["pdf-worker"], timeout=0.01)

    monkeypatch.setattr("solstone.observe.pdf_worker.subprocess.run", fake_run)

    with pytest.raises(PdfWorkerTimeoutError):
        run_pdf_worker("inspect", pdf, timeout_seconds=0.01)


@pytest.mark.timeout(60)
def test_pdf_worker_import_purity_fresh_interpreter():
    probe = """
import importlib
import json
import sys

importlib.import_module("solstone.observe.pdf_worker")
print("MODULES_JSON:" + json.dumps(sorted(sys.modules)))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    modules_line = [
        line for line in result.stdout.splitlines() if line.startswith("MODULES_JSON:")
    ]
    assert len(modules_line) == 1
    modules = set(json.loads(modules_line[0][len("MODULES_JSON:") :]))
    assert not any(module.startswith("solstone.think") for module in modules)
    assert not any(module.startswith("solstone.convey") for module in modules)
