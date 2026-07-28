# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from solstone.think.providers import nvattest_install
from solstone.think.providers.nvattest_authority import (
    NvattestArtifactIdentity,
    NvattestTargetEntry,
)

REAL_ARCHIVE_USER_AGENT = "solstone-nvattest-install-integration/1.0"


@dataclass(frozen=True, slots=True)
class FixtureArchive:
    archive_path: Path
    entry: NvattestTargetEntry


class NvattestInstallErrorProxy(RuntimeError):
    pass


def _install_download_from_fixture(
    monkeypatch: pytest.MonkeyPatch,
    fixture: FixtureArchive,
) -> list[tuple[str, Path, int, str]]:
    calls: list[tuple[str, Path, int, str]] = []

    def fake_download(
        url: str,
        dest: Path,
        expected_size_bytes: int,
        expected_sha256: str,
    ) -> None:
        calls.append((url, dest, expected_size_bytes, expected_sha256))
        assert (url, expected_size_bytes, expected_sha256) == (
            fixture.entry.artifact.url,
            fixture.entry.artifact.size_bytes,
            fixture.entry.artifact.sha256,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture.archive_path, dest)
        nvattest_install._verify_file(dest, expected_size_bytes, expected_sha256)

    monkeypatch.setattr(nvattest_install, "_download_file", fake_download)
    return calls


def _write_payload_tarball(
    archive_path: Path,
    entry: NvattestTargetEntry,
    *,
    roots: tuple[str, ...],
    label: str,
    omitted: set[str],
    executable_overrides: dict[str, bool],
    extra_files: dict[str, bytes] | None = None,
) -> None:
    with tarfile.open(archive_path, "w:xz") as archive:
        for root in roots:
            prefix = f"{root}/" if root else ""
            if root:
                _add_dir(archive, root)
            dirs = _payload_dirs(entry, omitted)
            for directory in sorted(dirs):
                _add_dir(archive, f"{prefix}{directory}")
            for member in entry.inventory:
                if member.relpath in omitted:
                    continue
                path = f"{prefix}{member.relpath}"
                if member.kind == "symlink":
                    assert member.symlink_target is not None
                    _add_symlink(archive, path, member.symlink_target)
                    continue
                executable = executable_overrides.get(
                    member.relpath,
                    member.executable,
                )
                _add_file(
                    archive,
                    path,
                    _member_content(member.relpath, label),
                    mode=0o755 if executable else 0o644,
                )
        for name, data in sorted((extra_files or {}).items()):
            _add_file(archive, name, data, mode=0o644)


def _payload_dirs(entry: NvattestTargetEntry, omitted: set[str]) -> set[str]:
    dirs: set[str] = set()
    for member in entry.inventory:
        if member.relpath in omitted:
            continue
        parts = Path(member.relpath).parts[:-1]
        for index in range(1, len(parts) + 1):
            dirs.add(Path(*parts[:index]).as_posix())
    return dirs


def _raw_download_fixture(
    tmp_path: Path,
    entry: NvattestTargetEntry,
    *,
    data: bytes,
    expected_size_delta: int = 0,
    expected_sha256: str | None = None,
) -> FixtureArchive:
    archive_path = tmp_path / f"raw-{entry.key}.tar.xz"
    archive_path.write_bytes(data)
    artifact = NvattestArtifactIdentity(
        name=archive_path.name,
        url=f"https://example.invalid/{archive_path.name}",
        size_bytes=len(data) + expected_size_delta,
        sha256=expected_sha256 or hashlib.sha256(data).hexdigest(),
    )
    return FixtureArchive(
        archive_path=archive_path,
        entry=replace(entry, artifact=artifact),
    )


