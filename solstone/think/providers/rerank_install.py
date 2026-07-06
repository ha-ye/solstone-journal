# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Install and verify local rerank cross-encoder provider artifacts.

This module performs no network access at import time.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from solstone.think.utils import get_journal

SIDECAR_NAME = ".rerank-install.json"


class RerankInstallError(RuntimeError):
    """Rerank artifact acquisition failure with a recovery reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class RerankFileSpec:
    path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class RerankModelSpec:
    repo: str
    revision: str
    files: tuple[RerankFileSpec, ...]


@dataclass(frozen=True)
class RerankInstallRecord:
    repo: str
    revision: str
    files: dict[str, str]

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "files": self.files,
                    "repo": self.repo,
                    "revision": self.revision,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    @classmethod
    def from_json(cls, text: str) -> RerankInstallRecord:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("rerank install record must be an object")
        repo = data.get("repo")
        revision = data.get("revision")
        files = data.get("files")
        if not isinstance(repo, str) or not repo:
            raise ValueError("rerank install record repo must be a non-empty string")
        if not isinstance(revision, str) or not revision:
            raise ValueError(
                "rerank install record revision must be a non-empty string"
            )
        if not isinstance(files, dict):
            raise ValueError("rerank install record files must be an object")
        normalized: dict[str, str] = {}
        for name, digest in files.items():
            if not isinstance(name, str) or not name:
                raise ValueError("rerank install record file names must be strings")
            if not isinstance(digest, str) or not digest:
                raise ValueError("rerank install record file digests must be strings")
            normalized[name] = digest
        return cls(repo=repo, revision=revision, files=normalized)


RERANK_MODEL_SPEC = RerankModelSpec(
    repo="Xenova/ms-marco-MiniLM-L-6-v2",
    revision="a09144355adeed5f58c8ed011d209bf8ee5a1fec",
    files=(
        RerankFileSpec(
            path="onnx/model.onnx",
            size_bytes=90992115,
            sha256="c623d0bcb99f4622beb413eaef00cfbe5db20df9f1dd982da4b4f26022881870",
        ),
        RerankFileSpec(
            path="tokenizer.json",
            size_bytes=711396,
            sha256="d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
        ),
    ),
)


def cache_root(journal_path: str | Path | None = None) -> Path:
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return root / "cache" / "providers" / "rerank"


def asset_dir(
    *, spec: RerankModelSpec = RERANK_MODEL_SPEC, journal_path: str | Path | None = None
) -> Path:
    return cache_root(journal_path) / spec.revision


def _relative_asset_path(file_spec: RerankFileSpec) -> Path:
    rel = Path(file_spec.path)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise RerankInstallError(
            "invalid_file_path", f"invalid rerank asset path: {file_spec.path!r}"
        )
    return rel


def asset_path(
    file_spec: RerankFileSpec,
    *,
    spec: RerankModelSpec = RERANK_MODEL_SPEC,
    journal_path: str | Path | None = None,
) -> Path:
    return asset_dir(spec=spec, journal_path=journal_path) / _relative_asset_path(
        file_spec
    )


def sidecar_path(
    *, spec: RerankModelSpec = RERANK_MODEL_SPEC, journal_path: str | Path | None = None
) -> Path:
    return asset_dir(spec=spec, journal_path=journal_path) / SIDECAR_NAME


def _expected_files(spec: RerankModelSpec) -> dict[str, str]:
    return {file_spec.path: file_spec.sha256 for file_spec in spec.files}


def _record_for_spec(spec: RerankModelSpec) -> RerankInstallRecord:
    return RerankInstallRecord(
        repo=spec.repo,
        revision=spec.revision,
        files=_expected_files(spec),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_file(path: Path, file_spec: RerankFileSpec) -> None:
    if not path.is_file():
        raise RerankInstallError(
            "file_missing", f"rerank asset missing: {file_spec.path}"
        )
    actual_size = path.stat().st_size
    if actual_size != file_spec.size_bytes:
        raise RerankInstallError(
            "size_mismatch",
            (
                f"size mismatch for {file_spec.path}: "
                f"expected {file_spec.size_bytes}, got {actual_size}"
            ),
        )
    actual_sha256 = _sha256_file(path)
    if actual_sha256 != file_spec.sha256:
        raise RerankInstallError(
            "sha256_mismatch",
            (
                f"sha256 mismatch for {file_spec.path}: "
                f"expected {file_spec.sha256}, got {actual_sha256}"
            ),
        )


def _download_url(spec: RerankModelSpec, file_spec: RerankFileSpec) -> str:
    return (
        f"https://huggingface.co/{spec.repo}/resolve/{spec.revision}/{file_spec.path}"
    )


def _tmp_path(dest: Path) -> Path:
    return dest.with_name(f"{dest.name}.tmp")


def _download_file(
    url: str,
    dest: Path,
    file_spec: RerankFileSpec,
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
    except RerankInstallError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise RerankInstallError(
            "download_failed", f"failed to download {file_spec.path}: {exc}"
        ) from exc


def _write_sidecar(path: Path, record: RerankInstallRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as handle:
        handle.write(record.to_json())
        tmp_path = Path(handle.name)
    tmp_path.replace(path)


def _cleanup_partial_install(
    *, spec: RerankModelSpec, journal_path: str | Path | None = None
) -> None:
    for file_spec in spec.files:
        path = asset_path(file_spec, spec=spec, journal_path=journal_path)
        path.unlink(missing_ok=True)
        _tmp_path(path).unlink(missing_ok=True)
    sidecar_path(spec=spec, journal_path=journal_path).unlink(missing_ok=True)


def check_rerank_model(
    *, spec: RerankModelSpec = RERANK_MODEL_SPEC, journal_path: str | Path | None = None
) -> RerankInstallRecord:
    sidecar = sidecar_path(spec=spec, journal_path=journal_path)
    if not sidecar.is_file():
        raise RerankInstallError(
            "sidecar_missing", f"rerank sidecar missing: {sidecar}"
        )
    try:
        record = RerankInstallRecord.from_json(sidecar.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RerankInstallError(
            "sidecar_invalid", f"rerank sidecar invalid: {sidecar}: {exc}"
        ) from exc
    if record.repo != spec.repo or record.revision != spec.revision:
        raise RerankInstallError(
            "sidecar_mismatch",
            "rerank sidecar does not match pinned repo/revision",
        )
    if record.files != _expected_files(spec):
        raise RerankInstallError(
            "sidecar_mismatch", "rerank sidecar file digests do not match pins"
        )
    for file_spec in spec.files:
        _verify_file(
            asset_path(file_spec, spec=spec, journal_path=journal_path), file_spec
        )
    return record


def install_rerank_model(
    *,
    force: bool = False,
    spec: RerankModelSpec = RERANK_MODEL_SPEC,
    journal_path: str | Path | None = None,
) -> RerankInstallRecord:
    if not force:
        try:
            return check_rerank_model(spec=spec, journal_path=journal_path)
        except RerankInstallError:
            pass

    try:
        sidecar_path(spec=spec, journal_path=journal_path).unlink(missing_ok=True)
        for file_spec in spec.files:
            _download_file(
                _download_url(spec, file_spec),
                asset_path(file_spec, spec=spec, journal_path=journal_path),
                file_spec,
            )
        record = _record_for_spec(spec)
        _write_sidecar(sidecar_path(spec=spec, journal_path=journal_path), record)
        return record
    except RerankInstallError:
        _cleanup_partial_install(spec=spec, journal_path=journal_path)
        raise
    except Exception as exc:
        _cleanup_partial_install(spec=spec, journal_path=journal_path)
        raise RerankInstallError(
            "install_failed", f"rerank install failed: {exc}"
        ) from exc
