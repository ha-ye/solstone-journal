# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import bz2
import hashlib
import json
import os
from pathlib import Path

import pytest

from solstone.think.backup import install, readiness


def _make_bz2(payload: bytes) -> bytes:
    return bz2.compress(payload)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _patch_linux_amd64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "_platform_info", lambda: ("linux", "amd64"))
    monkeypatch.setattr(readiness, "_platform_info", lambda: ("linux", "amd64"))


def test_ensure_restic_installs_verified_asset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    payload = b"fake restic binary"
    asset = _make_bz2(payload)
    filename = "restic_0.19.0_linux_amd64.bz2"
    _patch_linux_amd64(monkeypatch)
    monkeypatch.delenv(readiness.RESTIC_BUNDLE_ENV, raising=False)
    monkeypatch.setitem(readiness.RESTIC_BZ2_SHA256, filename, _sha256(asset))
    monkeypatch.setattr(install, "_fetch_url", lambda url, *, timeout: asset)

    binary_path = install.ensure_restic(tool_dir=tmp_path)

    assert binary_path == tmp_path / "restic"
    assert binary_path.read_bytes() == payload
    assert os.access(binary_path, os.X_OK)

    sentinel = json.loads((tmp_path / ".install-complete").read_text())
    assert sentinel["sha256"] == _sha256(payload)
    assert sentinel["platform"] == {"os": "linux", "arch": "amd64"}
    assert sentinel["binary_path"] == str(binary_path)
    assert (tmp_path / "restic.LICENSE").read_text() == install.RESTIC_LICENSE_TEXT


def test_ensure_restic_fails_closed_on_sha_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    original = _make_bz2(b"original")
    tampered = _make_bz2(b"tampered")
    filename = "restic_0.19.0_linux_amd64.bz2"
    calls = 0

    def fake_fetch_url(url: str, *, timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        return tampered

    _patch_linux_amd64(monkeypatch)
    monkeypatch.delenv(readiness.RESTIC_BUNDLE_ENV, raising=False)
    monkeypatch.setitem(readiness.RESTIC_BZ2_SHA256, filename, _sha256(original))
    monkeypatch.setattr(install, "_fetch_url", fake_fetch_url)

    with pytest.raises(RuntimeError) as exc_info:
        install.ensure_restic(tool_dir=tmp_path)

    message = str(exc_info.value)
    assert "restic asset SHA mismatch" in message
    assert f"expected: {_sha256(original)}" in message
    assert f"actual:   {_sha256(tampered)}" in message
    assert calls == 1


def test_ensure_restic_reuses_ready_sentinel_without_downloading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    payload = b"fake restic binary"
    asset = _make_bz2(payload)
    filename = "restic_0.19.0_linux_amd64.bz2"
    _patch_linux_amd64(monkeypatch)
    monkeypatch.delenv(readiness.RESTIC_BUNDLE_ENV, raising=False)
    monkeypatch.setitem(readiness.RESTIC_BZ2_SHA256, filename, _sha256(asset))
    monkeypatch.setattr(install, "_fetch_url", lambda url, *, timeout: asset)

    installed = install.ensure_restic(tool_dir=tmp_path)
    assert installed == tmp_path / "restic"

    calls = 0

    def fail_fetch_url(url: str, *, timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        raise AssertionError("fetch should not be called")

    monkeypatch.setattr(readiness, "_restic_version_ok", lambda path: True)
    monkeypatch.setattr(install, "_fetch_url", fail_fetch_url)

    assert install.ensure_restic(tool_dir=tmp_path) == installed
    assert calls == 0


@pytest.mark.parametrize(
    ("os_name", "arch", "expected_filename"),
    [
        ("darwin", "amd64", "restic_0.19.0_darwin_amd64.bz2"),
        ("darwin", "arm64", "restic_0.19.0_darwin_arm64.bz2"),
        ("linux", "amd64", "restic_0.19.0_linux_amd64.bz2"),
        ("linux", "arm64", "restic_0.19.0_linux_arm64.bz2"),
        ("linux", "x86_64", "restic_0.19.0_linux_amd64.bz2"),
        ("linux", "aarch64", "restic_0.19.0_linux_arm64.bz2"),
    ],
)
def test_select_restic_asset_platforms(
    os_name: str,
    arch: str,
    expected_filename: str,
):
    filename, url, sha256 = readiness.select_restic_asset(os_name, arch)

    assert filename == expected_filename
    assert url == (
        "https://github.com/restic/restic/releases/download/v0.19.0/"
        f"{expected_filename}"
    )
    assert sha256 == readiness.RESTIC_BZ2_SHA256[expected_filename]


def test_select_restic_asset_rejects_unsupported_platform():
    with pytest.raises(RuntimeError, match="unsupported platform"):
        readiness.select_restic_asset("windows", "amd64")
    with pytest.raises(RuntimeError, match="unsupported platform"):
        readiness.select_restic_asset("linux", "riscv64")


def test_ensure_restic_uses_bundled_override_without_downloading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    payload = b"bundled fake restic binary"
    asset = _make_bz2(payload)
    bundle_path = tmp_path / "restic_0.19.0_linux_amd64.bz2"
    bundle_path.write_bytes(asset)
    filename = "restic_0.19.0_linux_amd64.bz2"

    _patch_linux_amd64(monkeypatch)
    monkeypatch.setenv(readiness.RESTIC_BUNDLE_ENV, str(bundle_path))
    monkeypatch.setitem(readiness.RESTIC_BZ2_SHA256, filename, _sha256(asset))

    def fail_fetch_url(url: str, *, timeout: float) -> bytes:
        raise AssertionError("fetch should not be called")

    monkeypatch.setattr(install, "_fetch_url", fail_fetch_url)

    binary_path = install.ensure_restic(tool_dir=tmp_path / "tool")

    assert binary_path.read_bytes() == payload
    assert os.access(binary_path, os.X_OK)
