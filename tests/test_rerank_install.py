# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from solstone.think.providers import rerank_install

MODEL_BYTES = b"model fixture bytes"
TOKENIZER_BYTES = b'{"tokenizer":"fixture"}'
MODEL_PATH = "onnx/model.onnx"
TOKENIZER_PATH = "tokenizer.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixture_bytes() -> dict[str, bytes]:
    return {
        MODEL_PATH: MODEL_BYTES,
        TOKENIZER_PATH: TOKENIZER_BYTES,
    }


def _fixture_spec() -> rerank_install.RerankModelSpec:
    data = _fixture_bytes()
    return rerank_install.RerankModelSpec(
        repo="Xenova/ms-marco-MiniLM-L-6-v2",
        revision="a09144355adeed5f58c8ed011d209bf8ee5a1fec",
        files=(
            rerank_install.RerankFileSpec(
                path=MODEL_PATH,
                sha256=_sha256(data[MODEL_PATH]),
                size_bytes=len(data[MODEL_PATH]),
            ),
            rerank_install.RerankFileSpec(
                path=TOKENIZER_PATH,
                sha256=_sha256(data[TOKENIZER_PATH]),
                size_bytes=len(data[TOKENIZER_PATH]),
            ),
        ),
    )


def _fake_download_factory(
    data: dict[str, bytes],
    calls: list[str] | None = None,
):
    def fake_download(
        _url: str,
        dest: Path,
        file_spec: rerank_install.RerankFileSpec,
        **_kwargs: Any,
    ) -> None:
        if calls is not None:
            calls.append(file_spec.path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data[file_spec.path])

    return fake_download