def download_real_archive(root: Path, entry: NvattestTargetEntry) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    archive = root / entry.artifact.name
    tmp = archive.with_name(f".{archive.name}.tmp")
    digest = hashlib.sha256()
    try:
        request = urllib.request.Request(
            entry.artifact.url,
            headers={"User-Agent": REAL_ARCHIVE_USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            with tmp.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    handle.write(chunk)
        actual = digest.hexdigest()
        assert actual == entry.artifact.sha256
        tmp.replace(archive)
        return archive
    finally:
        tmp.unlink(missing_ok=True)


def _inject_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    fixture: FixtureArchive,
) -> None:
    if failure_point == "before_download":

        def fail_before_download(*_args: object, **_kwargs: object) -> None:
            raise nvattest_install.NvattestInstallError("download_failed", "before")

        monkeypatch.setattr(nvattest_install, "_download_file", fail_before_download)
        return

    if failure_point == "after_download":

        def fail_after_download(
            _url: str,
            dest: Path,
            _expected_size_bytes: int,
            _expected_sha256: str,
        ) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fixture.archive_path, dest)
            raise nvattest_install.NvattestInstallError("download_failed", "after")

        monkeypatch.setattr(nvattest_install, "_download_file", fail_after_download)
        return

    _install_download_from_fixture(monkeypatch, fixture)

    if failure_point == "after_verification":
        monkeypatch.setattr(
            nvattest_install,
            "_safe_extract_nvattest_tarball",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                nvattest_install.NvattestInstallError("archive_extract_failed", "after")
            ),
        )
        return

    if failure_point == "after_extraction":
        real_find = nvattest_install._find_extracted_root

        def fail_after_extraction(raw_dir: Path, entry: NvattestTargetEntry) -> Path:
            real_find(raw_dir, entry)
            raise nvattest_install.NvattestInstallError(
                "archive_layout_invalid",
                "after extraction",
            )

        monkeypatch.setattr(
            nvattest_install, "_find_extracted_root", fail_after_extraction
        )
        return

    if failure_point == "after_layout_validation":
        monkeypatch.setattr(
            nvattest_install,
            "_materialize_payload_tree",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                nvattest_install.NvattestInstallError(
                    "archive_layout_invalid",
                    "after layout",
                )
            ),
        )
        return

    if failure_point == "after_fingerprinting":
        real_promote = nvattest_install._promote_payload_tree

        def fail_after_fingerprinting(
            payload_dir: Path,
            root: Path,
            sidecar_json: str,
        ) -> None:
            assert payload_dir.exists()
            assert root.exists()
            assert sidecar_json
            raise nvattest_install.NvattestInstallError(
                "archive_layout_invalid",
                "after fingerprint",
            )

        monkeypatch.setattr(
            nvattest_install, "_promote_payload_tree", fail_after_fingerprinting
        )
        assert real_promote is not None
        return

    if failure_point == "during_final_promotion":
        original_replace = Path.replace

        def fail_sidecar_commit(path: Path, target: Path) -> Path:
            if path.name == "sidecar.tmp":
                raise NvattestInstallErrorProxy("sidecar commit failed")
            return original_replace(path, target)

        monkeypatch.setattr(Path, "replace", fail_sidecar_commit)
        return

    raise AssertionError(f"unknown failure point: {failure_point}")


def _add_dir(archive: tarfile.TarFile, name: str) -> None:
    info = _tarinfo(name, tarfile.DIRTYPE, 0o755)
    archive.addfile(info)


def _add_file(
    archive: tarfile.TarFile,
    name: str,
    data: bytes,
    *,
    mode: int,
) -> None:
    info = _tarinfo(name, tarfile.REGTYPE, mode)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def _add_symlink(archive: tarfile.TarFile, name: str, linkname: str) -> None:
    info = _tarinfo(name, tarfile.SYMTYPE, 0o777)
    info.linkname = linkname
    archive.addfile(info)


def _member_content(relpath: str, label: str) -> bytes:
    return f"{label}:{relpath}\n".encode("utf-8")


def _tarinfo(name: str, member_type: bytes, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = member_type
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info
