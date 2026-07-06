# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from solstone.think.providers import rfdetr_install

ENGINE_BYTES = b"rf-detr cli fixture bytes"
MODEL_BYTES = b"rf-detr model fixture bytes"


@dataclass(frozen=True)
class _EngineFixture:
    tarball_bytes: bytes
    tarball_sha256: str
    binary_sha256: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def engine_tarball(tmp_path: Path) -> _EngineFixture:
    src = (
        tmp_path
        / "engine-src"
        / f"rfdetr-cli-{rfdetr_install.ENGINE_REF}-linux-cpu-x64"
    )
    src.mkdir(parents=True)
    (src / rfdetr_install.ENGINE_BINARY_NAME).write_bytes(ENGINE_BYTES)
    (src / "LICENSE").write_text("fixture license\n", encoding="utf-8")
    (src / "PROVENANCE.txt").write_text("fixture provenance\n", encoding="utf-8")
    tarball = tmp_path / rfdetr_install.RFDETR_SPEC.engine.tarball_name
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(src, arcname=src.name)
    tarball_bytes = tarball.read_bytes()
    return _EngineFixture(
        tarball_bytes=tarball_bytes,
        tarball_sha256=_sha256(tarball_bytes),
        binary_sha256=_sha256(ENGINE_BYTES),
    )


def _fixture_spec(
    engine: _EngineFixture,
    *,
    model_bytes: bytes = MODEL_BYTES,
    tarball_sha256: str | None = None,
    binary_sha256: str | None = None,
    model_sha256: str | None = None,
    model_size_bytes: int | None = None,
) -> rfdetr_install.RfdetrSpec:
    pinned = rfdetr_install.RFDETR_SPEC
    return rfdetr_install.RfdetrSpec(
        engine=rfdetr_install.RfdetrEngineSpec(
            ref=pinned.engine.ref,
            release_tag=pinned.engine.release_tag,
            tarball_name=pinned.engine.tarball_name,
            tarball_sha256=tarball_sha256 or engine.tarball_sha256,
            binary_name=pinned.engine.binary_name,
            binary_sha256=binary_sha256 or engine.binary_sha256,
        ),
        model=rfdetr_install.RfdetrModelSpec(
            repo=pinned.model.repo,
            revision=pinned.model.revision,
            filename=pinned.model.filename,
            sha256=model_sha256 or _sha256(model_bytes),
            size_bytes=(
                len(model_bytes) if model_size_bytes is None else model_size_bytes
            ),
        ),
    )


def _payloads(
    spec: rfdetr_install.RfdetrSpec,
    engine: _EngineFixture,
    *,
    model_bytes: bytes = MODEL_BYTES,
) -> dict[str, bytes]:
    return {
        spec.engine.tarball_name: engine.tarball_bytes,
        spec.model.filename: model_bytes,
    }


def _fake_download_factory(
    payloads: dict[str, bytes],
    calls: list[str] | None = None,
):
    def fake_download(
        _url: str,
        dest: Path,
        file_spec: rfdetr_install.RfdetrFileSpec,
        **_kwargs: Any,
    ) -> None:
        if calls is not None:
            calls.append(file_spec.path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payloads[file_spec.path])

    return fake_download


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

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


def _patch_stream_map(
    monkeypatch: pytest.MonkeyPatch, payloads: dict[str, bytes]
) -> None:
    def fake_stream(_method: str, url: str, **_kwargs: Any) -> _FakeStream:
        for needle, payload in payloads.items():
            if needle in url:
                return _FakeStream(_FakeResponse(payload))
        raise AssertionError(f"unexpected download URL: {url}")

    monkeypatch.setattr(httpx, "stream", fake_stream)


def _tmp_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.tmp")


def _assert_cleaned(spec: rfdetr_install.RfdetrSpec, journal_path: Path) -> None:
    binary = rfdetr_install.binary_path(spec=spec, journal_path=journal_path)
    model = rfdetr_install.model_path(spec=spec, journal_path=journal_path)
    tarball = rfdetr_install._engine_tarball_path(spec, journal_path)
    assert not binary.exists()
    assert not _tmp_path(binary).exists()
    assert not model.exists()
    assert not _tmp_path(model).exists()
    assert not tarball.exists()
    assert not _tmp_path(tarball).exists()
    assert not rfdetr_install._engine_extract_dir(spec, journal_path).exists()
    assert not rfdetr_install.sidecar_path(journal_path=journal_path).exists()


def _mark_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rfdetr_install, "_platform_info", lambda: ("linux", "x86_64"))


def test_sidecar_round_trip() -> None:
    record = rfdetr_install.RfdetrInstallRecord(
        status="installed",
        engine_ref=rfdetr_install.RFDETR_SPEC.engine.ref,
        engine_sha256=rfdetr_install.RFDETR_SPEC.engine.binary_sha256,
        model_repo=rfdetr_install.RFDETR_SPEC.model.repo,
        model_revision=rfdetr_install.RFDETR_SPEC.model.revision,
        model_file=rfdetr_install.RFDETR_SPEC.model.filename,
        model_sha256=rfdetr_install.RFDETR_SPEC.model.sha256,
    )
    assert rfdetr_install.RfdetrInstallRecord.from_json(record.to_json()) == record

    unavailable = rfdetr_install.RfdetrInstallRecord(status="platform_unavailable")
    assert (
        rfdetr_install.RfdetrInstallRecord.from_json(unavailable.to_json())
        == unavailable
    )