class _FakeResponse:
    def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return

    def iter_bytes(self):
        midpoint = max(1, len(self._payload) // 2)
        yield self._payload[:midpoint]
        yield self._payload[midpoint:]


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    def __enter__(self) -> _FakeResponse:
        return self.response

    def __exit__(self, *_args: object) -> bool:
        return False


def _patch_stream(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    headers: dict[str, str] | None = None,
) -> None:
    def fake_stream(*_args: Any, **_kwargs: Any) -> _FakeStream:
        return _FakeStream(_FakeResponse(payload, headers=headers))

    monkeypatch.setattr(httpx, "stream", fake_stream)


def _paths(
    spec: rerank_install.RerankModelSpec, journal_path: Path
) -> tuple[Path, Path, Path]:
    model = rerank_install.asset_path(
        spec.files[0], spec=spec, journal_path=journal_path
    )
    tokenizer = rerank_install.asset_path(
        spec.files[1], spec=spec, journal_path=journal_path
    )
    sidecar = rerank_install.sidecar_path(spec=spec, journal_path=journal_path)
    return model, tokenizer, sidecar


def test_fetch_if_missing_writes_files_and_sidecar(tmp_path, monkeypatch) -> None:
    spec = _fixture_spec()
    calls: list[str] = []
    monkeypatch.setattr(
        rerank_install,
        "_download_file",
        _fake_download_factory(_fixture_bytes(), calls),
    )

    record = rerank_install.install_rerank_model(spec=spec, journal_path=tmp_path)

    model, tokenizer, sidecar = _paths(spec, tmp_path)
    assert model.read_bytes() == MODEL_BYTES
    assert tokenizer.read_bytes() == TOKENIZER_BYTES
    assert sidecar.is_file()
    assert record == rerank_install.RerankInstallRecord.from_json(
        sidecar.read_text(encoding="utf-8")
    )
    assert calls == [MODEL_PATH, TOKENIZER_PATH]


def test_present_valid_install_is_noop(tmp_path, monkeypatch) -> None:
    spec = _fixture_spec()
    monkeypatch.setattr(
        rerank_install,
        "_download_file",
        _fake_download_factory(_fixture_bytes()),
    )
    rerank_install.install_rerank_model(spec=spec, journal_path=tmp_path)
    monkeypatch.setattr(
        rerank_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
    )

    record = rerank_install.install_rerank_model(spec=spec, journal_path=tmp_path)

    assert record.files == {
        file_spec.path: file_spec.sha256 for file_spec in spec.files
    }


def test_check_valid_uses_no_network(tmp_path, monkeypatch) -> None:
    spec = _fixture_spec()
    monkeypatch.setattr(
        rerank_install,
        "_download_file",
        _fake_download_factory(_fixture_bytes()),
    )
    rerank_install.install_rerank_model(spec=spec, journal_path=tmp_path)
    monkeypatch.setattr(
        rerank_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
    )

    assert (
        rerank_install.check_rerank_model(spec=spec, journal_path=tmp_path).revision
        == spec.revision
    )


def test_check_missing_raises_without_download(tmp_path, monkeypatch) -> None:
    spec = _fixture_spec()
    monkeypatch.setattr(
        rerank_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
    )

    with pytest.raises(rerank_install.RerankInstallError) as exc_info:
        rerank_install.check_rerank_model(spec=spec, journal_path=tmp_path)

    assert exc_info.value.reason_code == "sidecar_missing"


def test_force_refetches_when_present(tmp_path, monkeypatch) -> None:
    spec = _fixture_spec()
    monkeypatch.setattr(
        rerank_install,
        "_download_file",
        _fake_download_factory(_fixture_bytes()),
    )
    rerank_install.install_rerank_model(spec=spec, journal_path=tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        rerank_install,
        "_download_file",
        _fake_download_factory(_fixture_bytes(), calls),
    )

    rerank_install.install_rerank_model(force=True, spec=spec, journal_path=tmp_path)

    assert calls == [MODEL_PATH, TOKENIZER_PATH]


def test_sha256_mismatch_cleans_partial_install(tmp_path, monkeypatch) -> None:
    spec = _fixture_spec()
    bad = bytes([MODEL_BYTES[0] ^ 1]) + MODEL_BYTES[1:]
    _patch_stream(monkeypatch, bad)

    with pytest.raises(rerank_install.RerankInstallError) as exc_info:
        rerank_install.install_rerank_model(spec=spec, journal_path=tmp_path)

    model, tokenizer, sidecar = _paths(spec, tmp_path)
    assert exc_info.value.reason_code == "sha256_mismatch"
    assert not model.exists()
    assert not model.with_name(f"{model.name}.tmp").exists()
    assert not tokenizer.exists()
    assert not sidecar.exists()


def test_size_mismatch_cleans_partial_install(tmp_path, monkeypatch) -> None:
    spec = _fixture_spec()
    _patch_stream(monkeypatch, b"x", headers={})

    with pytest.raises(rerank_install.RerankInstallError) as exc_info:
        rerank_install.install_rerank_model(spec=spec, journal_path=tmp_path)

    model, tokenizer, sidecar = _paths(spec, tmp_path)
    assert exc_info.value.reason_code == "size_mismatch"
    assert not model.exists()
    assert not model.with_name(f"{model.name}.tmp").exists()
    assert not tokenizer.exists()
    assert not sidecar.exists()


def test_download_verifies_before_rename(tmp_path, monkeypatch) -> None:
    spec = _fixture_spec()
    file_spec = spec.files[0]
    bad = bytes([MODEL_BYTES[0] ^ 1]) + MODEL_BYTES[1:]
    _patch_stream(monkeypatch, bad)
    dest = tmp_path / "model.onnx"

    with pytest.raises(rerank_install.RerankInstallError) as exc_info:
        rerank_install._download_file(
            "https://example.test/model.onnx", dest, file_spec
        )

    assert exc_info.value.reason_code == "sha256_mismatch"
    assert not dest.exists()
    assert not dest.with_name(f"{dest.name}.tmp").exists()


def test_invalid_sidecar_shape_fails_check(tmp_path) -> None:
    spec = _fixture_spec()
    sidecar = rerank_install.sidecar_path(spec=spec, journal_path=tmp_path)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{}\n", encoding="utf-8")

    with pytest.raises(rerank_install.RerankInstallError) as exc_info:
        rerank_install.check_rerank_model(spec=spec, journal_path=tmp_path)

    assert exc_info.value.reason_code == "sidecar_invalid"
