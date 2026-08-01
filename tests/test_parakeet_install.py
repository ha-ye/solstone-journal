# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
import os
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest

from solstone.think import parakeet_readiness
from solstone.think.providers import fit_report, parakeet_install
from solstone.think.providers.artifact_proof import (
    artifact_manifest_path,
    prove_manifest,
)
from solstone.think.providers.install_state import read_install_status
from tests.helpers.journal_config import seed_journal_config


class _SysShim:
    def __init__(self, real_sys: Any, *, platform: str) -> None:
        self._real_sys = real_sys
        self.platform = platform

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real_sys, name)


class _PlatformShim:
    def __init__(self, real_platform: Any, *, machine: Any) -> None:
        self._real_platform = real_platform
        self.machine = machine

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real_platform, name)


def _init_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_journal_config({"providers": {}}, tmp_path)
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None


def _parakeet_status() -> dict:
    return read_install_status(name="parakeet")


def _fit(severity: fit_report.FitSeverity) -> fit_report.FitReport:
    return fit_report.FitReport(
        artifact="test parakeet",
        checks=(fit_report.FitCheck("test", severity, f"{severity} detail"),),
    )


def _server_tarball(tmp_path: Path, backend: str) -> Path:
    inner_name = (
        f"parakeet-{parakeet_readiness.PARAKEET_CPP_RELEASE_TAG}-bin-linux-"
        f"{backend}-x64"
    )
    fixture_root = tmp_path / f"fixture-{backend}" / inner_name
    fixture_root.mkdir(parents=True)
    (fixture_root / parakeet_readiness.PARAKEET_CPP_BINARY_NAME).write_text(
        f"#!/bin/sh\nprintf 'fake {parakeet_readiness.PARAKEET_CPP_BINARY_NAME} {backend}\\n'\n",
        encoding="utf-8",
    )
    (fixture_root / "LICENSE").write_text("license\n", encoding="utf-8")
    (fixture_root / "README.md").write_text("readme\n", encoding="utf-8")
    (fixture_root / "parakeet-cli").write_text("cli\n", encoding="utf-8")
    tarball = tmp_path / f"{backend}.tar.gz"
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(fixture_root, arcname=inner_name)
    return tarball


def _stage_ready_files() -> tuple[Path, Path, Path]:
    cpu = parakeet_install.binary_path("cpu")
    vulkan = parakeet_install.binary_path("vulkan")
    model = parakeet_install.model_path()
    for path in (cpu, vulkan):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_text("model\n", encoding="utf-8")
    fingerprint = parakeet_install.target_fingerprint()
    for backend in parakeet_readiness.PARAKEET_CPP_BINARY_BACKENDS:
        parakeet_install._write_binary_manifest(
            backend=backend,
            attempt_status=None,
            fingerprint=fingerprint,
            journal_path=None,
        )
    parakeet_install._write_model_manifest(
        attempt_status=None,
        fingerprint=fingerprint,
        journal_path=None,
    )
    return cpu, vulkan, model


def _write_ready_binary_manifests() -> None:
    fingerprint = parakeet_install.target_fingerprint()
    for backend in parakeet_readiness.PARAKEET_CPP_BINARY_BACKENDS:
        parakeet_install._write_binary_manifest(
            backend=backend,
            attempt_status=None,
            fingerprint=fingerprint,
            journal_path=None,
        )


def test_install_hint_literal() -> None:
    assert parakeet_install.install_hint() == "journal install-provider parakeet"


def test_parakeet_server_pins_cover_expected_platforms_and_backends() -> None:
    assert parakeet_install.PARAKEET_SERVER_PINS == {
        ("x86_64-unknown-linux-gnu", "vulkan"): {
            "filename": "parakeet-v0.5.0-bin-linux-vulkan-x64.tar.gz",
            "sha256": "36c8d4b93594ec18928c9c76b02e04b2d738e859deda8b5e3944bb34fc0646eb",
        },
        ("x86_64-unknown-linux-gnu", "cpu"): {
            "filename": "parakeet-v0.5.0-bin-linux-cpu-x64.tar.gz",
            "sha256": "636a9fc48ac023096037790f9b77d7e5043b200dd6399ec0438bd648c35d79b9",
        },
        ("aarch64-unknown-linux-gnu", "vulkan"): {
            "filename": "parakeet-v0.5.0-bin-linux-vulkan-arm64.tar.gz",
            "sha256": "b95483070eb87ed144b9f39826a69fb67ea516c68aacc4fcf13a121a746ad7e4",
        },
        ("aarch64-unknown-linux-gnu", "cpu"): {
            "filename": "parakeet-v0.5.0-bin-linux-cpu-arm64.tar.gz",
            "sha256": "a7c9064c64b84f6b041252d5d2334d4a47693636e9c7c6ab2c535fcef11cf88b",
        },
    }