def test_install_writes_files_and_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_tarball: _EngineFixture
) -> None:
    _mark_supported(monkeypatch)
    spec = _fixture_spec(engine_tarball)
    monkeypatch.setattr(
        rfdetr_install,
        "_download_file",
        _fake_download_factory(_payloads(spec, engine_tarball)),
    )

    record = rfdetr_install.install_rfdetr(spec=spec, journal_path=tmp_path)

    binary = rfdetr_install.binary_path(spec=spec, journal_path=tmp_path)
    model = rfdetr_install.model_path(spec=spec, journal_path=tmp_path)
    sidecar = rfdetr_install.sidecar_path(journal_path=tmp_path)
    assert binary.read_bytes() == ENGINE_BYTES
    assert os.access(binary, os.X_OK)
    assert model.read_bytes() == MODEL_BYTES
    assert sidecar.is_file()
    assert record.status == "installed"
    assert record == rfdetr_install.RfdetrInstallRecord.from_json(
        sidecar.read_text(encoding="utf-8")
    )


def test_present_valid_install_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_tarball: _EngineFixture
) -> None:
    _mark_supported(monkeypatch)
    spec = _fixture_spec(engine_tarball)
    monkeypatch.setattr(
        rfdetr_install,
        "_download_file",
        _fake_download_factory(_payloads(spec, engine_tarball)),
    )
    rfdetr_install.install_rfdetr(spec=spec, journal_path=tmp_path)
    monkeypatch.setattr(
        rfdetr_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
    )

    record = rfdetr_install.install_rfdetr(spec=spec, journal_path=tmp_path)

    assert record.engine_ref == spec.engine.ref
    assert record.model_file == spec.model.filename


def test_check_valid_uses_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_tarball: _EngineFixture
) -> None:
    _mark_supported(monkeypatch)
    spec = _fixture_spec(engine_tarball)
    monkeypatch.setattr(
        rfdetr_install,
        "_download_file",
        _fake_download_factory(_payloads(spec, engine_tarball)),
    )
    rfdetr_install.install_rfdetr(spec=spec, journal_path=tmp_path)
    monkeypatch.setattr(
        rfdetr_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
    )

    assert (
        rfdetr_install.check_rfdetr_model(spec=spec, journal_path=tmp_path).engine_ref
        == spec.engine.ref
    )


def test_check_missing_raises_without_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_tarball: _EngineFixture
) -> None:
    _mark_supported(monkeypatch)
    spec = _fixture_spec(engine_tarball)
    monkeypatch.setattr(
        rfdetr_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
    )

    with pytest.raises(rfdetr_install.RfdetrInstallError) as exc_info:
        rfdetr_install.check_rfdetr_model(spec=spec, journal_path=tmp_path)

    assert exc_info.value.reason_code == "sidecar_missing"


def test_force_refetches_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_tarball: _EngineFixture
) -> None:
    _mark_supported(monkeypatch)
    spec = _fixture_spec(engine_tarball)
    monkeypatch.setattr(
        rfdetr_install,
        "_download_file",
        _fake_download_factory(_payloads(spec, engine_tarball)),
    )
    rfdetr_install.install_rfdetr(spec=spec, journal_path=tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        rfdetr_install,
        "_download_file",
        _fake_download_factory(_payloads(spec, engine_tarball), calls),
    )

    rfdetr_install.install_rfdetr(force=True, spec=spec, journal_path=tmp_path)

    assert calls == [spec.engine.tarball_name, spec.model.filename]


def test_model_sha256_mismatch_cleans_partial_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_tarball: _EngineFixture
) -> None:
    _mark_supported(monkeypatch)
    spec = _fixture_spec(engine_tarball)
    bad_model = bytes([MODEL_BYTES[0] ^ 1]) + MODEL_BYTES[1:]
    _patch_stream_map(
        monkeypatch,
        _payloads(spec, engine_tarball, model_bytes=bad_model),
    )

    with pytest.raises(rfdetr_install.RfdetrInstallError) as exc_info:
        rfdetr_install.install_rfdetr(spec=spec, journal_path=tmp_path)

    assert exc_info.value.reason_code == "sha256_mismatch"
    _assert_cleaned(spec, tmp_path)


def test_tarball_sha256_mismatch_cleans_partial_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_tarball: _EngineFixture
) -> None:
    _mark_supported(monkeypatch)
    spec = _fixture_spec(engine_tarball, tarball_sha256="0" * 64)
    _patch_stream_map(monkeypatch, _payloads(spec, engine_tarball))

    with pytest.raises(rfdetr_install.RfdetrInstallError) as exc_info:
        rfdetr_install.install_rfdetr(spec=spec, journal_path=tmp_path)

    assert exc_info.value.reason_code == "sha256_mismatch"
    _assert_cleaned(spec, tmp_path)


