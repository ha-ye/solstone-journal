# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Install and locate NVIDIA nvattest runtime artifacts.

This module performs no network access at import time.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from solstone.think.providers.rfdetr_install import (
    RfdetrInstallError,
)
from solstone.think.providers.rfdetr_install import (
    _safe_extract_tarball as _rfdetr_safe_extract_tarball,
)
from solstone.think.utils import get_journal

SPP_NVATTEST_DIR_ENV = "SPP_NVATTEST_DIR"
NVATTEST_VERSION = "1.2.2"
NVATTEST_ARCHIVE_NAME = "libnvat-linux-x86_64-1.2.2.1780962352-archive.tar.xz"
NVATTEST_ARCHIVE_URL = (
    "https://developer.download.nvidia.com/compute/nvat/redist/libnvat/"
    f"linux-x86_64/{NVATTEST_ARCHIVE_NAME}"
)
NVATTEST_ARCHIVE_SHA256 = (
    "3f10da6fca794b7e3025c6645447947ec8bc45bcfde5b5b1d23241c7115630db"
)
SIDECAR_NAME = ".nvattest-install.json"


class NvattestInstallError(RuntimeError):
    """nvattest artifact acquisition failure with a recovery reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class NvattestArchiveSpec:
    version: str
    url: str
    archive_name: str
    sha256: str


@dataclass(frozen=True, slots=True)
class NvattestInstallRecord:
    version: str
    archive_name: str
    archive_sha256: str

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "archive_name": self.archive_name,
                    "archive_sha256": self.archive_sha256,
                    "version": self.version,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


NVATTEST_ARCHIVE_SPEC = NvattestArchiveSpec(
    version=NVATTEST_VERSION,
    url=NVATTEST_ARCHIVE_URL,
    archive_name=NVATTEST_ARCHIVE_NAME,
    sha256=NVATTEST_ARCHIVE_SHA256,
)


def cache_root(journal_path: str | Path | None = None) -> Path:
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return root / "cache" / "providers" / "nvattest"


def resolve_nvattest_dir(
    explicit_override: str | Path | None = None,
    *,
    journal_path: str | Path | None = None,
) -> Path:
    """Resolve the nvattest directory from override, env, then journal cache."""

    if explicit_override is not None:
        return Path(explicit_override).expanduser()
    env_path = os.environ.get(SPP_NVATTEST_DIR_ENV)
    if env_path:
        return Path(env_path).expanduser()
    return cache_root(journal_path)


def install_nvattest(
    *,
    force: bool = False,
    spec: NvattestArchiveSpec = NVATTEST_ARCHIVE_SPEC,
    journal_path: str | Path | None = None,
) -> Path:
    """Download, verify, and install nvattest into the journal provider cache."""

    root = cache_root(journal_path)
    if not force and _installed(root, spec):
        return root

    archive = _archive_path(spec, journal_path)
    extract_dir = root / ".extract"
    _download_file(spec.url, archive, spec.sha256)
    shutil.rmtree(extract_dir, ignore_errors=True)
    try:
        _safe_extract_nvattest_tarball(archive, extract_dir)
        source = _find_extracted_root(extract_dir)
        _install_extracted_tree(source, root)
        _write_sidecar(
            root / SIDECAR_NAME,
            NvattestInstallRecord(
                version=spec.version,
                archive_name=spec.archive_name,
                archive_sha256=spec.sha256,
            ),
        )
        return root
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)
        archive.unlink(missing_ok=True)


def _has_runtime_layout(root: Path) -> bool:
    return (root / "bin" / "nvattest").is_file() and (root / "lib").is_dir()


def _installed(root: Path, spec: NvattestArchiveSpec) -> bool:
    if not _has_runtime_layout(root):
        return False
    try:
        data = json.loads((root / SIDECAR_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    return (
        data.get("archive_sha256") == spec.sha256
        and data.get("version") == spec.version
    )


def _archive_path(
    spec: NvattestArchiveSpec,
    journal_path: str | Path | None = None,
) -> Path:
    return cache_root(journal_path) / ".downloads" / spec.archive_name


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_file(path: Path, expected_sha256: str) -> None:
    if not path.is_file():
        raise NvattestInstallError("file_missing", f"nvattest asset missing: {path}")
    actual_sha256 = _sha256_file(path)
    if actual_sha256 != expected_sha256:
        raise NvattestInstallError(
            "sha256_mismatch",
            (
                f"sha256 mismatch for {path.name}: "
                f"expected {expected_sha256}, got {actual_sha256}"
            ),
        )


def _tmp_path(dest: Path) -> Path:
    return dest.with_name(f"{dest.name}.tmp")


def _download_file(url: str, dest: Path, expected_sha256: str) -> None:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(dest)
    dest.unlink(missing_ok=True)
    tmp.unlink(missing_ok=True)
    try:
        with httpx.stream("GET", url, timeout=600.0, follow_redirects=True) as response:
            response.raise_for_status()
            with tmp.open("wb") as handle:
                for chunk in response.iter_bytes():
                    if chunk:
                        handle.write(chunk)
        _verify_file(tmp, expected_sha256)
        tmp.replace(dest)
    except NvattestInstallError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise NvattestInstallError(
            "download_failed",
            f"failed to download nvattest archive: {exc}",
        ) from exc


def _safe_extract_nvattest_tarball(tarball: Path, dest: Path) -> None:
    try:
        _rfdetr_safe_extract_tarball(tarball, dest)
    except RfdetrInstallError as exc:
        reason_code = getattr(exc, "reason_code", "archive_extract_failed")
        if reason_code == "archive_path_traversal":
            raise NvattestInstallError(reason_code, str(exc)) from exc
        raise NvattestInstallError("archive_extract_failed", str(exc)) from exc


def _find_extracted_root(extract_dir: Path) -> Path:
    if _has_runtime_layout(extract_dir):
        return extract_dir
    matches = [
        path
        for path in extract_dir.rglob("nvattest")
        if path.is_file()
        and path.parent.name == "bin"
        and (path.parent.parent / "lib").is_dir()
    ]
    if len(matches) != 1:
        raise NvattestInstallError(
            "archive_layout_invalid",
            f"expected exactly one extracted nvattest binary, found {len(matches)}",
        )
    return matches[0].parent.parent


def _install_extracted_tree(source: Path, root: Path) -> None:
    binary = source / "bin" / "nvattest"
    lib_dir = source / "lib"
    if not binary.is_file() or not lib_dir.is_dir():
        raise NvattestInstallError(
            "archive_layout_invalid",
            "extracted archive must contain bin/nvattest and lib/",
        )

    root.mkdir(parents=True, exist_ok=True)
    for name in ("bin", "lib", "include", "share"):
        shutil.rmtree(root / name, ignore_errors=True)
    for name in ("LICENSE",):
        (root / name).unlink(missing_ok=True)

    for name in ("bin", "lib", "include", "share"):
        src = source / name
        if src.exists():
            shutil.move(str(src), str(root / name))
    license_src = source / "LICENSE"
    if license_src.exists():
        shutil.move(str(license_src), str(root / "LICENSE"))
    _chmod_executable(root / "bin" / "nvattest")


def _chmod_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_sidecar(path: Path, record: NvattestInstallRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as handle:
        tmp_path = Path(handle.name)
        handle.write(record.to_json())
    tmp_path.replace(path)
