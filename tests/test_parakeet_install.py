# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
import os
import shutil
import tarfile
from pathlib import Path

import pytest

from solstone.think import parakeet_readiness
from solstone.think.journal_config import read_journal_config
from solstone.think.providers import parakeet_install
from solstone.think.providers.install_state import read_install_status


def _init_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text('{"providers": {}}\n', encoding="utf-8")
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    import solstone.think.utils as think_utils

    think_utils._journal_path_cache = None


def _parakeet_status() -> dict:
    return read_install_status(scope="bundled", name="parakeet")


def _parakeet_slot() -> dict:
    return read_journal_config()["providers"]["bundled"]["parakeet"]


def _server_tarball(tmp_path: Path, backend: str) -> Path:
    inner_name = (
        f"parakeet-{parakeet_readiness.PARAKEET_CPP_RELEASE_TAG}-bin-linux-"
        f"{backend}-x64"
    )
    fixture_root = tmp_path / f"fixture-{backend}" / inner_name
    fixture_root.mkdir(parents=True)
    (fixture_root / parakeet_readiness.PARAKEET_CPP_BINARY_NAME).write_bytes(
        f"fake {parakeet_readiness.PARAKEET_CPP_BINARY_NAME} {backend}".encode()
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
        path.write_text("server\n", encoding="utf-8")
        path.chmod(0o755)
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_text("model\n", encoding="utf-8")
    return cpu, vulkan, model


def test_install_hint_literal() -> None:
    assert parakeet_install.install_hint() == "journal install-provider parakeet"


def test_parakeet_server_pins_cover_expected_platforms_and_backends() -> None:
    assert parakeet_install.PARAKEET_SERVER_PINS == {
        ("x86_64-unknown-linux-gnu", "vulkan"): {
            "filename": "parakeet-v0.4.0-bin-linux-vulkan-x64.tar.gz",
            "sha256": "12ee636ccb4a8b3c8f316f1f40c63f5aa4da178bf11563795b39385480ede87e",
        },
        ("x86_64-unknown-linux-gnu", "cpu"): {
            "filename": "parakeet-v0.4.0-bin-linux-cpu-x64.tar.gz",
            "sha256": "0846509eeb64fcb40e0ad28cd16b5bec5387e4799e08c85fb600b428bb306240",
        },
        ("aarch64-unknown-linux-gnu", "vulkan"): {
            "filename": "parakeet-v0.4.0-bin-linux-vulkan-arm64.tar.gz",
            "sha256": "b1e9251c9d247dffffc5e2db44bb993fb5ec40faab208ec83f7b89b8cc24efd0",
        },
        ("aarch64-unknown-linux-gnu", "cpu"): {
            "filename": "parakeet-v0.4.0-bin-linux-cpu-arm64.tar.gz",
            "sha256": "6634487a4cdbd3185e7a127aa4f22fbc49ec56421f7bfb14f450400260597773",
        },
    }


def test_non_linux_artifact_key_raises_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parakeet_readiness.sys, "platform", "darwin")
    monkeypatch.setattr(parakeet_readiness.platform, "machine", lambda: "arm64")

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
    assert result["install_state"] == "installed"
    assert final_path.exists()
    assert (
        final_path.read_bytes()
        == f"fake {parakeet_readiness.PARAKEET_CPP_BINARY_NAME} cpu".encode()
    )
    assert os.access(final_path, os.X_OK)
    assert (install_dir / "LICENSE").is_file()
    assert (install_dir / "README.md").is_file()
    assert not (
        install_dir
        / f"parakeet-{parakeet_readiness.PARAKEET_CPP_RELEASE_TAG}-bin-linux-cpu-x64"
    ).exists()


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
    assert "sha256 mismatch" in status["install_error"]
    slot = _parakeet_slot()
    assert slot["install_state"] == "failed"
    assert "sha256 mismatch" in slot["install_error"]


def test_install_parakeet_writes_distinct_binary_and_model_metadata(
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
    slot = _parakeet_slot()
    assert (
        slot["binary_artifact_cpu"]
        == parakeet_install.PARAKEET_SERVER_PINS[
            (parakeet_install.parakeet_server_artifact_key(), "cpu")
        ]["filename"]
    )
    assert (
        slot["binary_artifact_vulkan"]
        == parakeet_install.PARAKEET_SERVER_PINS[
            (parakeet_install.parakeet_server_artifact_key(), "vulkan")
        ]["filename"]
    )
    assert slot["binary_path_cpu"] != slot["binary_path_vulkan"]
    assert Path(slot["binary_path_cpu"]).is_file()
    assert Path(slot["binary_path_vulkan"]).is_file()
    assert slot["model_repo"] == parakeet_readiness.PARAKEET_CPP_MODEL_REPO
    assert slot["model_filename"] == parakeet_readiness.PARAKEET_CPP_MODEL_FILENAME
    assert slot["model_revision"] == parakeet_readiness.PARAKEET_CPP_MODEL_REVISION
    assert slot["model_path"] == str(parakeet_install.model_path())
    assert Path(slot["model_path"]).is_file()


def test_ensure_artifacts_installed_resolves_requested_backend(
    tmp_path, monkeypatch
) -> None:
    _init_journal(tmp_path, monkeypatch)
    cpu, vulkan, model = _stage_ready_files()

    assert parakeet_install.ensure_artifacts_installed("cpu") == (cpu, model)
    assert parakeet_install.ensure_artifacts_installed("vulkan") == (vulkan, model)


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
