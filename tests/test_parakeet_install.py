# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
import os
import shutil
import tarfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from solstone.think import parakeet_readiness
from solstone.think.journal_config import (
    JournalConfigTransaction,
    read_journal_config,
)
from solstone.think.journal_io.errors import LockTimeout
from solstone.think.providers import fit_report, parakeet_install
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
    return read_install_status(scope="bundled", name="parakeet")


def _parakeet_slot() -> dict:
    return read_journal_config()["providers"]["bundled"]["parakeet"]


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


def test_write_parakeet_metadata_waits_for_config_lock_and_preserves_commits(
    tmp_path,
    monkeypatch,
) -> None:
    _init_journal(tmp_path, monkeypatch)
    journal_path = tmp_path
    config = {
        "setup": {
            "completed_at": "2026-07-01T00:00:00+00:00",
            "completed_by": "setup-writer",
        },
        "service": {"port": 5015, "host": "127.0.0.1"},
        "providers": {"bundled": {}},
    }
    calls: list[tuple[str, object]] = []

    def recording_mutate(mutator, *, journal_path=None):
        calls.append(("mutate", journal_path))
        mutation = mutator(config)
        return JournalConfigTransaction(
            value=mutation.value,
            changed=mutation.changed,
            written=mutation.changed,
        )

    monkeypatch.setattr(parakeet_install, "mutate_journal_config", recording_mutate)

    parakeet_install._write_parakeet_metadata(
        {"model_repo": "openai/parakeet-test"},
        journal_path=journal_path,
    )

    assert calls == [("mutate", journal_path)]
    persisted = config
    assert persisted["setup"]["completed_at"] == "2026-07-01T00:00:00+00:00"
    assert persisted["setup"]["completed_by"] == "setup-writer"
    assert persisted["service"]["port"] == 5015
    assert persisted["service"]["host"] == "127.0.0.1"
    assert (
        persisted["providers"]["bundled"]["parakeet"]["model_repo"]
        == "openai/parakeet-test"
    )


def test_write_parakeet_metadata_rejects_unknown_key_without_lock(
    monkeypatch,
) -> None:
    mutate_calls: list[object] = []

    def recording_mutate(mutator, *, journal_path=None):
        mutate_calls.append(journal_path)
        pytest.fail("metadata validation must happen before transaction entry")

    monkeypatch.setattr(parakeet_install, "mutate_journal_config", recording_mutate)

    with pytest.raises(ValueError) as exc_info:
        parakeet_install._write_parakeet_metadata({"unexpected": "value"})

    assert str(exc_info.value) == "unknown parakeet install metadata key: unexpected"
    assert mutate_calls == []


def test_write_parakeet_metadata_propagates_config_lock_timeout_without_config_io(
    monkeypatch,
) -> None:
    timeout = LockTimeout(path=Path("busy.lock"), timeout=0.01)

    def busy_mutate(mutator, *, journal_path=None):
        raise timeout

    monkeypatch.setattr(parakeet_install, "mutate_journal_config", busy_mutate)

    with pytest.raises(LockTimeout) as exc_info:
        parakeet_install._write_parakeet_metadata({"model_repo": "openai/test"})

    assert exc_info.value is timeout


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


def test_install_parakeet_rechecks_readiness_under_provider_lock(
    tmp_path, monkeypatch
) -> None:
    _init_journal(tmp_path, monkeypatch)
    _stage_ready_files()
    lock_calls = []

    @contextmanager
    def fake_hold_install_lock(journal_path=None):
        lock_calls.append(parakeet_install._install_lock_path(journal_path))
        yield

    monkeypatch.setattr(parakeet_install, "_hold_install_lock", fake_hold_install_lock)
    monkeypatch.setattr(
        parakeet_install,
        "_install_parakeet_server_unlocked",
        lambda *_args: pytest.fail("ready artifacts should not reinstall"),
    )
    monkeypatch.setattr(
        parakeet_install,
        "_install_model_unlocked",
        lambda: pytest.fail("ready model should not reinstall"),
    )

    result = parakeet_install.install_parakeet()

    assert result["install_state"] == "installed"
    assert lock_calls == [parakeet_install.cache_root() / "install"]


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
