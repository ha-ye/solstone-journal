# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import gzip
import stat
import struct
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from scripts.release_archive_manifest import (
    ArchiveManifest,
    ArchiveManifestRow,
    archive_manifest,
    assert_archives_semantically_identical,
)
from scripts.release_candidate_driver import DriverError

ZIP_DATE_TIME = (2026, 1, 2, 3, 4, 6)


def _first_error(excinfo: pytest.ExceptionInfo[DriverError]) -> str:
    return excinfo.value.failures[0].error


def _zip_file_info(
    name: str,
    *,
    mode: int = 0o644,
    create_system: int = 3,
    file_type: int = 0,
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, ZIP_DATE_TIME)
    info.create_system = create_system
    info.external_attr = (file_type | mode) << 16
    return info


def _zip_dir_info(name: str, *, mode: int = 0o755) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name if name.endswith("/") else f"{name}/", ZIP_DATE_TIME)
    info.create_system = 3
    info.external_attr = ((stat.S_IFDIR | mode) & 0xFFFF) << 16
    info.external_attr |= 0x10
    return info


def _write_zip(
    path: Path,
    entries: list[tuple[zipfile.ZipInfo, bytes]],
    *,
    reverse: bool = False,
) -> None:
    ordered = list(reversed(entries)) if reverse else entries
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for info, content in ordered:
            archive.writestr(info, content)


def _patch_zip_central_directory(
    path: Path,
    member: str,
    *,
    external_attr: int | None = None,
    create_system: int | None = None,
) -> None:
    data = bytearray(path.read_bytes())
    pos = 0
    while True:
        idx = data.find(b"PK\x01\x02", pos)
        if idx == -1:
            break
        name_len = struct.unpack_from("<H", data, idx + 28)[0]
        extra_len = struct.unpack_from("<H", data, idx + 30)[0]
        comment_len = struct.unpack_from("<H", data, idx + 32)[0]
        name = bytes(data[idx + 46 : idx + 46 + name_len]).decode("utf-8")
        if name == member:
            if create_system is not None:
                data[idx + 5] = create_system
            if external_attr is not None:
                struct.pack_into("<L", data, idx + 38, external_attr)
            path.write_bytes(data)
            return
        pos = idx + 46 + name_len + extra_len + comment_len
    raise AssertionError(f"{member!r} not found in {path}")


def _write_tar(
    path: Path,
    entries: list[tarfile.TarInfo],
    contents: dict[str, bytes] | None = None,
) -> None:
    content_by_name = contents or {}
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gzipped:
            with tarfile.open(fileobj=gzipped, mode="w") as archive:
                for info in entries:
                    info.mtime = 0
                    fileobj = None
                    if info.isreg():
                        content = content_by_name[info.name]
                        info.size = len(content)
                        fileobj = BytesIO(content)
                    archive.addfile(info, fileobj)


def _tar_file_info(name: str, *, mode: int = 0o644) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mode = mode
    return info


def _tar_dir_info(name: str, *, mode: int = 0o755) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = mode
    return info


def test_zip_archives_without_explicit_directory_entries_compare_clean(
    tmp_path: Path,
) -> None:
    retained = tmp_path / "retained.whl"
    candidate = tmp_path / "candidate.whl"
    entries = [
        (_zip_file_info("pkg/module.py", mode=0o664), b"VALUE = 1\n"),
        (_zip_file_info("pkg-1.0.dist-info/METADATA", mode=0o664), b"Name: pkg\n"),
    ]

    _write_zip(retained, entries)
    _write_zip(candidate, entries, reverse=True)

    assert retained.read_bytes() != candidate.read_bytes()
    manifest = assert_archives_semantically_identical(retained, candidate)
    assert manifest.member_count == 2
    assert [row.path for row in manifest.rows] == [
        "pkg-1.0.dist-info/METADATA",
        "pkg/module.py",
    ]


