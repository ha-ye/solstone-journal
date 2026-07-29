#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Canonical archive manifests for release publication checks."""

from __future__ import annotations

import hashlib
import stat
import tarfile
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from scripts.release_candidate_driver import DriverError
from scripts.transparency_core import failure

ArchiveMemberKind = Literal["file", "dir"]
READ_CHUNK_BYTES = 256 * 1024


@dataclass(frozen=True, order=True)
class ArchiveManifestRow:
    path: str
    kind: ArchiveMemberKind
    mode: int
    size: int
    sha256: str


@dataclass(frozen=True)
class ArchiveManifest:
    rows: tuple[ArchiveManifestRow, ...]
    member_count: int
    total_uncompressed: int

    @property
    def by_path(self) -> Mapping[str, ArchiveManifestRow]:
        return {row.path: row for row in self.rows}


def archive_manifest(
    path: Path,
    *,
    limits: ArchiveManifest | None = None,
    label: str = "archive",
) -> ArchiveManifest:
    """Return a canonical manifest for a ZIP/wheel or tar.gz archive."""
    if path.name.endswith((".whl", ".zip")):
        return _zip_manifest(path, limits=limits, label=label)
    if path.name.endswith(".tar.gz"):
        return _tar_manifest(path, limits=limits, label=label)
    raise DriverError(
        [
            failure(
                "release archive type is unsupported",
                expected=".whl, .zip, or .tar.gz archive",
                actual=path.name,
                repair="restore the retained release candidate or fetch the expected archive",
            )
        ]
    )


def assert_archives_semantically_identical(
    retained: Path,
    candidate: Path,
    *,
    retained_label: str = "retained archive",
    candidate_label: str = "candidate archive",
) -> ArchiveManifest:
    """Raise unless two on-disk archives have identical canonical manifests."""
    retained_manifest = archive_manifest(retained, label=retained_label)
    candidate_manifest = archive_manifest(
        candidate,
        limits=retained_manifest,
        label=candidate_label,
    )
    if candidate_manifest.rows != retained_manifest.rows:
        raise DriverError(
            [
                failure(
                    "release archive canonical manifest mismatch",
                    expected=_manifest_summary(retained_manifest, retained_label),
                    actual=_manifest_diff(retained_manifest, candidate_manifest),
                    repair="restore matching model archives or cut the next version",
                )
            ]
        )
    return retained_manifest


def _zip_manifest(
    path: Path,
    *,
    limits: ArchiveManifest | None,
    label: str,
) -> ArchiveManifest:
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            _check_member_count(len(infos), limits=limits, label=label)
            seen: set[str] = set()
            for info in infos:
                _validate_member_path(info.filename, seen=seen, label=label)
            rows: list[ArchiveManifestRow] = []
            total_uncompressed = 0
            for info in infos:
                mode = _zip_mode(info, label=label)
                kind = _zip_kind(info, mode=mode, label=label)
                row_size = int(info.file_size)
                total_uncompressed = _check_member_limits(
                    member_path=info.filename,
                    kind=kind,
                    size=row_size,
                    total_before=total_uncompressed,
                    limits=limits,
                    label=label,
                )
                sha256 = ""
                if kind == "file":
                    with archive.open(info) as member:
                        sha256 = _sha256_stream(member)
                rows.append(
                    ArchiveManifestRow(
                        path=info.filename,
                        kind=kind,
                        mode=mode & 0o7777,
                        size=row_size,
                        sha256=sha256,
                    )
                )
    except zipfile.BadZipFile as exc:
        _raise_malformed(path, label=label, exc=exc)
    except OSError as exc:
        _raise_malformed(path, label=label, exc=exc)
    return _build_manifest(rows, total_uncompressed=total_uncompressed)


