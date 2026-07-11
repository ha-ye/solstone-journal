# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from solstone.think.providers import nvattest_install


def test_install_nvattest_reinstalls_partial_cache_without_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = nvattest_install.cache_root(tmp_path)
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "nvattest").write_text("stale\n", encoding="utf-8")
    (root / "lib").mkdir()

    spec = _fixture_spec(tmp_path)
    calls: list[Path] = []

    def fake_download(_url: str, dest: Path, _expected_sha256: str) -> None:
        calls.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tmp_path / spec.archive_name, dest)

    monkeypatch.setattr(nvattest_install, "_download_file", fake_download)

    installed = nvattest_install.install_nvattest(spec=spec, journal_path=tmp_path)

    assert installed == root
    assert len(calls) == 1
    assert (root / "bin" / "nvattest").read_text(encoding="utf-8") == "new\n"
    assert (root / "lib" / "libnvat.so").is_symlink()
    assert (root / "lib" / "libnvat.so").readlink() == Path("libnvat.so.1")
    sidecar = json.loads(
        (root / nvattest_install.SIDECAR_NAME).read_text(encoding="utf-8")
    )
    assert sidecar["archive_sha256"] == spec.sha256
    assert sidecar["version"] == spec.version


def test_install_nvattest_reinstalls_stale_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = nvattest_install.cache_root(tmp_path)
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "nvattest").write_text("old\n", encoding="utf-8")
    (root / "lib").mkdir()
    (root / nvattest_install.SIDECAR_NAME).write_text(
        json.dumps({"archive_sha256": "old", "version": "0.0.0"}) + "\n",
        encoding="utf-8",
    )

    spec = _fixture_spec(tmp_path)
    calls: list[Path] = []

    def fake_download(_url: str, dest: Path, _expected_sha256: str) -> None:
        calls.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tmp_path / spec.archive_name, dest)

    monkeypatch.setattr(nvattest_install, "_download_file", fake_download)

    nvattest_install.install_nvattest(spec=spec, journal_path=tmp_path)

    assert len(calls) == 1
    assert (root / "bin" / "nvattest").read_text(encoding="utf-8") == "new\n"


def test_install_nvattest_valid_sidecar_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = nvattest_install.cache_root(tmp_path)
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "nvattest").write_text("installed\n", encoding="utf-8")
    (root / "lib").mkdir()
    spec = nvattest_install.NvattestArchiveSpec(
        version="1.0.0",
        url="https://example.invalid/nvattest.tar.gz",
        archive_name="nvattest.tar.gz",
        sha256="abc123",
    )
    (root / nvattest_install.SIDECAR_NAME).write_text(
        json.dumps({"archive_sha256": spec.sha256, "version": spec.version}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        nvattest_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not run"),
    )

    assert nvattest_install.install_nvattest(spec=spec, journal_path=tmp_path) == root


def _fixture_spec(tmp_path: Path) -> nvattest_install.NvattestArchiveSpec:
    archive_name = "nvattest-fixture.tar.gz"
    source = tmp_path / "source" / "nvattest-fixture"
    (source / "bin").mkdir(parents=True)
    (source / "bin" / "nvattest").write_text("new\n", encoding="utf-8")
    (source / "lib").mkdir()
    (source / "lib" / "libnvat.so.1").write_text("library\n", encoding="utf-8")
    (source / "lib" / "libnvat.so").symlink_to("libnvat.so.1")
    (source / "LICENSE").write_text("license\n", encoding="utf-8")

    archive_path = tmp_path / archive_name
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source, arcname=source.name)
    return nvattest_install.NvattestArchiveSpec(
        version="9.9.9",
        url="https://example.invalid/nvattest-fixture.tar.gz",
        archive_name=archive_name,
        sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    )