def test_non_linux_artifact_key_raises_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        parakeet_readiness,
        "sys",
        _SysShim(parakeet_readiness.sys, platform="darwin"),
    )
    monkeypatch.setattr(
        parakeet_readiness,
        "platform",
        _PlatformShim(parakeet_readiness.platform, machine=lambda: "arm64"),
    )

    with pytest.raises(parakeet_install.ParakeetProviderError) as exc_info:
        parakeet_install.parakeet_server_artifact_key()

    assert exc_info.value.reason_code == "unsupported_platform"


def test_cpu_and_vulkan_install_dirs_are_distinct(tmp_path, monkeypatch) -> None:
    _init_journal(tmp_path, monkeypatch)

    assert parakeet_install.binary_install_dir(
        "cpu"
    ) != parakeet_install.binary_install_dir("vulkan")


def test_install_parakeet_server_relocates_and_chmods_binary(
    tmp_path, monkeypatch
) -> None:
    _init_journal(tmp_path, monkeypatch)
    fixture_tarball = _server_tarball(tmp_path, "cpu")

    def fake_download(_url, dest, **_kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture_tarball, dest)

    monkeypatch.setattr(parakeet_install, "_download_file", fake_download)
    monkeypatch.setattr(
        parakeet_install, "_verify_sha256", lambda _path, _expected: None
    )

    result = parakeet_install.install_parakeet_server("cpu")

    install_dir = parakeet_install.binary_install_dir("cpu")
    final_path = parakeet_install.binary_path("cpu")
    assert result["install_state"] == "verifying"
    assert final_path.exists()
    assert final_path.read_text(encoding="utf-8") == (
        f"#!/bin/sh\nprintf 'fake {parakeet_readiness.PARAKEET_CPP_BINARY_NAME} cpu\\n'\n"
    )
    assert os.access(final_path, os.X_OK)
    assert (install_dir / "LICENSE").is_file()
    assert (install_dir / "README.md").is_file()
    assert not (
        install_dir
        / f"parakeet-{parakeet_readiness.PARAKEET_CPP_RELEASE_TAG}-bin-linux-cpu-x64"
    ).exists()
    assert prove_manifest(
        artifact_manifest_path(install_dir),
        provider=parakeet_install.PARAKEET_PROVIDER_NAME,
        pin_identity=parakeet_install._binary_pin_identity(
            parakeet_install.parakeet_server_artifact_key(), "cpu"
        ),
    ).ready


def test_install_parakeet_server_extract_failure_preserves_prior_tree(
    tmp_path, monkeypatch
) -> None:
    _init_journal(tmp_path, monkeypatch)
    install_dir = parakeet_install.binary_install_dir("cpu")
    binary = parakeet_install.binary_path("cpu")
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"old server")
    binary.chmod(0o755)
    fingerprint = parakeet_install.target_fingerprint()
    parakeet_install._write_binary_manifest(
        backend="cpu",
        attempt_status=None,
        fingerprint=fingerprint,
        journal_path=None,
    )
    old_manifest = artifact_manifest_path(install_dir).read_text(encoding="utf-8")

    def fake_download(_url, dest, **_kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"archive")

    monkeypatch.setattr(parakeet_install, "_download_file", fake_download)
    monkeypatch.setattr(
        parakeet_install, "_verify_sha256", lambda _path, _expected: None
    )
    monkeypatch.setattr(
        parakeet_install,
        "_safe_extract_tarball",
        lambda _tarball, _dest: (_ for _ in ()).throw(RuntimeError("extract broke")),
    )

    with pytest.raises(RuntimeError, match="extract broke"):
        parakeet_install.install_parakeet_server("cpu")

    assert binary.read_bytes() == b"old server"
    assert (
        artifact_manifest_path(install_dir).read_text(encoding="utf-8") == old_manifest
    )