def _tar_manifest(
    path: Path,
    *,
    limits: ArchiveManifest | None,
    label: str,
) -> ArchiveManifest:
    rows: list[ArchiveManifestRow] = []
    seen: set[str] = set()
    total_uncompressed = 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            while True:
                info = archive.next()
                if info is None:
                    break
                _check_member_count(len(rows) + 1, limits=limits, label=label)
                kind = _tar_kind(info, label=label)
                row_size = int(info.size)
                total_uncompressed = _check_member_limits(
                    member_path=info.name,
                    kind=kind,
                    size=row_size,
                    total_before=total_uncompressed,
                    limits=limits,
                    label=label,
                )
                _validate_member_path(info.name, seen=seen, label=label)
                sha256 = ""
                if kind == "file":
                    member = archive.extractfile(info)
                    if member is None:
                        _raise_unsupported_kind(
                            info.name, "missing regular-file data", label
                        )
                    with member:
                        sha256 = _sha256_stream(member)
                rows.append(
                    ArchiveManifestRow(
                        path=info.name,
                        kind=kind,
                        mode=int(info.mode) & 0o7777,
                        size=row_size,
                        sha256=sha256,
                    )
                )
    except tarfile.TarError as exc:
        _raise_malformed(path, label=label, exc=exc)
    except (EOFError, OSError, ValueError) as exc:
        _raise_malformed(path, label=label, exc=exc)
    return _build_manifest(rows, total_uncompressed=total_uncompressed)


def _build_manifest(
    rows: list[ArchiveManifestRow],
    *,
    total_uncompressed: int,
) -> ArchiveManifest:
    sorted_rows = tuple(sorted(rows))
    return ArchiveManifest(
        rows=sorted_rows,
        member_count=len(sorted_rows),
        total_uncompressed=total_uncompressed,
    )


def _zip_mode(info: zipfile.ZipInfo, *, label: str) -> int:
    mode = info.external_attr >> 16
    if info.create_system != 3 or mode == 0:
        raise DriverError(
            [
                failure(
                    "release archive zip permission bits are undecidable",
                    expected="ZIP entries created by Unix with nonzero external_attr mode bits",
                    actual=(
                        f"{label} {info.filename!r} create_system={info.create_system} "
                        f"external_attr=0x{info.external_attr:08x}"
                    ),
                    repair="rebuild the archive with Unix permission metadata",
                )
            ]
        )
    return mode


def _zip_kind(
    info: zipfile.ZipInfo,
    *,
    mode: int,
    label: str,
) -> ArchiveMemberKind:
    mode_type = stat.S_IFMT(mode)
    if info.is_dir():
        if mode_type in {0, stat.S_IFDIR}:
            return "dir"
        _raise_unsupported_kind(info.filename, stat.filemode(mode), label)
    if mode_type in {0, stat.S_IFREG}:
        return "file"
    _raise_unsupported_kind(info.filename, stat.filemode(mode), label)


def _tar_kind(info: tarfile.TarInfo, *, label: str) -> ArchiveMemberKind:
    if info.isreg():
        return "file"
    if info.isdir():
        return "dir"
    if info.issym():
        _raise_unsupported_kind(info.name, "symlink", label)
    if info.islnk():
        _raise_unsupported_kind(info.name, "hardlink", label)
    if info.ischr():
        _raise_unsupported_kind(info.name, "character device", label)
    if info.isblk():
        _raise_unsupported_kind(info.name, "block device", label)
    if info.isfifo():
        _raise_unsupported_kind(info.name, "FIFO", label)
    _raise_unsupported_kind(info.name, repr(info.type), label)


def _validate_member_path(path: str, *, seen: set[str], label: str) -> None:
    parts = [part for part in path.split("/") if part]
    if not path or path.startswith("/") or ".." in parts:
        raise DriverError(
            [
                failure(
                    "release archive member path is unsafe",
                    expected="relative member path without '..' components",
                    actual=f"{label} {path!r}",
                    repair="rebuild the archive with safe relative paths",
                )
            ]
        )
    if path in seen:
        raise DriverError(
            [
                failure(
                    "release archive contains duplicate member path",
                    expected="unique member paths",
                    actual=f"{label} {path!r}",
                    repair="rebuild the archive without duplicate entries",
                )
            ]
        )
    seen.add(path)


