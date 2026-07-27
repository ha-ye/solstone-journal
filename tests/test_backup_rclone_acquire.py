# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from solstone.think.backup import rclone_install


def _asset(payload: bytes, filename: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{filename.removesuffix('.zip')}/rclone", payload)
    return buffer.getvalue()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fake_binary() -> bytes:
    return b"#!/bin/sh\necho 'rclone v1.74.4'\n"


def test_ensure_rclone_installs_verified_asset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    filename = "rclone-v1.74.4-linux-amd64.zip"
    archive = _asset(_fake_binary(), filename)
    monkeypatch.setattr(rclone_install, "_platform_info", lambda: ("linux", "amd64"))
    monkeypatch.setitem(rclone_install.RCLONE_ZIP_SHA256, filename, _sha256(archive))
    monkeypatch.setattr(
        rclone_install,
        "_download_with_retries",
        lambda url, *, attempts, timeout: archive,
    )

    binary_path = rclone_install.ensure_rclone(tool_dir=tmp_path)

    assert binary_path == tmp_path / "rclone"
    assert binary_path.read_bytes() == _fake_binary()
    assert os.access(binary_path, os.X_OK)
    sentinel = json.loads((tmp_path / ".install-complete").read_text())
    assert sentinel["tool"] == "rclone"
    assert sentinel["version"] == "1.74.4"
    assert sentinel["sha256"] == _sha256(_fake_binary())
    assert (tmp_path / "rclone.LICENSE").read_text() == (
        rclone_install.RCLONE_LICENSE_TEXT
    )


def test_ensure_rclone_fails_closed_on_sha_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    filename = "rclone-v1.74.4-linux-amd64.zip"
    archive = _asset(_fake_binary(), filename)
    monkeypatch.setattr(rclone_install, "_platform_info", lambda: ("linux", "amd64"))
    monkeypatch.setitem(rclone_install.RCLONE_ZIP_SHA256, filename, "0" * 64)
    monkeypatch.setattr(
        rclone_install,
        "_download_with_retries",
        lambda url, *, attempts, timeout: archive,
    )

    with pytest.raises(RuntimeError, match="rclone asset SHA mismatch"):
        rclone_install.ensure_rclone(tool_dir=tmp_path)


def test_ensure_rclone_reuses_verified_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    filename = "rclone-v1.74.4-linux-amd64.zip"
    archive = _asset(_fake_binary(), filename)
    monkeypatch.setattr(rclone_install, "_platform_info", lambda: ("linux", "amd64"))
    monkeypatch.setitem(rclone_install.RCLONE_ZIP_SHA256, filename, _sha256(archive))
    calls = 0

    def download(url: str, *, attempts: int, timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        return archive

    monkeypatch.setattr(rclone_install, "_download_with_retries", download)

    installed = rclone_install.ensure_rclone(tool_dir=tmp_path)
    reused = rclone_install.ensure_rclone(tool_dir=tmp_path)

    assert reused == installed
    assert calls == 1


def test_check_rclone_ready_is_read_only_and_honors_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = tmp_path / "rclone"
    payload = _fake_binary()
    binary.write_bytes(payload)
    binary.chmod(0o755)
    (tmp_path / ".install-complete").write_text(
        json.dumps(
            {
                "schema_version": rclone_install.RCLONE_SCHEMA_VERSION,
                "tool": "rclone",
                "version": rclone_install.RCLONE_VERSION,
                "sha256": _sha256(payload),
                "platform": {"os": "linux", "arch": "amd64"},
                "binary_path": str(binary),
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs):
        captured["argv"] = argv
        captured["timeout"] = kwargs["timeout"]
        return rclone_install.subprocess.CompletedProcess(
            argv,
            0,
            stdout="rclone v1.74.4\n",
            stderr="",
        )

    monkeypatch.setattr(rclone_install, "_platform_info", lambda: ("linux", "amd64"))
    monkeypatch.setattr(rclone_install.subprocess, "run", fake_run)

    assert (
        rclone_install.check_rclone_ready(tmp_path, version_timeout=5.0)
        == tmp_path / "rclone"
    )
    assert captured["argv"] == [str(binary), "version"]
    assert captured["timeout"] == 5.0


def test_ensure_rclone_reinstalls_when_ready_binary_cannot_be_hashed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    filename = "rclone-v1.74.4-linux-amd64.zip"
    archive = _asset(_fake_binary(), filename)
    monkeypatch.setattr(rclone_install, "_platform_info", lambda: ("linux", "amd64"))
    monkeypatch.setitem(rclone_install.RCLONE_ZIP_SHA256, filename, _sha256(archive))
    monkeypatch.setattr(
        rclone_install,
        "_download_with_retries",
        lambda url, *, attempts, timeout: archive,
    )
    installed = rclone_install.ensure_rclone(tool_dir=tmp_path)
    real_file_sha256 = rclone_install._file_sha256
    calls = 0

    def flaky_file_sha256(path: Path) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("transient read failure")
        return real_file_sha256(path)

    monkeypatch.setattr(rclone_install, "_file_sha256", flaky_file_sha256)

    reinstalled = rclone_install.ensure_rclone(tool_dir=tmp_path)

    assert reinstalled == installed
    assert calls == 1


@pytest.mark.parametrize(
    ("os_name", "arch", "asset_os"),
    [
        ("darwin", "amd64", "osx"),
        ("darwin", "arm64", "osx"),
        ("linux", "amd64", "linux"),
        ("linux", "arm64", "linux"),
    ],
)
def test_select_rclone_asset_platforms(
    os_name: str,
    arch: str,
    asset_os: str,
):
    filename, url, sha256 = rclone_install.select_rclone_asset(os_name, arch)

    assert filename == f"rclone-v1.74.4-{asset_os}-{arch}.zip"
    assert url == (
        f"https://downloads.rclone.org/v1.74.4/rclone-v1.74.4-{asset_os}-{arch}.zip"
    )
    assert sha256 == rclone_install.RCLONE_ZIP_SHA256[filename]


def test_select_rclone_asset_rejects_unsupported_platform():
    with pytest.raises(RuntimeError, match="unsupported platform"):
        rclone_install.select_rclone_asset("windows", "amd64")
    with pytest.raises(RuntimeError, match="unsupported platform"):
        rclone_install.select_rclone_asset("linux", "riscv64")