def test_duplicate_path_refuses(tmp_path: Path) -> None:
    archive = tmp_path / "duplicate.whl"
    info = _zip_file_info("pkg/file.txt")
    with pytest.warns(UserWarning, match="Duplicate name"):
        _write_zip(archive, [(info, b"first"), (info, b"second")])

    with pytest.raises(DriverError) as excinfo:
        archive_manifest(archive)

    assert _first_error(excinfo) == "release archive contains duplicate member path"


def test_entry_kind_change_refuses(tmp_path: Path) -> None:
    retained = tmp_path / "retained.tar.gz"
    candidate = tmp_path / "candidate.tar.gz"
    _write_tar(
        retained,
        [_tar_file_info("pkg/item")],
        {"pkg/item": b"same"},
    )
    _write_tar(candidate, [_tar_dir_info("pkg/item")])

    with pytest.raises(DriverError) as excinfo:
        assert_archives_semantically_identical(retained, candidate)

    assert _first_error(excinfo) == (
        "release archive member kind differs from retained manifest"
    )


def test_permission_bit_change_refuses(tmp_path: Path) -> None:
    retained = tmp_path / "retained.whl"
    candidate = tmp_path / "candidate.whl"
    _write_zip(retained, [(_zip_file_info("pkg/file.txt", mode=0o644), b"same")])
    _write_zip(candidate, [(_zip_file_info("pkg/file.txt", mode=0o600), b"same")])

    with pytest.raises(DriverError) as excinfo:
        assert_archives_semantically_identical(retained, candidate)

    assert _first_error(excinfo) == "release archive canonical manifest mismatch"


def test_content_divergence_refuses(tmp_path: Path) -> None:
    retained = tmp_path / "retained.whl"
    candidate = tmp_path / "candidate.whl"
    _write_zip(retained, [(_zip_file_info("pkg/file.txt"), b"abc")])
    _write_zip(candidate, [(_zip_file_info("pkg/file.txt"), b"abd")])

    with pytest.raises(DriverError) as excinfo:
        assert_archives_semantically_identical(retained, candidate)

    assert _first_error(excinfo) == "release archive canonical manifest mismatch"


def test_missing_member_refuses(tmp_path: Path) -> None:
    retained = tmp_path / "retained.whl"
    candidate = tmp_path / "candidate.whl"
    _write_zip(
        retained,
        [
            (_zip_file_info("pkg/a.txt"), b"a"),
            (_zip_file_info("pkg/b.txt"), b"b"),
        ],
    )
    _write_zip(candidate, [(_zip_file_info("pkg/a.txt"), b"a")])

    with pytest.raises(DriverError) as excinfo:
        assert_archives_semantically_identical(retained, candidate)

    assert _first_error(excinfo) == "release archive canonical manifest mismatch"


def test_extra_member_refuses_before_hashing(tmp_path: Path) -> None:
    retained = tmp_path / "retained.whl"
    candidate = tmp_path / "candidate.whl"
    _write_zip(retained, [(_zip_file_info("pkg/a.txt"), b"a")])
    _write_zip(
        candidate,
        [
            (_zip_file_info("pkg/a.txt"), b"a"),
            (_zip_file_info("pkg/b.txt"), b"b"),
        ],
    )

    with pytest.raises(DriverError) as excinfo:
        assert_archives_semantically_identical(retained, candidate)

    assert _first_error(excinfo) == (
        "release archive member count exceeds retained manifest"
    )


@pytest.mark.parametrize(
    ("create_system", "external_attr"),
    (
        (0, 0o644 << 16),
        (3, 0),
    ),
)
def test_zip_undecidable_permission_metadata_refuses(
    tmp_path: Path,
    create_system: int,
    external_attr: int,
) -> None:
    archive = tmp_path / "undecidable.whl"
    _write_zip(archive, [(_zip_file_info("pkg/file.txt"), b"content")])
    _patch_zip_central_directory(
        archive,
        "pkg/file.txt",
        create_system=create_system,
        external_attr=external_attr,
    )

    with pytest.raises(DriverError) as excinfo:
        archive_manifest(archive)

    assert (
        _first_error(excinfo) == "release archive zip permission bits are undecidable"
    )


