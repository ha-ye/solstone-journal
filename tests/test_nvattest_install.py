# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from solstone.think.journal_io import LockTimeout
from solstone.think.providers import nvattest_install


def test_linux_x86_64_archive_pin_is_exact() -> None:
    spec = nvattest_install.NVATTEST_ARCHIVES["linux-x86_64"]
    expected_url = (
        "https://updates.solstone.app/providers/nvattest/"
        "libnvat-linux-x86_64-1.2.2-sol.1-archive.tar.xz"
    )
    legacy_sha = "3f10da6fca794b7e3025c6645447947ec8bc45bcfde5b5b1d23241c7115630db"

    assert spec.version == "1.2.2-sol.1"
    assert spec.url == expected_url
    assert (
        spec.sha256
        == "60ef75d1873e7129f03ea80d107d92b2ef216d2a8815958617b30d9c721d474a"
    )
    source = Path(nvattest_install.__file__).read_text(encoding="utf-8")
    assert "developer.download.nvidia.com" not in source
    assert legacy_sha not in source


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
    (root / "share" / "ca").mkdir(parents=True)
    (root / "share" / "ca" / "ca-bundle.pem").write_text("ca\n", encoding="utf-8")
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


def test_install_nvattest_upgrades_old_nvidia_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = nvattest_install.cache_root(tmp_path)
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "nvattest").write_text("old\n", encoding="utf-8")
    (root / "lib").mkdir()
    (root / "share" / "ca").mkdir(parents=True)
    (root / "share" / "ca" / "ca-bundle.pem").write_text("old-ca\n", encoding="utf-8")
    (root / nvattest_install.SIDECAR_NAME).write_text(
        json.dumps(
            {
                "archive_sha256": (
                    "3f10da6fca794b7e3025c6645447947ec8bc45bcfde5b5b1d23241c7115630db"
                ),
                "version": "1.2.2",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    spec = _fixture_spec(tmp_path, version="1.2.2-sol.1")
    calls: list[Path] = []

    def fake_download(_url: str, dest: Path, _expected_sha256: str) -> None:
        calls.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tmp_path / spec.archive_name, dest)

    monkeypatch.setattr(nvattest_install, "_download_file", fake_download)

    nvattest_install.install_nvattest(spec=spec, journal_path=tmp_path)

    assert len(calls) == 1
    assert (root / "bin" / "nvattest").read_text(encoding="utf-8") == "new\n"
    sidecar = json.loads(
        (root / nvattest_install.SIDECAR_NAME).read_text(encoding="utf-8")
    )
    assert sidecar["version"] == "1.2.2-sol.1"


def test_install_nvattest_rejects_hash_mismatch_without_partial_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info) -> bool:
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self):
            yield b"not the pinned archive"

    def fake_stream(*_args, **_kwargs):
        return FakeResponse()

    monkeypatch.setattr("httpx.stream", fake_stream)
    spec = nvattest_install.NvattestArchiveSpec(
        version="1.2.2-sol.1",
        url="https://example.invalid/nvattest.tar.xz",
        archive_name="nvattest.tar.xz",
        sha256="0" * 64,
    )

    with pytest.raises(nvattest_install.NvattestInstallError) as exc_info:
        nvattest_install.install_nvattest(spec=spec, journal_path=tmp_path)

    assert exc_info.value.reason_code == "sha256_mismatch"
    root = nvattest_install.cache_root(tmp_path)
    assert not (root / "bin").exists()
    assert not (root / "lib").exists()
    assert not (root / "share").exists()
    assert not (root / ".downloads" / "nvattest.tar.xz").exists()
    assert not (root / ".downloads" / "nvattest.tar.xz.tmp").exists()


def test_ensure_nvattest_unsupported_platform_does_not_touch_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nvattest_install, "nvattest_archive_key", lambda: None)

    result = nvattest_install.ensure_nvattest_installed(journal_path=tmp_path)

    assert result.status == "platform_unsupported"
    assert result.reason_code == "platform_unsupported"
    assert not nvattest_install.cache_root(tmp_path).exists()


def test_ensure_nvattest_lock_timeout_is_in_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def fake_hold_lock(path: Path, *, timeout: float, **_kwargs) -> Iterator[None]:
        raise LockTimeout(path, timeout)
        yield

    monkeypatch.setattr(nvattest_install, "hold_lock", fake_hold_lock)

    result = nvattest_install.ensure_nvattest_installed(journal_path=tmp_path)

    assert result.status == "install_in_flight"
    assert result.reason_code == "install-in-progress"


def test_ensure_nvattest_override_skips_cache_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = tmp_path / "override"
    monkeypatch.setenv(nvattest_install.SPP_NVATTEST_DIR_ENV, str(override))
    monkeypatch.setattr(
        nvattest_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not run"),
    )

    result = nvattest_install.ensure_nvattest_installed(journal_path=tmp_path)

    assert result.status == "already_installed"
    assert result.nvattest_dir == override
    assert not nvattest_install.cache_root(tmp_path).exists()


def test_install_nvattest_accepts_wrapped_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _fixture_spec(tmp_path, wrapped=True)

    def fake_download(_url: str, dest: Path, _expected_sha256: str) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tmp_path / spec.archive_name, dest)

    monkeypatch.setattr(nvattest_install, "_download_file", fake_download)

    installed = nvattest_install.install_nvattest(spec=spec, journal_path=tmp_path)

    assert (installed / "bin" / "nvattest").read_text(encoding="utf-8") == "new\n"


def _fixture_spec(
    tmp_path: Path,
    *,
    version: str = "9.9.9",
    wrapped: bool = False,
) -> nvattest_install.NvattestArchiveSpec:
    archive_name = "nvattest-fixture.tar.gz"
    source = tmp_path / "source" / "nvattest-fixture"
    (source / "bin").mkdir(parents=True)
    (source / "bin" / "nvattest").write_text("new\n", encoding="utf-8")
    (source / "lib").mkdir()
    (source / "lib" / "libnvat.so.1").write_text("library\n", encoding="utf-8")
    (source / "lib" / "libnvat.so").symlink_to("libnvat.so.1")
    (source / "LICENSE").write_text("license\n", encoding="utf-8")
    (source / "share" / "ca").mkdir(parents=True)
    (source / "share" / "ca" / "ca-bundle.pem").write_text("ca\n", encoding="utf-8")
    (source / "share" / "THIRD_PARTY_NOTICES.md").write_text(
        "notices\n",
        encoding="utf-8",
    )

    archive_path = tmp_path / archive_name
    with tarfile.open(archive_path, "w:gz") as archive:
        if wrapped:
            archive.add(source, arcname=source.name)
        else:
            for child in source.iterdir():
                archive.add(child, arcname=child.name)
    return nvattest_install.NvattestArchiveSpec(
        version=version,
        url="https://example.invalid/nvattest-fixture.tar.gz",
        archive_name=archive_name,
        sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    )
