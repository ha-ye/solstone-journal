# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Install and verify local rf-detr.cpp provider artifacts.

This module performs no network access at import time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from solstone.think.utils import get_journal

LOG = logging.getLogger(__name__)

ENGINE_REF = "65c0ffcc"
MODEL_NAME = "rfdetr-nano-f16"
ENGINE_BINARY_NAME = "rfdetr-cli"
SIDECAR_NAME = ".rfdetr-install.json"


class RfdetrInstallError(RuntimeError):
    """rf-detr.cpp artifact acquisition failure with a recovery reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class RfdetrEngineSpec:
    ref: str
    release_tag: str
    tarball_name: str
    tarball_sha256: str
    binary_name: str
    binary_sha256: str


@dataclass(frozen=True)
class RfdetrModelSpec:
    repo: str
    revision: str
    filename: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class RfdetrSpec:
    engine: RfdetrEngineSpec
    model: RfdetrModelSpec


@dataclass(frozen=True)
class RfdetrFileSpec:
    path: str
    sha256: str
    size_bytes: int | None = None


RFDETR_SPEC = RfdetrSpec(
    engine=RfdetrEngineSpec(
        ref=ENGINE_REF,
        release_tag=f"bin-{ENGINE_REF}-1",
        tarball_name=f"rfdetr-cli-{ENGINE_REF}-linux-cpu-x64.tar.gz",
        tarball_sha256=(
            "74f3258a94c975444923be0cc451d90c1e8d9e2595d3cab6876a11086d8357dd"
        ),
        binary_name=ENGINE_BINARY_NAME,
        binary_sha256=(
            "7c4fb4d499d53509d5099e768510a164c6647b84480c72170b865233504f367c"
        ),
    ),
    model=RfdetrModelSpec(
        repo="mudler/rfdetr-cpp-nano",
        revision="c3dc0c037df499f5503545247df6618415fca643",
        filename=f"{MODEL_NAME}.gguf",
        sha256="d798cc448faa53209b88fc905c91beb1dd104634b95f6948cc4877540a8fd3ee",
        size_bytes=63439488,
    ),
)

RfdetrInstallStatus = Literal["installed", "platform_unavailable"]


@dataclass(frozen=True)
class RfdetrInstallRecord:
    status: RfdetrInstallStatus
    engine_ref: str | None = None
    engine_sha256: str | None = None
    model_repo: str | None = None
    model_revision: str | None = None
    model_file: str | None = None
    model_sha256: str | None = None

    def to_json(self) -> str:
        data = {
            "status": self.status,
            "engine_ref": self.engine_ref,
            "engine_sha256": self.engine_sha256,
            "model_repo": self.model_repo,
            "model_revision": self.model_revision,
            "model_file": self.model_file,
            "model_sha256": self.model_sha256,
        }
        return (
            json.dumps(
                {key: value for key, value in data.items() if value is not None},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    @classmethod
    def from_json(cls, text: str) -> RfdetrInstallRecord:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("rf-detr install record must be an object")
        status = data.get("status")
        if status not in {"installed", "platform_unavailable"}:
            raise ValueError(
                "rf-detr install record status must be installed or "
                "platform_unavailable"
            )
        if status == "platform_unavailable":
            return cls(status="platform_unavailable")

        engine_ref = data.get("engine_ref")
        engine_sha256 = data.get("engine_sha256")
        model_repo = data.get("model_repo")
        model_revision = data.get("model_revision")
        model_file = data.get("model_file")
        model_sha256 = data.get("model_sha256")
        for name, value in (
            ("engine_ref", engine_ref),
            ("engine_sha256", engine_sha256),
            ("model_repo", model_repo),
            ("model_revision", model_revision),
            ("model_file", model_file),
            ("model_sha256", model_sha256),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"rf-detr install record {name} must be a non-empty string"
                )

        return cls(
            status="installed",
            engine_ref=engine_ref,
            engine_sha256=engine_sha256,
            model_repo=model_repo,
            model_revision=model_revision,
            model_file=model_file,
            model_sha256=model_sha256,
        )


@dataclass(frozen=True)
class RfdetrPaths:
    status: Literal["installed", "not_installed", "platform_unavailable"]
    binary_path: Path | None = None
    model_path: Path | None = None


def _platform_info() -> tuple[str, str]:
    os_name = "linux" if sys.platform.startswith("linux") else sys.platform
    return os_name, platform.machine().lower()


def _rfdetr_platform_supported(
    os_name: str | None = None, arch: str | None = None
) -> bool:
    if os_name is None or arch is None:
        os_name, arch = _platform_info()
    return os_name == "linux" and arch.lower() in {"amd64", "x64", "x86_64"}


def cache_root(journal_path: str | Path | None = None) -> Path:
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return root / "cache" / "providers" / "rfdetr"


def binary_path(
    *, spec: RfdetrSpec = RFDETR_SPEC, journal_path: str | Path | None = None
) -> Path:
    return (
        cache_root(journal_path) / "engine" / spec.engine.ref / spec.engine.binary_name
    )


def model_path(
    *, spec: RfdetrSpec = RFDETR_SPEC, journal_path: str | Path | None = None
) -> Path:
    return (
        cache_root(journal_path) / "model" / spec.model.revision / spec.model.filename
    )


def sidecar_path(*, journal_path: str | Path | None = None) -> Path:
    return cache_root(journal_path) / SIDECAR_NAME


def _engine_extract_dir(
    spec: RfdetrSpec, journal_path: str | Path | None = None
) -> Path:
    return binary_path(spec=spec, journal_path=journal_path).parent / ".extract"


def _engine_tarball_path(
    spec: RfdetrSpec, journal_path: str | Path | None = None
) -> Path:
    return (
        binary_path(spec=spec, journal_path=journal_path).parent
        / spec.engine.tarball_name
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_file(path: Path, file_spec: RfdetrFileSpec) -> None:
    if not path.is_file():
        raise RfdetrInstallError(
            "file_missing", f"rf-detr asset missing: {file_spec.path}"
        )
    if file_spec.size_bytes is not None:
        actual_size = path.stat().st_size
        if actual_size != file_spec.size_bytes:
            raise RfdetrInstallError(
                "size_mismatch",
                (
                    f"size mismatch for {file_spec.path}: "
                    f"expected {file_spec.size_bytes}, got {actual_size}"
                ),
            )
    actual_sha256 = _sha256_file(path)
    if actual_sha256 != file_spec.sha256:
        raise RfdetrInstallError(
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
    file_spec: RfdetrFileSpec,
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
    except RfdetrInstallError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise RfdetrInstallError(
            "download_failed", f"failed to download {file_spec.path}: {exc}"
        ) from exc


def _write_sidecar(path: Path, record: RfdetrInstallRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as handle:
        handle.write(record.to_json())
        tmp_path = Path(handle.name)
    tmp_path.replace(path)


def _safe_extract_tarball(tarball: Path, dest: Path) -> None:
    import tarfile

    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with tarfile.open(tarball, "r:*") as archive:
        for member in archive.getmembers():
            target = (dest / member.name).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise RfdetrInstallError(
                    "archive_path_traversal",
                    f"Unsafe tar member path: {member.name}",
                )
        archive.extractall(dest)


def _find_extracted_binary(dest: Path, binary_name: str) -> Path:
    direct = dest / binary_name
    if direct.exists():
        return direct
    matches = [path for path in dest.rglob(binary_name) if path.is_file()]
    if not matches:
        raise RfdetrInstallError(
            "binary_missing",
            f"Extracted archive did not contain {binary_name}",
        )
    if len(matches) > 1:
        matches.sort(key=lambda path: len(path.parts))
    return matches[0]


def _chmod_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _engine_url(spec: RfdetrSpec) -> str:
    return (
        "https://github.com/solpbc/rf-detr.cpp/releases/download/"
        f"{spec.engine.release_tag}/{spec.engine.tarball_name}"
    )


def _model_url(spec: RfdetrSpec) -> str:
    return (
        f"https://huggingface.co/{spec.model.repo}/resolve/"
        f"{spec.model.revision}/{spec.model.filename}"
    )


def _install_engine(spec: RfdetrSpec, journal_path: str | Path | None = None) -> None:
    tarball_dest = _engine_tarball_path(spec, journal_path)
    extract_dir = _engine_extract_dir(spec, journal_path)
    final_path = binary_path(spec=spec, journal_path=journal_path)
    _download_file(
        _engine_url(spec),
        tarball_dest,
        RfdetrFileSpec(
            spec.engine.tarball_name,
            spec.engine.tarball_sha256,
            None,
        ),
    )
    shutil.rmtree(extract_dir, ignore_errors=True)
    _safe_extract_tarball(tarball_dest, extract_dir)
    found = _find_extracted_binary(extract_dir, spec.engine.binary_name)
    _verify_file(
        found,
        RfdetrFileSpec(
            spec.engine.binary_name,
            spec.engine.binary_sha256,
            None,
        ),
    )
    _chmod_executable(found)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    found.replace(final_path)
    shutil.rmtree(extract_dir, ignore_errors=True)
    tarball_dest.unlink(missing_ok=True)


def _install_model(spec: RfdetrSpec, journal_path: str | Path | None = None) -> None:
    _download_file(
        _model_url(spec),
        model_path(spec=spec, journal_path=journal_path),
        RfdetrFileSpec(
            spec.model.filename,
            spec.model.sha256,
            spec.model.size_bytes,
        ),
    )


def _record_for_spec(spec: RfdetrSpec) -> RfdetrInstallRecord:
    return RfdetrInstallRecord(
        status="installed",
        engine_ref=spec.engine.ref,
        engine_sha256=spec.engine.binary_sha256,
        model_repo=spec.model.repo,
        model_revision=spec.model.revision,
        model_file=spec.model.filename,
        model_sha256=spec.model.sha256,
    )


def _cleanup_partial_install(
    *, spec: RfdetrSpec = RFDETR_SPEC, journal_path: str | Path | None = None
) -> None:
    binary = binary_path(spec=spec, journal_path=journal_path)
    binary.unlink(missing_ok=True)
    _tmp_path(binary).unlink(missing_ok=True)
    tarball = _engine_tarball_path(spec, journal_path)
    tarball.unlink(missing_ok=True)
    _tmp_path(tarball).unlink(missing_ok=True)
    shutil.rmtree(_engine_extract_dir(spec, journal_path), ignore_errors=True)
    model = model_path(spec=spec, journal_path=journal_path)
    model.unlink(missing_ok=True)
    _tmp_path(model).unlink(missing_ok=True)
    sidecar_path(journal_path=journal_path).unlink(missing_ok=True)


def check_rfdetr_model(
    *, spec: RfdetrSpec = RFDETR_SPEC, journal_path: str | Path | None = None
) -> RfdetrInstallRecord:
    # Deliberately diverges from rerank: unsupported hosts are a clean check pass.
    if not _rfdetr_platform_supported():
        return RfdetrInstallRecord(status="platform_unavailable")

    sidecar = sidecar_path(journal_path=journal_path)
    if not sidecar.is_file():
        raise RfdetrInstallError(
            "sidecar_missing", f"rf-detr sidecar missing: {sidecar}"
        )
    try:
        record = RfdetrInstallRecord.from_json(sidecar.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RfdetrInstallError(
            "sidecar_invalid", f"rf-detr sidecar invalid: {sidecar}: {exc}"
        ) from exc
    if record.status == "platform_unavailable":
        return record
    if (
        record.engine_ref != spec.engine.ref
        or record.engine_sha256 != spec.engine.binary_sha256
        or record.model_repo != spec.model.repo
        or record.model_revision != spec.model.revision
        or record.model_file != spec.model.filename
        or record.model_sha256 != spec.model.sha256
    ):
        raise RfdetrInstallError(
            "sidecar_mismatch", "rf-detr sidecar does not match pinned artifacts"
        )
    _verify_file(
        binary_path(spec=spec, journal_path=journal_path),
        RfdetrFileSpec(
            spec.engine.binary_name,
            spec.engine.binary_sha256,
            None,
        ),
    )
    _verify_file(
        model_path(spec=spec, journal_path=journal_path),
        RfdetrFileSpec(
            spec.model.filename,
            spec.model.sha256,
            spec.model.size_bytes,
        ),
    )
    return record


def install_rfdetr(
    *,
    force: bool = False,
    spec: RfdetrSpec = RFDETR_SPEC,
    journal_path: str | Path | None = None,
) -> RfdetrInstallRecord:
    os_name, arch = _platform_info()
    if not _rfdetr_platform_supported(os_name, arch):
        record = RfdetrInstallRecord(status="platform_unavailable")
        _write_sidecar(sidecar_path(journal_path=journal_path), record)
        LOG.info("rf-detr.cpp platform unavailable on %s/%s", os_name, arch)
        return record

    if not force:
        try:
            return check_rfdetr_model(spec=spec, journal_path=journal_path)
        except RfdetrInstallError:
            pass

    from solstone.think.providers.fit_report import (
        build_rfdetr_fit_report,
        render_fit_report,
    )

    report = build_rfdetr_fit_report(journal_path)
    rendered = render_fit_report(report)
    if report.overall == "blocked":
        raise RfdetrInstallError("host_unfit", rendered)
    if report.overall == "warning":
        LOG.warning("rf-detr.cpp host fit warning:\n%s", rendered)

    try:
        _cleanup_partial_install(spec=spec, journal_path=journal_path)
        _install_engine(spec, journal_path)
        _install_model(spec, journal_path)
        record = _record_for_spec(spec)
        _write_sidecar(sidecar_path(journal_path=journal_path), record)
        return record
    except RfdetrInstallError:
        _cleanup_partial_install(spec=spec, journal_path=journal_path)
        raise
    except Exception as exc:
        _cleanup_partial_install(spec=spec, journal_path=journal_path)
        raise RfdetrInstallError(
            "install_failed", f"rf-detr install failed: {exc}"
        ) from exc


def rfdetr_paths(
    *, spec: RfdetrSpec = RFDETR_SPEC, journal_path: str | Path | None = None
) -> RfdetrPaths:
    if not _rfdetr_platform_supported():
        return RfdetrPaths(status="platform_unavailable")
    try:
        record = check_rfdetr_model(spec=spec, journal_path=journal_path)
    except RfdetrInstallError:
        return RfdetrPaths(status="not_installed")
    if record.status == "platform_unavailable":
        return RfdetrPaths(status="not_installed")
    return RfdetrPaths(
        status="installed",
        binary_path=binary_path(spec=spec, journal_path=journal_path),
        model_path=model_path(spec=spec, journal_path=journal_path),
    )


__all__ = [
    "ENGINE_REF",
    "MODEL_NAME",
    "RFDETR_SPEC",
    "RfdetrEngineSpec",
    "RfdetrInstallError",
    "RfdetrInstallRecord",
    "RfdetrModelSpec",
    "RfdetrPaths",
    "RfdetrSpec",
    "binary_path",
    "cache_root",
    "check_rfdetr_model",
    "install_rfdetr",
    "model_path",
    "rfdetr_paths",
    "sidecar_path",
]