def test_zip_symlink_refuses(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.whl"
    _write_zip(
        archive,
        [
            (
                _zip_file_info(
                    "pkg/link",
                    mode=0o777,
                    file_type=stat.S_IFLNK,
                ),
                b"target",
            )
        ],
    )

    with pytest.raises(DriverError) as excinfo:
        archive_manifest(archive)

    assert _first_error(excinfo) == "release archive member kind is unsupported"


@pytest.mark.parametrize("member_path", ("/abs.txt", "pkg/../evil.txt"))
def test_unsafe_member_path_refuses(tmp_path: Path, member_path: str) -> None:
    archive = tmp_path / "unsafe.whl"
    _write_zip(archive, [(_zip_file_info(member_path), b"content")])

    with pytest.raises(DriverError) as excinfo:
        archive_manifest(archive)

    assert _first_error(excinfo) == "release archive member path is unsafe"


@pytest.mark.parametrize(
    ("tar_type", "expected_kind"),
    (
        (tarfile.SYMTYPE, "symlink"),
        (tarfile.LNKTYPE, "hardlink"),
        (tarfile.CHRTYPE, "character device"),
        (tarfile.BLKTYPE, "block device"),
        (tarfile.FIFOTYPE, "FIFO"),
    ),
)
def test_tar_special_member_refuses(
    tmp_path: Path,
    tar_type: bytes,
    expected_kind: str,
) -> None:
    archive = tmp_path / "special.tar.gz"
    info = tarfile.TarInfo(f"pkg/{expected_kind}")
    info.type = tar_type
    info.mode = 0o644
    if tar_type in {tarfile.CHRTYPE, tarfile.BLKTYPE}:
        info.devmajor = 1
        info.devminor = 3
    if tar_type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
        info.linkname = "pkg/file.txt"
    _write_tar(archive, [info])

    with pytest.raises(DriverError) as excinfo:
        archive_manifest(archive)

    assert _first_error(excinfo) == "release archive member kind is unsupported"
    assert expected_kind in excinfo.value.failures[0].actual


def test_malformed_archive_refuses(tmp_path: Path) -> None:
    archive = tmp_path / "malformed.whl"
    archive.write_bytes(b"not a zip")

    with pytest.raises(DriverError) as excinfo:
        archive_manifest(archive)

    assert _first_error(excinfo) == "release archive could not be read"


def test_oversize_member_refuses_before_hashing(tmp_path: Path) -> None:
    retained = tmp_path / "retained.whl"
    candidate = tmp_path / "candidate.whl"
    _write_zip(retained, [(_zip_file_info("pkg/file.txt"), b"abc")])
    _write_zip(candidate, [(_zip_file_info("pkg/file.txt"), b"abcd")])

    with pytest.raises(DriverError) as excinfo:
        assert_archives_semantically_identical(retained, candidate)

    assert _first_error(excinfo) == (
        "release archive member size exceeds retained manifest"
    )


def test_cumulative_uncompressed_limit_refuses(tmp_path: Path) -> None:
    archive = tmp_path / "candidate.whl"
    _write_zip(archive, [(_zip_file_info("pkg/file.txt"), b"abc")])
    limits = ArchiveManifest(
        rows=(
            ArchiveManifestRow(
                path="pkg/file.txt",
                kind="file",
                mode=0o644,
                size=3,
                sha256="",
            ),
        ),
        member_count=1,
        total_uncompressed=2,
    )

    with pytest.raises(DriverError) as excinfo:
        archive_manifest(archive, limits=limits)

    assert _first_error(excinfo) == (
        "release archive total uncompressed size exceeds retained manifest"
    )
