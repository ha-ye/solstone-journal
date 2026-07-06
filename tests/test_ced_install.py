# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from solstone.think.providers import ced_install

ARTIFACT_KEY = "linux-cpu-x64"
TARBALL_NAME = "ced-v0.1.0-lib-linux-cpu-x64.tar.gz"
MODEL_BYTES = b"ced model fixture bytes"
LIB_BYTES = b"ced shared library fixture"
HEADER_BYTES = b"int ced_capi_fixture(void);\n"
LICENSE_BYTES = b"MIT license fixture\n"
README_BYTES = b"ced readme fixture\n"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _add_tar_member(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = 0o644
    archive.addfile(member, io.BytesIO(data))


def _engine_tarball_bytes(
    *,
    artifact_key: str = ARTIFACT_KEY,
    lib_name: str = "libced.so",
) -> bytes:
    inner_name = f"ced-v0.1.0-lib-{artifact_key}"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        root = tarfile.TarInfo(inner_name)
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        _add_tar_member(archive, f"{inner_name}/{lib_name}", LIB_BYTES)
        _add_tar_member(archive, f"{inner_name}/ced_capi.h", HEADER_BYTES)
        _add_tar_member(archive, f"{inner_name}/LICENSE", LICENSE_BYTES)
        _add_tar_member(archive, f"{inner_name}/README.md", README_BYTES)
        _add_tar_member(archive, f"{inner_name}/ignored.txt", b"ignored\n")
    return output.getvalue()


def _fixture_specs() -> tuple[
    ced_install.CedEngineSpec,
    ced_install.CedModelSpec,
    dict[str, bytes],
]:
    tarball = _engine_tarball_bytes()
    data = {
        TARBALL_NAME: tarball,
        "ced-tiny-q8_0.gguf": MODEL_BYTES,
    }
    engine_spec = ced_install.CedEngineSpec(
        version="v-test",
        artifacts={
            ARTIFACT_KEY: ced_install.CedFileSpec(
                path=TARBALL_NAME,
                sha256=_sha256(tarball),
                size_bytes=len(tarball),
            )
        },
        lib_names={ARTIFACT_KEY: "libced.so"},
        header_name="ced_capi.h",
    )
    model_spec = ced_install.CedModelSpec(
        repo="mudler/ced-gguf",
        revision="test-revision",
        file="ced-tiny-q8_0.gguf",
        sha256=_sha256(MODEL_BYTES),
        size_bytes=len(MODEL_BYTES),
    )
    return engine_spec, model_spec, data


def _fake_download_factory(
    data: dict[str, bytes],
    calls: list[str] | None = None,
):
    def fake_download(
        _url: str,
        dest: Path,
        file_spec: ced_install.CedFileSpec,
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


def _force_supported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ced_install,
        "ced_engine_artifact_key",
        lambda os_name=None, arch=None: ARTIFACT_KEY,
    )


def _paths(
    engine_spec: ced_install.CedEngineSpec,
    model_spec: ced_install.CedModelSpec,
    journal_path: Path,
) -> dict[str, Path]:
    return {
        "engine": ced_install.engine_dir(
            ARTIFACT_KEY, engine_spec=engine_spec, journal_path=journal_path
        ),
        "lib": ced_install.engine_lib_path(
            ARTIFACT_KEY, engine_spec=engine_spec, journal_path=journal_path
        ),
        "header": ced_install.engine_header_path(
            ARTIFACT_KEY, engine_spec=engine_spec, journal_path=journal_path
        ),
        "license": ced_install.engine_dir(
            ARTIFACT_KEY, engine_spec=engine_spec, journal_path=journal_path
        )
        / "LICENSE",
        "readme": ced_install.engine_dir(
            ARTIFACT_KEY, engine_spec=engine_spec, journal_path=journal_path
        )
        / "README.md",
        "model": ced_install.model_path(
            engine_spec=engine_spec,
            model_spec=model_spec,
            journal_path=journal_path,
        ),
        "sidecar": ced_install.sidecar_path(
            engine_spec=engine_spec,
            journal_path=journal_path,
        ),
        "tarball": ced_install._engine_tarball_path(
            ARTIFACT_KEY, engine_spec=engine_spec, journal_path=journal_path
        ),
    }


def test_pins_match_authoritative_scope_values() -> None:
    assert ced_install.SIDECAR_NAME == ".ced-install.json"
    assert ced_install.CED_ENGINE_SPEC.version == "v0.1.0"
    assert ced_install.CED_ENGINE_SPEC.artifacts == {
        "linux-cpu-x64": ced_install.CedFileSpec(
            path="ced-v0.1.0-lib-linux-cpu-x64.tar.gz",
            sha256="915e0573bc4e17197a7a893d0eb98e1a851abb64451b2e1a8ad51f5f99040360",
            size_bytes=788651,
        ),
        "linux-cpu-arm64": ced_install.CedFileSpec(
            path="ced-v0.1.0-lib-linux-cpu-arm64.tar.gz",
            sha256="a87de0a8b086429aa5d6544a6f881a70e62726d07901734640ac85dbf146181e",
            size_bytes=720034,
        ),
        "macos-metal-arm64": ced_install.CedFileSpec(
            path="ced-v0.1.0-lib-macos-metal-arm64.tar.gz",
            sha256="4c913ba0ece1d06ba2210da9fcaee3d8199ca3c62697c331810f224444e4054b",
            size_bytes=686952,
        ),
    }
    assert ced_install.CED_ENGINE_SPEC.lib_names == {
        "linux-cpu-x64": "libced.so",
        "linux-cpu-arm64": "libced.so",
        "macos-metal-arm64": "libced.dylib",
    }
    assert ced_install.CED_MODEL_SPEC == ced_install.CedModelSpec(
        repo="mudler/ced-gguf",
        revision="b5e9a4aad6438763c8da16079d77563fbed35c65",
        file="ced-tiny-q8_0.gguf",
        sha256="48bee4e2fc3cc85d7806e03471db24e77fda6c2a2e81ffe9ef67caebaf2bd674",
        size_bytes=6211616,
    )


@pytest.mark.parametrize(
    ("os_name", "arch", "expected"),
    [
        ("linux", "x86_64", "linux-cpu-x64"),
        ("linux", "amd64", "linux-cpu-x64"),
        ("linux", "aarch64", "linux-cpu-arm64"),
        ("linux", "arm64", "linux-cpu-arm64"),
        ("darwin", "arm64", "macos-metal-arm64"),
        ("darwin", "x86_64", None),
        ("windows", "amd64", None),
    ],
)
def test_platform_artifact_key(
    os_name: str,
    arch: str,
    expected: str | None,
) -> None:
    assert ced_install.ced_engine_artifact_key(os_name, arch) == expected


def test_unsupported_platform_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ced_install.parakeet_readiness, "_platform_info", lambda: ("windows", "amd64")
    )
    monkeypatch.setattr(
        ced_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
    )

    assert ced_install.check_ced_assets(journal_path=tmp_path) is None
    assert ced_install.install_ced_assets(journal_path=tmp_path) is None