def _check_member_count(
    observed_count: int,
    *,
    limits: ArchiveManifest | None,
    label: str,
) -> None:
    if limits is not None and observed_count > limits.member_count:
        raise DriverError(
            [
                failure(
                    "release archive member count exceeds retained manifest",
                    expected=f"<= {limits.member_count} members",
                    actual=f"{label} members={observed_count}",
                    repair="audit the package index; cut the next version if bytes differ",
                )
            ]
        )


def _check_member_limits(
    *,
    member_path: str,
    kind: ArchiveMemberKind,
    size: int,
    total_before: int,
    limits: ArchiveManifest | None,
    label: str,
) -> int:
    total_after = total_before + size
    if limits is None:
        return total_after
    limit_row = limits.by_path.get(member_path)
    if limit_row is None:
        raise DriverError(
            [
                failure(
                    "release archive member is not in retained manifest",
                    expected="candidate archive members from the retained manifest",
                    actual=f"{label} unexpected member {member_path!r}",
                    repair="restore matching model archives or cut the next version",
                )
            ]
        )
    if kind != limit_row.kind:
        raise DriverError(
            [
                failure(
                    "release archive member kind differs from retained manifest",
                    expected=f"{member_path!r} kind={limit_row.kind}",
                    actual=f"{label} kind={kind}",
                    repair="restore matching model archives or cut the next version",
                )
            ]
        )
    if size > limit_row.size:
        raise DriverError(
            [
                failure(
                    "release archive member size exceeds retained manifest",
                    expected=f"{member_path!r} size <= {limit_row.size}",
                    actual=f"{label} size={size}",
                    repair="audit the package index; cut the next version if bytes differ",
                )
            ]
        )
    if total_after > limits.total_uncompressed:
        raise DriverError(
            [
                failure(
                    "release archive total uncompressed size exceeds retained manifest",
                    expected=f"<= {limits.total_uncompressed} bytes",
                    actual=f"{label} total_uncompressed={total_after}",
                    repair="audit the package index; cut the next version if bytes differ",
                )
            ]
        )
    return total_after


def _sha256_stream(member: BinaryIO) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = member.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _raise_unsupported_kind(path: str, kind: str, label: str) -> None:
    raise DriverError(
        [
            failure(
                "release archive member kind is unsupported",
                expected="regular file or directory",
                actual=f"{label} {path!r} kind={kind}",
                repair="rebuild the archive without links, devices, or special files",
            )
        ]
    )


def _raise_malformed(path: Path, *, label: str, exc: Exception) -> None:
    raise DriverError(
        [
            failure(
                "release archive could not be read",
                expected=f"well-formed {label}",
                actual=f"{path.name}: {type(exc).__name__}",
                repair="restore the retained archive or retry the download",
            )
        ]
    ) from None


def _manifest_summary(manifest: ArchiveManifest, label: str) -> str:
    return (
        f"{label} members={manifest.member_count} "
        f"total_uncompressed={manifest.total_uncompressed} "
        f"rows={_row_keys(manifest)}"
    )


def _manifest_diff(expected: ArchiveManifest, actual: ArchiveManifest) -> str:
    expected_by_path = expected.by_path
    actual_by_path = actual.by_path
    missing = sorted(set(expected_by_path) - set(actual_by_path))
    extra = sorted(set(actual_by_path) - set(expected_by_path))
    changed = sorted(
        path
        for path in set(expected_by_path) & set(actual_by_path)
        if expected_by_path[path] != actual_by_path[path]
    )
    return (
        f"missing={missing} extra={extra} changed={changed} "
        f"actual={_manifest_summary(actual, 'candidate archive')}"
    )


def _row_keys(manifest: ArchiveManifest) -> list[str]:
    return [f"{row.path}:{row.kind}:0o{row.mode:o}:{row.size}" for row in manifest.rows]
