# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Install and verify local ced.cpp sound-tagging provider artifacts.

This module performs no network access at import time.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from solstone.think import parakeet_readiness
from solstone.think.providers.parakeet_install import (
    ParakeetProviderError,
    _safe_extract_tarball,
)
from solstone.think.utils import get_journal

SIDECAR_NAME = ".ced-install.json"
ENGINE_RELEASE_BASE_URL = (
    "https://github.com/localai-org/ced.cpp/releases/download/v0.1.0"
)
_ENGINE_REQUIRED_FILES = ("LICENSE", "README.md")


class CedInstallError(RuntimeError):
    """ced.cpp artifact acquisition failure with a recovery reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class CedFileSpec:
    path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class CedEngineSpec:
    version: str
    artifacts: dict[str, CedFileSpec]
    lib_names: dict[str, str]
    header_name: str


@dataclass(frozen=True)
class CedModelSpec:
    repo: str
    revision: str
    file: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class CedInstallRecord:
    engine_version: str
    artifact_key: str
    model_repo: str
    model_revision: str
    files: dict[str, str]

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "artifact_key": self.artifact_key,
                    "engine_version": self.engine_version,
                    "files": self.files,
                    "model_repo": self.model_repo,
                    "model_revision": self.model_revision,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    @classmethod
    def from_json(cls, text: str) -> CedInstallRecord:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("ced install record must be an object")
        engine_version = data.get("engine_version")
        artifact_key = data.get("artifact_key")
        model_repo = data.get("model_repo")
        model_revision = data.get("model_revision")
        files = data.get("files")
        for key, value in (
            ("engine_version", engine_version),
            ("artifact_key", artifact_key),
            ("model_repo", model_repo),
            ("model_revision", model_revision),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"ced install record {key} must be a non-empty string")
        if not isinstance(files, dict):
            raise ValueError("ced install record files must be an object")
        normalized: dict[str, str] = {}
        for name, digest in files.items():
            if not isinstance(name, str) or not name:
                raise ValueError("ced install record file names must be strings")
            if not isinstance(digest, str) or not digest:
                raise ValueError("ced install record file digests must be strings")
            normalized[name] = digest
        return cls(
            engine_version=engine_version,
            artifact_key=artifact_key,
            model_repo=model_repo,
            model_revision=model_revision,
            files=normalized,
        )


CED_ENGINE_SPEC = CedEngineSpec(
    version="v0.1.0",
    artifacts={
        "linux-cpu-x64": CedFileSpec(
            path="ced-v0.1.0-lib-linux-cpu-x64.tar.gz",
            sha256="915e0573bc4e17197a7a893d0eb98e1a851abb64451b2e1a8ad51f5f99040360",
            size_bytes=788651,
        ),
        "linux-cpu-arm64": CedFileSpec(
            path="ced-v0.1.0-lib-linux-cpu-arm64.tar.gz",
            sha256="a87de0a8b086429aa5d6544a6f881a70e62726d07901734640ac85dbf146181e",
            size_bytes=720034,
        ),
        "macos-metal-arm64": CedFileSpec(
            path="ced-v0.1.0-lib-macos-metal-arm64.tar.gz",
            sha256="4c913ba0ece1d06ba2210da9fcaee3d8199ca3c62697c331810f224444e4054b",
            size_bytes=686952,
        ),
    },
    lib_names={
        "linux-cpu-x64": "libced.so",
        "linux-cpu-arm64": "libced.so",
        "macos-metal-arm64": "libced.dylib",
    },
    header_name="ced_capi.h",
)

CED_MODEL_SPEC = CedModelSpec(
    repo="mudler/ced-gguf",
    revision="b5e9a4aad6438763c8da16079d77563fbed35c65",
    file="ced-tiny-q8_0.gguf",
    sha256="48bee4e2fc3cc85d7806e03471db24e77fda6c2a2e81ffe9ef67caebaf2bd674",
    size_bytes=6211616,
)


def ced_engine_artifact_key(
    os_name: str | None = None, arch: str | None = None
) -> str | None:
    if os_name is None or arch is None:
        detected_os, detected_arch = parakeet_readiness._platform_info()
        os_name = os_name or detected_os
        arch = arch or detected_arch

    normalized_arch = arch.lower()
    if os_name == "linux":
        if normalized_arch in {"amd64", "x64", "x86_64"}:
            return "linux-cpu-x64"
        if normalized_arch in {"arm64", "aarch64"}:
            return "linux-cpu-arm64"
    if os_name == "darwin" and normalized_arch == "arm64":
        return "macos-metal-arm64"
    return None


def cache_root(journal_path: str | Path | None = None) -> Path:
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return root / "cache" / "providers" / "ced"


def asset_dir(
    *,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    journal_path: str | Path | None = None,
) -> Path:
    return cache_root(journal_path) / engine_spec.version


def _resolve_artifact_key(artifact_key: str | None = None) -> str:
    resolved = artifact_key or ced_engine_artifact_key()
    if resolved is None:
        raise CedInstallError("unsupported_platform", "ced assets unsupported here")
    return resolved


def engine_dir(
    artifact_key: str,
    *,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    journal_path: str | Path | None = None,
) -> Path:
    return (
        asset_dir(engine_spec=engine_spec, journal_path=journal_path)
        / "engine"
        / artifact_key
    )


def engine_lib_path(
    artifact_key: str | None = None,
    *,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    journal_path: str | Path | None = None,
) -> Path:
    resolved = _resolve_artifact_key(artifact_key)
    return (
        engine_dir(resolved, engine_spec=engine_spec, journal_path=journal_path)
        / engine_spec.lib_names[resolved]
    )


def engine_header_path(
    artifact_key: str | None = None,
    *,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    journal_path: str | Path | None = None,
) -> Path:
    resolved = _resolve_artifact_key(artifact_key)
    return (
        engine_dir(resolved, engine_spec=engine_spec, journal_path=journal_path)
        / engine_spec.header_name
    )


def model_dir(
    *,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    model_spec: CedModelSpec = CED_MODEL_SPEC,
    journal_path: str | Path | None = None,
) -> Path:
    return (
        asset_dir(engine_spec=engine_spec, journal_path=journal_path)
        / "models"
        / model_spec.repo.replace("/", "__")
        / model_spec.revision
    )


def model_path(
    *,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    model_spec: CedModelSpec = CED_MODEL_SPEC,
    journal_path: str | Path | None = None,
) -> Path:
    return (
        model_dir(
            engine_spec=engine_spec, model_spec=model_spec, journal_path=journal_path
        )
        / model_spec.file
    )


def sidecar_path(
    *,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    journal_path: str | Path | None = None,
) -> Path:
    return asset_dir(engine_spec=engine_spec, journal_path=journal_path) / SIDECAR_NAME


def _engine_file_spec(engine_spec: CedEngineSpec, artifact_key: str) -> CedFileSpec:
    try:
        return engine_spec.artifacts[artifact_key]
    except KeyError as exc:
        raise CedInstallError(
            "unsupported_platform", f"ced assets unsupported for {artifact_key}"
        ) from exc


def _model_file_spec(model_spec: CedModelSpec) -> CedFileSpec:
    return CedFileSpec(
        path=model_spec.file,
        sha256=model_spec.sha256,
        size_bytes=model_spec.size_bytes,
    )


def _expected_files(
    engine_spec: CedEngineSpec, model_spec: CedModelSpec, artifact_key: str
) -> dict[str, str]:
    engine_file = _engine_file_spec(engine_spec, artifact_key)
    return {
        f"engine/{artifact_key}/{engine_file.path}": engine_file.sha256,
        f"models/{model_spec.repo}/{model_spec.revision}/{model_spec.file}": model_spec.sha256,
    }


def _record_for_spec(
    engine_spec: CedEngineSpec, model_spec: CedModelSpec, artifact_key: str
) -> CedInstallRecord:
    return CedInstallRecord(
        engine_version=engine_spec.version,
        artifact_key=artifact_key,
        model_repo=model_spec.repo,
        model_revision=model_spec.revision,
        files=_expected_files(engine_spec, model_spec, artifact_key),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_file(path: Path, file_spec: CedFileSpec) -> None:
    if not path.is_file():
        raise CedInstallError("file_missing", f"ced asset missing: {file_spec.path}")
    actual_size = path.stat().st_size
    if actual_size != file_spec.size_bytes:
        raise CedInstallError(
            "size_mismatch",
            (
                f"size mismatch for {file_spec.path}: "
                f"expected {file_spec.size_bytes}, got {actual_size}"
            ),
        )
    actual_sha256 = _sha256_file(path)
    if actual_sha256 != file_spec.sha256:
        raise CedInstallError(
            "sha256_mismatch",
            (
                f"sha256 mismatch for {file_spec.path}: "
                f"expected {file_spec.sha256}, got {actual_sha256}"
            ),
        )


def _tmp_path(dest: Path) -> Path:
    return dest.with_name(f"{dest.name}.tmp")


def _download_file(
    url: str,
    dest: Path,
    file_spec: CedFileSpec,
    *,
    timeout_s: float = 600.0,
) -> None:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(dest)
    dest.unlink(missing_ok=True)
    tmp.unlink(missing_ok=True)
    try:
        with httpx.stream(
            "GET", url, timeout=timeout_s, follow_redirects=True
        ) as response:
            response.raise_for_status()
            with tmp.open("wb") as handle:
                for chunk in response.iter_bytes():
                    if chunk:
                        handle.write(chunk)
        _verify_file(tmp, file_spec)
        tmp.replace(dest)
    except CedInstallError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise CedInstallError(
            "download_failed", f"failed to download {file_spec.path}: {exc}"
        ) from exc


def _write_sidecar(path: Path, record: CedInstallRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as handle:
        handle.write(record.to_json())
        tmp_path = Path(handle.name)
    tmp_path.replace(path)


def _engine_tarball_path(
    artifact_key: str,
    *,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    journal_path: str | Path | None = None,
) -> Path:
    return (
        asset_dir(engine_spec=engine_spec, journal_path=journal_path)
        / "downloads"
        / _engine_file_spec(engine_spec, artifact_key).path
    )


def _engine_extract_tmp_dir(
    artifact_key: str,
    *,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    journal_path: str | Path | None = None,
) -> Path:
    return (
        asset_dir(engine_spec=engine_spec, journal_path=journal_path)
        / "engine"
        / f".{artifact_key}.extract"
    )


def _engine_stage_dir(
    artifact_key: str,
    *,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    journal_path: str | Path | None = None,
) -> Path:
    return (
        asset_dir(engine_spec=engine_spec, journal_path=journal_path)
        / "engine"
        / f".{artifact_key}.stage"
    )


def _find_extracted_file(dest: Path, filename: str) -> Path:
    direct = dest / filename
    if direct.is_file():
        return direct
    matches = [path for path in dest.rglob(filename) if path.is_file()]
    if not matches:
        raise CedInstallError(
            "file_missing", f"extracted ced archive did not contain {filename}"
        )
    if len(matches) > 1:
        matches.sort(key=lambda path: len(path.parts))
    return matches[0]


def _extract_engine_tarball(tarball: Path, dest: Path) -> None:
    try:
        _safe_extract_tarball(tarball, dest)
    except ParakeetProviderError as exc:
        raise CedInstallError(exc.reason_code, str(exc)) from exc


def _install_engine(
    artifact_key: str,
    *,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    journal_path: str | Path | None = None,
) -> None:
    file_spec = _engine_file_spec(engine_spec, artifact_key)
    tarball = _engine_tarball_path(
        artifact_key, engine_spec=engine_spec, journal_path=journal_path
    )
    extract_tmp = _engine_extract_tmp_dir(
        artifact_key, engine_spec=engine_spec, journal_path=journal_path
    )
    stage = _engine_stage_dir(
        artifact_key, engine_spec=engine_spec, journal_path=journal_path
    )
    final_dir = engine_dir(
        artifact_key, engine_spec=engine_spec, journal_path=journal_path
    )

    _download_file(f"{ENGINE_RELEASE_BASE_URL}/{file_spec.path}", tarball, file_spec)
    shutil.rmtree(extract_tmp, ignore_errors=True)
    shutil.rmtree(stage, ignore_errors=True)
    try:
        _extract_engine_tarball(tarball, extract_tmp)
        stage.mkdir(parents=True, exist_ok=True)
        for name in (
            engine_spec.lib_names[artifact_key],
            engine_spec.header_name,
            *_ENGINE_REQUIRED_FILES,
        ):
            shutil.copy2(_find_extracted_file(extract_tmp, name), stage / name)
        if final_dir.exists():
            shutil.rmtree(final_dir)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        stage.rename(final_dir)
    finally:
        tarball.unlink(missing_ok=True)
        _tmp_path(tarball).unlink(missing_ok=True)
        shutil.rmtree(extract_tmp, ignore_errors=True)
        shutil.rmtree(stage, ignore_errors=True)


def _install_model(
    *,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    model_spec: CedModelSpec = CED_MODEL_SPEC,
    journal_path: str | Path | None = None,
) -> None:
    file_spec = _model_file_spec(model_spec)
    url = f"https://huggingface.co/{model_spec.repo}/resolve/{model_spec.revision}/{model_spec.file}"
    _download_file(
        url,
        model_path(
            engine_spec=engine_spec, model_spec=model_spec, journal_path=journal_path
        ),
        file_spec,
    )


def _check_nonempty_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise CedInstallError("file_missing", f"ced asset missing: {label} at {path}")
    if path.stat().st_size <= 0:
        raise CedInstallError("size_mismatch", f"ced asset is empty: {label} at {path}")


def _check_model_size(path: Path, model_spec: CedModelSpec) -> None:
    if not path.is_file():
        raise CedInstallError("file_missing", f"ced model missing: {path}")
    actual_size = path.stat().st_size
    if actual_size != model_spec.size_bytes:
        raise CedInstallError(
            "size_mismatch",
            (
                f"size mismatch for {model_spec.file}: "
                f"expected {model_spec.size_bytes}, got {actual_size}"
            ),
        )


def _cleanup_partial_install(
    artifact_key: str | None = None,
    *,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    model_spec: CedModelSpec = CED_MODEL_SPEC,
    journal_path: str | Path | None = None,
) -> None:
    resolved = artifact_key or ced_engine_artifact_key()
    if resolved is not None:
        final_dir = engine_dir(
            resolved, engine_spec=engine_spec, journal_path=journal_path
        )
        shutil.rmtree(final_dir, ignore_errors=True)
        shutil.rmtree(
            _engine_extract_tmp_dir(
                resolved, engine_spec=engine_spec, journal_path=journal_path
            ),
            ignore_errors=True,
        )
        shutil.rmtree(
            _engine_stage_dir(
                resolved, engine_spec=engine_spec, journal_path=journal_path
            ),
            ignore_errors=True,
        )
        tarball = _engine_tarball_path(
            resolved, engine_spec=engine_spec, journal_path=journal_path
        )
        tarball.unlink(missing_ok=True)
        _tmp_path(tarball).unlink(missing_ok=True)

    model = model_path(
        engine_spec=engine_spec, model_spec=model_spec, journal_path=journal_path
    )
    model.unlink(missing_ok=True)
    _tmp_path(model).unlink(missing_ok=True)
    sidecar_path(engine_spec=engine_spec, journal_path=journal_path).unlink(
        missing_ok=True
    )


def check_ced_assets(
    *,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    model_spec: CedModelSpec = CED_MODEL_SPEC,
    journal_path: str | Path | None = None,
) -> CedInstallRecord | None:
    artifact_key = ced_engine_artifact_key()
    if artifact_key is None:
        return None
    sidecar = sidecar_path(engine_spec=engine_spec, journal_path=journal_path)
    if not sidecar.is_file():
        raise CedInstallError("sidecar_missing", f"ced sidecar missing: {sidecar}")
    try:
        record = CedInstallRecord.from_json(sidecar.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CedInstallError(
            "sidecar_invalid", f"ced sidecar invalid: {sidecar}: {exc}"
        ) from exc
    if (
        record.engine_version != engine_spec.version
        or record.artifact_key != artifact_key
        or record.model_repo != model_spec.repo
        or record.model_revision != model_spec.revision
    ):
        raise CedInstallError(
            "sidecar_mismatch",
            "ced sidecar does not match pinned engine/model spec",
        )
    if record.files != _expected_files(engine_spec, model_spec, artifact_key):
        raise CedInstallError(
            "sidecar_mismatch", "ced sidecar file digests do not match pins"
        )

    _check_model_size(
        model_path(
            engine_spec=engine_spec, model_spec=model_spec, journal_path=journal_path
        ),
        model_spec,
    )
    _check_nonempty_file(
        engine_lib_path(
            artifact_key, engine_spec=engine_spec, journal_path=journal_path
        ),
        engine_spec.lib_names[artifact_key],
    )
    _check_nonempty_file(
        engine_header_path(
            artifact_key, engine_spec=engine_spec, journal_path=journal_path
        ),
        engine_spec.header_name,
    )
    for name in _ENGINE_REQUIRED_FILES:
        _check_nonempty_file(
            engine_dir(artifact_key, engine_spec=engine_spec, journal_path=journal_path)
            / name,
            name,
        )
    return record


def install_ced_assets(
    *,
    force: bool = False,
    engine_spec: CedEngineSpec = CED_ENGINE_SPEC,
    model_spec: CedModelSpec = CED_MODEL_SPEC,
    journal_path: str | Path | None = None,
) -> CedInstallRecord | None:
    artifact_key = ced_engine_artifact_key()
    if artifact_key is None:
        return None
    if not force:
        try:
            return check_ced_assets(
                engine_spec=engine_spec,
                model_spec=model_spec,
                journal_path=journal_path,
            )
        except CedInstallError:
            pass

    try:
        sidecar_path(engine_spec=engine_spec, journal_path=journal_path).unlink(
            missing_ok=True
        )
        _install_engine(
            artifact_key, engine_spec=engine_spec, journal_path=journal_path
        )
        _install_model(
            engine_spec=engine_spec, model_spec=model_spec, journal_path=journal_path
        )
        record = _record_for_spec(engine_spec, model_spec, artifact_key)
        _write_sidecar(
            sidecar_path(engine_spec=engine_spec, journal_path=journal_path), record
        )
        return record
    except CedInstallError:
        _cleanup_partial_install(
            artifact_key,
            engine_spec=engine_spec,
            model_spec=model_spec,
            journal_path=journal_path,
        )
        raise
    except Exception as exc:
        _cleanup_partial_install(
            artifact_key,
            engine_spec=engine_spec,
            model_spec=model_spec,
            journal_path=journal_path,
        )
        raise CedInstallError("install_failed", f"ced install failed: {exc}") from exc