def test_fetch_if_missing_writes_engine_model_and_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_spec, model_spec, data = _fixture_specs()
    calls: list[str] = []
    _force_supported_platform(monkeypatch)
    monkeypatch.setattr(
        ced_install,
        "_download_file",
        _fake_download_factory(data, calls),
    )

    record = ced_install.install_ced_assets(
        engine_spec=engine_spec,
        model_spec=model_spec,
        journal_path=tmp_path,
    )

    paths = _paths(engine_spec, model_spec, tmp_path)
    assert paths["lib"].read_bytes() == LIB_BYTES
    assert paths["header"].read_bytes() == HEADER_BYTES
    assert paths["license"].read_bytes() == LICENSE_BYTES
    assert paths["readme"].read_bytes() == README_BYTES
    assert not (paths["engine"] / "ignored.txt").exists()
    assert paths["model"].read_bytes() == MODEL_BYTES
    assert paths["sidecar"].is_file()
    assert record == ced_install.CedInstallRecord.from_json(
        paths["sidecar"].read_text(encoding="utf-8")
    )
    assert calls == [TARBALL_NAME, "ced-tiny-q8_0.gguf"]


def test_check_valid_uses_no_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_spec, model_spec, data = _fixture_specs()
    _force_supported_platform(monkeypatch)
    monkeypatch.setattr(
        ced_install,
        "_download_file",
        _fake_download_factory(data),
    )
    ced_install.install_ced_assets(
        engine_spec=engine_spec,
        model_spec=model_spec,
        journal_path=tmp_path,
    )
    monkeypatch.setattr(
        ced_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
    )

    assert (
        ced_install.check_ced_assets(
            engine_spec=engine_spec,
            model_spec=model_spec,
            journal_path=tmp_path,
        ).model_revision
        == model_spec.revision
    )


def test_check_missing_raises_without_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_spec, model_spec, _data = _fixture_specs()
    _force_supported_platform(monkeypatch)
    monkeypatch.setattr(
        ced_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
    )

    with pytest.raises(ced_install.CedInstallError) as exc_info:
        ced_install.check_ced_assets(
            engine_spec=engine_spec,
            model_spec=model_spec,
            journal_path=tmp_path,
        )

    assert exc_info.value.reason_code == "sidecar_missing"