def test_inner_binary_sha256_mismatch_cleans_partial_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_tarball: _EngineFixture
) -> None:
    _mark_supported(monkeypatch)
    spec = _fixture_spec(engine_tarball, binary_sha256="0" * 64)
    _patch_stream_map(monkeypatch, _payloads(spec, engine_tarball))

    with pytest.raises(rfdetr_install.RfdetrInstallError) as exc_info:
        rfdetr_install.install_rfdetr(spec=spec, journal_path=tmp_path)

    assert exc_info.value.reason_code == "sha256_mismatch"
    _assert_cleaned(spec, tmp_path)


def test_model_download_verifies_before_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_tarball: _EngineFixture
) -> None:
    spec = _fixture_spec(engine_tarball)
    file_spec = rfdetr_install.RfdetrFileSpec(
        spec.model.filename,
        spec.model.sha256,
        spec.model.size_bytes,
    )
    bad_model = bytes([MODEL_BYTES[0] ^ 1]) + MODEL_BYTES[1:]
    _patch_stream_map(monkeypatch, {spec.model.filename: bad_model})
    dest = tmp_path / spec.model.filename

    with pytest.raises(rfdetr_install.RfdetrInstallError) as exc_info:
        rfdetr_install._download_file(
            f"https://example.test/{spec.model.filename}", dest, file_spec
        )

    assert exc_info.value.reason_code == "sha256_mismatch"
    assert not dest.exists()
    assert not _tmp_path(dest).exists()


def test_extract_rejects_path_traversal(tmp_path: Path) -> None:
    tarball = tmp_path / "bad.tar.gz"
    with tarfile.open(tarball, "w:gz") as archive:
        member = tarfile.TarInfo("../evil")
        payload = b"bad"
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(rfdetr_install.RfdetrInstallError) as exc_info:
        rfdetr_install._safe_extract_tarball(tarball, tmp_path / "extract")

    assert exc_info.value.reason_code == "archive_path_traversal"


def test_platform_unavailable_writes_marker_no_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_tarball: _EngineFixture
) -> None:
    monkeypatch.setattr(rfdetr_install, "_platform_info", lambda: ("linux", "aarch64"))
    spec = _fixture_spec(engine_tarball)
    monkeypatch.setattr(
        rfdetr_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
    )

    record = rfdetr_install.install_rfdetr(spec=spec, journal_path=tmp_path)

    assert record.status == "platform_unavailable"
    sidecar = rfdetr_install.sidecar_path(journal_path=tmp_path)
    assert sidecar.is_file()
    assert (
        rfdetr_install.RfdetrInstallRecord.from_json(
            sidecar.read_text(encoding="utf-8")
        ).status
        == "platform_unavailable"
    )
    assert not rfdetr_install.binary_path(spec=spec, journal_path=tmp_path).exists()
    assert not rfdetr_install.model_path(spec=spec, journal_path=tmp_path).exists()
    assert (
        rfdetr_install.check_rfdetr_model(spec=spec, journal_path=tmp_path).status
        == "platform_unavailable"
    )


def test_rfdetr_paths_three_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_tarball: _EngineFixture
) -> None:
    _mark_supported(monkeypatch)
    spec = _fixture_spec(engine_tarball)
    empty = tmp_path / "empty"
    assert (
        rfdetr_install.rfdetr_paths(spec=spec, journal_path=empty).status
        == "not_installed"
    )
    monkeypatch.setattr(
        rfdetr_install,
        "_download_file",
        _fake_download_factory(_payloads(spec, engine_tarball)),
    )
    rfdetr_install.install_rfdetr(spec=spec, journal_path=tmp_path)

    installed = rfdetr_install.rfdetr_paths(spec=spec, journal_path=tmp_path)
    assert installed.status == "installed"
    assert installed.binary_path == rfdetr_install.binary_path(
        spec=spec, journal_path=tmp_path
    )
    assert installed.model_path == rfdetr_install.model_path(
        spec=spec, journal_path=tmp_path
    )

    monkeypatch.setattr(rfdetr_install, "_platform_info", lambda: ("linux", "aarch64"))
    unavailable = rfdetr_install.rfdetr_paths(spec=spec, journal_path=tmp_path)
    assert unavailable.status == "platform_unavailable"
    assert unavailable.binary_path is None
    assert unavailable.model_path is None


def test_invalid_sidecar_shape_fails_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_tarball: _EngineFixture
) -> None:
    _mark_supported(monkeypatch)
    spec = _fixture_spec(engine_tarball)
    sidecar = rfdetr_install.sidecar_path(journal_path=tmp_path)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{}\n", encoding="utf-8")

    with pytest.raises(rfdetr_install.RfdetrInstallError) as exc_info:
        rfdetr_install.check_rfdetr_model(spec=spec, journal_path=tmp_path)

    assert exc_info.value.reason_code == "sidecar_invalid"