def test_install_parakeet_server_manifest_failure_preserves_prior_tree(
    tmp_path, monkeypatch
) -> None:
    _init_journal(tmp_path, monkeypatch)
    install_dir = parakeet_install.binary_install_dir("cpu")
    binary = parakeet_install.binary_path("cpu")
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"old server")
    binary.chmod(0o755)
    fingerprint = parakeet_install.target_fingerprint()
    parakeet_install._write_binary_manifest(
        backend="cpu",
        attempt_status=None,
        fingerprint=fingerprint,
        journal_path=None,
    )
    old_manifest = artifact_manifest_path(install_dir).read_text(encoding="utf-8")
    tarball = _server_tarball(tmp_path, "cpu")

    def fake_download(_url, dest, **_kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tarball, dest)

    monkeypatch.setattr(parakeet_install, "_download_file", fake_download)
    monkeypatch.setattr(
        parakeet_install, "_verify_sha256", lambda _path, _expected: None
    )
    monkeypatch.setattr(
        parakeet_install,
        "_write_binary_manifest",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("manifest broke")),
    )

    with pytest.raises(RuntimeError, match="manifest broke"):
        parakeet_install.install_parakeet_server("cpu")

    assert binary.read_bytes() == b"old server"
    assert (
        artifact_manifest_path(install_dir).read_text(encoding="utf-8") == old_manifest
    )