def test_present_valid_install_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_spec, model_spec, data = _fixture_specs()
    _force_supported_platform(monkeypatch)
    monkeypatch.setattr(
        ced_install,
        "_download_file",
        _fake_download_factory(data),
    )
    ced_install.install_ced_assets(
        engine_spec=engine_spec,
        model_spec=model_spec,
        journal_path=tmp_path,
    )
    monkeypatch.setattr(
        ced_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
    )

    record = ced_install.install_ced_assets(
        engine_spec=engine_spec,
        model_spec=model_spec,
        journal_path=tmp_path,
    )

    assert record.files == ced_install._expected_files(
        engine_spec, model_spec, ARTIFACT_KEY
    )


def test_force_refetches_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_spec, model_spec, data = _fixture_specs()
    _force_supported_platform(monkeypatch)
    monkeypatch.setattr(
        ced_install,
        "_download_file",
        _fake_download_factory(data),
    )
    ced_install.install_ced_assets(
        engine_spec=engine_spec,
        model_spec=model_spec,
        journal_path=tmp_path,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        ced_install,
        "_download_file",
        _fake_download_factory(data, calls),
    )

    ced_install.install_ced_assets(
        force=True,
        engine_spec=engine_spec,
        model_spec=model_spec,
        journal_path=tmp_path,
    )

    assert calls == [TARBALL_NAME, "ced-tiny-q8_0.gguf"]


def test_sha256_mismatch_cleans_partial_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_spec, model_spec, data = _fixture_specs()
    bad = bytes([data[TARBALL_NAME][0] ^ 1]) + data[TARBALL_NAME][1:]
    _force_supported_platform(monkeypatch)
    _patch_stream(monkeypatch, bad)

    with pytest.raises(ced_install.CedInstallError) as exc_info:
        ced_install.install_ced_assets(
            engine_spec=engine_spec,
            model_spec=model_spec,
            journal_path=tmp_path,
        )

    paths = _paths(engine_spec, model_spec, tmp_path)
    assert exc_info.value.reason_code == "sha256_mismatch"
    assert not paths["engine"].exists()
    assert not paths["model"].exists()
    assert not paths["model"].with_name(f"{paths['model'].name}.tmp").exists()
    assert not paths["sidecar"].exists()
    assert not paths["tarball"].exists()
    assert not paths["tarball"].with_name(f"{paths['tarball'].name}.tmp").exists()


def test_size_mismatch_cleans_partial_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_spec, model_spec, _data = _fixture_specs()
    _force_supported_platform(monkeypatch)
    _patch_stream(monkeypatch, b"x", headers={})

    with pytest.raises(ced_install.CedInstallError) as exc_info:
        ced_install.install_ced_assets(
            engine_spec=engine_spec,
            model_spec=model_spec,
            journal_path=tmp_path,
        )

    paths = _paths(engine_spec, model_spec, tmp_path)
    assert exc_info.value.reason_code == "size_mismatch"
    assert not paths["engine"].exists()
    assert not paths["model"].exists()
    assert not paths["sidecar"].exists()
    assert not paths["tarball"].exists()
    assert not paths["tarball"].with_name(f"{paths['tarball'].name}.tmp").exists()


def test_download_verifies_before_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_spec = ced_install.CedFileSpec(
        path="ced-tiny-q8_0.gguf",
        sha256=_sha256(MODEL_BYTES),
        size_bytes=len(MODEL_BYTES),
    )
    bad = bytes([MODEL_BYTES[0] ^ 1]) + MODEL_BYTES[1:]
    _patch_stream(monkeypatch, bad)
    dest = tmp_path / "ced-tiny-q8_0.gguf"

    with pytest.raises(ced_install.CedInstallError) as exc_info:
        ced_install._download_file("https://example.test/model.gguf", dest, file_spec)

    assert exc_info.value.reason_code == "sha256_mismatch"
    assert not dest.exists()
    assert not dest.with_name(f"{dest.name}.tmp").exists()


def test_invalid_sidecar_shape_fails_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_spec, model_spec, _data = _fixture_specs()
    _force_supported_platform(monkeypatch)
    sidecar = ced_install.sidecar_path(engine_spec=engine_spec, journal_path=tmp_path)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ced_install.CedInstallError) as exc_info:
        ced_install.check_ced_assets(
            engine_spec=engine_spec,
            model_spec=model_spec,
            journal_path=tmp_path,
        )

    assert exc_info.value.reason_code == "sidecar_invalid"


def test_safe_extract_tarball_rejects_path_traversal(tmp_path: Path) -> None:
    tarball = tmp_path / "bad.tar.gz"
    data = b"bad"
    with tarfile.open(tarball, "w:gz") as archive:
        member = tarfile.TarInfo("../escape")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))

    with pytest.raises(ced_install.CedInstallError) as exc_info:
        ced_install._extract_engine_tarball(tarball, tmp_path / "dest")

    assert exc_info.value.reason_code == "archive_path_traversal"