def test_sha256_mismatch_fails_closed_and_records_failed_state(
    tmp_path, monkeypatch
) -> None:
    _init_journal(tmp_path, monkeypatch)

    def fake_download(_url, dest, **_kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not the pinned archive")

    monkeypatch.setattr(parakeet_install, "_download_file", fake_download)

    with pytest.raises(parakeet_install.ParakeetProviderError) as exc_info:
        parakeet_install.install_parakeet_server("cpu")

    assert exc_info.value.reason_code == "sha256_mismatch"
    status = _parakeet_status()
    assert status["install_state"] == "failed"
    assert status["install_error"] is not None
    assert status["error_code"] == "sha256_mismatch"
    assert "sha256 mismatch" in status["install_error"]


def test_install_parakeet_writes_distinct_binary_and_model_manifests(
    tmp_path, monkeypatch
) -> None:
    _init_journal(tmp_path, monkeypatch)
    tarballs = {
        "cpu": _server_tarball(tmp_path, "cpu"),
        "vulkan": _server_tarball(tmp_path, "vulkan"),
    }

    def fake_download(_url, dest, **_kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.name.endswith(".gguf"):
            dest.write_bytes(b"fake gguf")
            return
        backend = "vulkan" if "vulkan" in dest.name else "cpu"
        shutil.copy2(tarballs[backend], dest)

    monkeypatch.setattr(parakeet_install, "_download_file", fake_download)
    monkeypatch.setattr(
        parakeet_install, "_verify_sha256", lambda _path, _expected: None
    )

    result = parakeet_install.install_parakeet()

    assert result["install_state"] == "installed"
    artifact_key = parakeet_install.parakeet_server_artifact_key()
    cpu_manifest = artifact_manifest_path(parakeet_install.binary_install_dir("cpu"))
    vulkan_manifest = artifact_manifest_path(
        parakeet_install.binary_install_dir("vulkan")
    )
    assert parakeet_install.binary_path("cpu") != parakeet_install.binary_path("vulkan")
    assert parakeet_install.binary_path("cpu").is_file()
    assert parakeet_install.binary_path("vulkan").is_file()
    assert parakeet_install.model_path().is_file()
    assert (
        prove_manifest(
            cpu_manifest,
            provider=parakeet_install.PARAKEET_PROVIDER_NAME,
            pin_identity=parakeet_install._binary_pin_identity(artifact_key, "cpu"),
        ).ready
        is True
    )
    assert (
        prove_manifest(
            vulkan_manifest,
            provider=parakeet_install.PARAKEET_PROVIDER_NAME,
            pin_identity=parakeet_install._binary_pin_identity(artifact_key, "vulkan"),
        ).ready
        is True
    )
    assert (
        prove_manifest(
            artifact_manifest_path(parakeet_install.model_dir()),
            provider=parakeet_install.PARAKEET_PROVIDER_NAME,
            pin_identity=parakeet_install._model_pin_identity(),
        ).ready
        is True
    )


def test_install_parakeet_blocks_before_downloads(tmp_path, monkeypatch) -> None:
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fit_report,
        "build_parakeet_fit_report",
        lambda journal_path=None: _fit("blocked"),
    )
    monkeypatch.setattr(
        parakeet_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
    )

    with pytest.raises(parakeet_install.ParakeetProviderError) as exc_info:
        parakeet_install.install_parakeet()

    assert exc_info.value.reason_code == "host_unfit"


def test_install_parakeet_warning_continues_to_download(
    tmp_path,
    monkeypatch,
) -> None:
    _init_journal(tmp_path, monkeypatch)
    tarballs = {
        "cpu": _server_tarball(tmp_path, "cpu"),
        "vulkan": _server_tarball(tmp_path, "vulkan"),
    }
    downloads: list[Path] = []

    monkeypatch.setattr(
        fit_report,
        "build_parakeet_fit_report",
        lambda journal_path=None: _fit("warning"),
    )

    def fake_download(_url, dest, **_kwargs):
        downloads.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.name.endswith(".gguf"):
            dest.write_bytes(b"fake gguf")
            return
        backend = "vulkan" if "vulkan" in dest.name else "cpu"
        shutil.copy2(tarballs[backend], dest)

    monkeypatch.setattr(parakeet_install, "_download_file", fake_download)
    monkeypatch.setattr(
        parakeet_install, "_verify_sha256", lambda _path, _expected: None
    )

    assert parakeet_install.install_parakeet()["install_state"] == "installed"
    assert downloads


def test_install_parakeet_ready_short_circuits_before_component_installs(
    tmp_path, monkeypatch
) -> None:
    _init_journal(tmp_path, monkeypatch)
    _stage_ready_files()
    monkeypatch.setattr(
        parakeet_install,
        "_install_parakeet_server_unlocked",
        lambda *_args, **_kwargs: pytest.fail("ready artifacts should not reinstall"),
    )
    monkeypatch.setattr(
        parakeet_install,
        "_install_model_unlocked",
        lambda *_args, **_kwargs: pytest.fail("ready model should not reinstall"),
    )

    result = parakeet_install.install_parakeet()

    assert result["install_state"] == "installed"


def test_ensure_artifacts_installed_resolves_requested_backend(
    tmp_path, monkeypatch
) -> None:
    _init_journal(tmp_path, monkeypatch)
    cpu, vulkan, model = _stage_ready_files()

    assert parakeet_install.ensure_artifacts_installed("cpu") == (cpu, model)
    assert parakeet_install.ensure_artifacts_installed("vulkan") == (vulkan, model)


def test_inspect_readiness_names_missing_openmp_runtime(tmp_path, monkeypatch) -> None:
    _init_journal(tmp_path, monkeypatch)
    _stage_ready_files()
    monkeypatch.setattr(
        parakeet_readiness,
        "probe_parakeet_cpp_binary",
        lambda _path: parakeet_readiness.ParakeetCppProbe(
            runnable=False,
            reason_code="openmp_runtime_unavailable",
            detail=(
                "error while loading shared libraries: libgomp.so.1: "
                "cannot open shared object file"
            ),
        ),
    )

    readiness = parakeet_install.inspect_readiness()

    assert readiness.status == "host-ineligible"
    assert readiness.reason_code == "openmp_runtime_unavailable"
    assert readiness.host == {
        "binary_runtime": {
            "backend": "cpu",
            "runnable": False,
            "reason_code": "openmp_runtime_unavailable",
            "detail": (
                "error while loading shared libraries: libgomp.so.1: "
                "cannot open shared object file"
            ),
        }
    }

    with pytest.raises(parakeet_install.ParakeetProviderError) as exc:
        parakeet_install.ensure_artifacts_installed("cpu")

    assert exc.value.reason_code == "openmp_runtime_unavailable"


def test_ensure_artifacts_installed_reports_missing_binary_and_model(
    tmp_path, monkeypatch
) -> None:
    _init_journal(tmp_path, monkeypatch)

    with pytest.raises(parakeet_install.ParakeetProviderError) as binary_exc:
        parakeet_install.ensure_artifacts_installed("cpu")

    assert binary_exc.value.reason_code == "binary_missing"

    for backend in parakeet_readiness.PARAKEET_CPP_BINARY_BACKENDS:
        path = parakeet_install.binary_path(backend)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("server\n", encoding="utf-8")
        path.chmod(0o755)
    _write_ready_binary_manifests()

    with pytest.raises(parakeet_install.ParakeetProviderError) as model_exc:
        parakeet_install.ensure_artifacts_installed("cpu")

    assert model_exc.value.reason_code == "model_missing"


def test_safe_extract_tarball_rejects_path_traversal(tmp_path) -> None:
    tarball = tmp_path / "bad.tar.gz"
    data = b"bad"
    with tarfile.open(tarball, "w:gz") as archive:
        member = tarfile.TarInfo("../escape")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))

    with pytest.raises(parakeet_install.ParakeetProviderError) as exc_info:
        parakeet_install._safe_extract_tarball(tarball, tmp_path / "dest")

    assert exc_info.value.reason_code == "archive_path_traversal"
