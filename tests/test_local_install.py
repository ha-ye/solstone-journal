# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import shutil
import tarfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from solstone.think.journal_config import read_journal_config
from solstone.think.models import LOCAL_MODEL
from solstone.think.providers import local_install, local_vulkan, memory
from solstone.think.providers.install_state import read_install_status
from solstone.think.providers.local import LOCAL_MODEL_SPECS


def _init_journal(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text(
        json.dumps({"providers": {}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def _local_status() -> dict:
    return read_install_status(scope="bundled", name="local")


def _local_slot() -> dict:
    return read_journal_config()["providers"]["bundled"]["local"]


def _write_probe_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "probe.sh"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def test_install_hint_literal() -> None:
    assert local_install.install_hint() == "journal install-provider local"


def test_llama_server_pins_cover_expected_platforms() -> None:
    pins = local_install.LLAMA_SERVER_PINS
    # macOS arm64 (Metal) + both Linux arches on the cross-vendor Vulkan build.
    assert {
        "aarch64-apple-darwin",
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
    } <= set(pins)
    for key, pin in pins.items():
        assert pin["release_tag"] == "b9291"
        assert pin["binary_name"] == "llama-server"
        # sha256 is a 64-char hex digest.
        assert len(pin["sha256"]) == 64
        int(pin["sha256"], 16)
        # Linux GPU acceleration rides the cross-vendor Vulkan prebuilt on both
        # arches (NVIDIA + AMD + Intel from one binary).
        if key.endswith("-unknown-linux-gnu"):
            assert "vulkan" in pin["filename"]
    assert (
        pins["aarch64-unknown-linux-gnu"]["filename"]
        == "llama-b9291-bin-ubuntu-vulkan-arm64.tar.gz"
    )


def test_install_llama_server_relocates_binary_and_libraries(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    pin = local_install.pin_for_current_platform()
    if local_install.llama_server_artifact_key() == "x86_64-unknown-linux-gnu":
        assert pin["filename"] == "llama-b9291-bin-ubuntu-vulkan-x64.tar.gz"
        assert (
            pin["sha256"]
            == "7e3bf4202bedc71c2c9fbfbe02d10075b8d596bb963e7ab006663582dc2e92c2"
        )
    artifact_key = local_install.llama_server_artifact_key()
    install_dir = local_install.binary_install_dir(artifact_key, pin)
    binary_path = local_install.binary_path_for_pin(artifact_key, pin)
    inner_name = "llama-b9291"
    lib_names = ["libllama.so", "libggml.so", "libfoo.dylib"]
    fixture_root = tmp_path / "fixture" / inner_name
    fixture_root.mkdir(parents=True)
    (fixture_root / pin["binary_name"]).write_bytes(b"fake llama-server")
    for lib_name in lib_names:
        (fixture_root / lib_name).write_bytes(f"fake {lib_name}".encode())
    fixture_tarball = tmp_path / pin["filename"]
    with tarfile.open(fixture_tarball, "w:gz") as archive:
        archive.add(fixture_root, arcname=inner_name)
    quarantine_calls: list[Path] = []

    def fake_download(_url, dest, **_kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture_tarball, dest)

    def record_quarantine(path):
        quarantine_calls.append(Path(path))

    monkeypatch.setattr(local_install, "_download_file", fake_download)
    monkeypatch.setattr(local_install, "_verify_sha256", lambda _path, _expected: None)
    monkeypatch.setattr(local_install, "_clear_macos_quarantine", record_quarantine)

    def assert_flat_layout() -> None:
        assert binary_path.exists()
        assert binary_path.read_bytes() == b"fake llama-server"
        for lib_name in lib_names:
            lib_path = install_dir / lib_name
            assert lib_path.exists()
            assert lib_path.read_bytes() == f"fake {lib_name}".encode()
        assert not (install_dir / inner_name).exists()
        assert (install_dir / pin["filename"]).exists()

    result = local_install.install_llama_server()

    assert result["install_state"] == "installed"
    assert_flat_layout()
    assert quarantine_calls == [install_dir]

    result = local_install.install_llama_server()

    assert result["install_state"] == "installed"
    assert_flat_layout()
    assert quarantine_calls == [install_dir, install_dir]


def test_install_llama_server_writes_canonical_sequence(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    pin = {
        "release_tag": "v1",
        "filename": "llama.tar.gz",
        "sha256": "abc123",
        "binary_name": "llama-server",
    }
    final_path = local_install.binary_path_for_pin("test-platform", pin)
    final_path.parent.mkdir(parents=True)
    final_path.write_text("binary", encoding="utf-8")
    observed: list[tuple[str, str, dict]] = []

    monkeypatch.setattr(
        local_install, "llama_server_artifact_key", lambda: "test-platform"
    )
    monkeypatch.setattr(local_install, "pin_for_current_platform", lambda: pin)

    def fake_download(_url, _dest, **_kwargs):
        observed.append(
            ("download", _local_status()["install_state"], dict(_local_slot()))
        )

    def fake_verify(_path, _expected):
        observed.append(
            ("verify", _local_status()["install_state"], dict(_local_slot()))
        )

    monkeypatch.setattr(local_install, "_download_file", fake_download)
    monkeypatch.setattr(local_install, "_verify_sha256", fake_verify)
    monkeypatch.setattr(
        local_install, "_safe_extract_tarball", lambda _tarball, _dest: None
    )
    monkeypatch.setattr(
        local_install, "_find_extracted_binary", lambda _dest, _name: final_path
    )
    monkeypatch.setattr(local_install, "_chmod_executable", lambda _path: None)
    monkeypatch.setattr(local_install, "_clear_macos_quarantine", lambda _path: None)

    result = local_install.install_llama_server()

    assert [entry[0] for entry in observed] == ["download", "verify"]
    assert observed[0][1] == "downloading"
    assert observed[0][2]["binary_artifact"] == "llama.tar.gz"
    assert observed[1][1] == "verifying"
    assert result["install_state"] == "installed"
    slot = _local_slot()
    assert slot["install_state"] == "installed"
    assert slot["binary_artifact"] == "llama.tar.gz"
    assert slot["binary_sha256"] == "abc123"
    assert slot["binary_path"] == str(final_path)
    assert "state" not in slot


def test_probe_binary_runnable_returns_true_for_zero_exit(tmp_path):
    script = _write_probe_script(tmp_path, "exit 0")

    assert local_install.probe_binary_runnable(script) == (True, None)


def test_probe_binary_runnable_returns_verbatim_loader_stderr(tmp_path):
    detail = "dyld: Library not loaded: @rpath/libfoo.dylib"
    script = _write_probe_script(tmp_path, f"echo '{detail}' >&2\nexit 1")

    runnable, error = local_install.probe_binary_runnable(script)

    assert runnable is False
    assert error == detail


def test_probe_binary_runnable_returns_verbatim_non_loader_stderr(tmp_path):
    detail = "plain launch failure"
    script = _write_probe_script(tmp_path, f"echo '{detail}' >&2\nexit 2")

    runnable, error = local_install.probe_binary_runnable(script)

    assert runnable is False
    assert error == detail


def test_probe_binary_runnable_uses_stdout_when_stderr_empty(tmp_path):
    detail = "stdout launch failure"
    script = _write_probe_script(tmp_path, f"echo '{detail}'\nexit 3")

    runnable, error = local_install.probe_binary_runnable(script)

    assert runnable is False
    assert error == detail


def test_probe_binary_runnable_times_out(tmp_path, monkeypatch):
    script = _write_probe_script(tmp_path, "sleep 5")
    monkeypatch.setattr(local_install, "_PROBE_TIMEOUT_SECONDS", 0.5)

    started_at = time.monotonic()
    runnable, error = local_install.probe_binary_runnable(script)

    assert time.monotonic() - started_at < 2
    assert runnable is False
    assert error is not None
    assert error.startswith("timed out")


def test_probe_binary_runnable_handles_missing_path(tmp_path):
    runnable, error = local_install.probe_binary_runnable(tmp_path / "missing")

    assert runnable is False
    assert error


def test_install_model_writes_canonical_sequence(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    spec = LOCAL_MODEL_SPECS[LOCAL_MODEL]
    observed: list[tuple[str, str, dict]] = []

    def fake_download(_url, _dest, **_kwargs):
        observed.append(
            ("download", _local_status()["install_state"], dict(_local_slot()))
        )

    def fake_verify(_path, _expected):
        observed.append(
            ("verify", _local_status()["install_state"], dict(_local_slot()))
        )

    monkeypatch.setattr(local_install, "_download_file", fake_download)
    monkeypatch.setattr(local_install, "_verify_sha256", fake_verify)

    result = local_install.install_model(LOCAL_MODEL)

    assert [entry[0] for entry in observed] == [
        "download",
        "download",
        "verify",
        "verify",
    ]
    assert observed[0][1] == "downloading"
    assert observed[0][2]["model_id"] == LOCAL_MODEL
    assert observed[2][1] == "verifying"
    assert result["install_state"] == "installed"
    slot = _local_slot()
    assert slot["install_state"] == "installed"
    assert slot["model_id"] == LOCAL_MODEL
    assert slot["model_path"] == str(local_install.model_path(spec.model_id))
    assert slot["model_sha256"] == spec.sha256
    assert slot["mmproj_path"] == str(local_install.mmproj_path(spec.model_id))
    assert slot["mmproj_sha256"] == spec.mmproj_sha256
    assert "state" not in slot


def test_install_model_threads_optional_mmproj_artifact(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    spec = replace(
        LOCAL_MODEL_SPECS[LOCAL_MODEL],
        mmproj_filename="mmproj-test.gguf",
        mmproj_sha256="mmproj-sha",
    )
    downloads: list[Path] = []
    verifies: list[tuple[Path, str]] = []

    monkeypatch.setitem(local_install.LOCAL_MODEL_SPECS, LOCAL_MODEL, spec)

    def fake_download(_url, dest, **_kwargs):
        downloads.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"artifact")

    def fake_verify(path, expected):
        verifies.append((path, expected))

    monkeypatch.setattr(local_install, "_download_file", fake_download)
    monkeypatch.setattr(local_install, "_verify_sha256", fake_verify)

    local_install.install_model(LOCAL_MODEL)

    gguf_path = local_install.model_path(LOCAL_MODEL)
    mmproj_path = local_install.mmproj_path(LOCAL_MODEL)
    assert mmproj_path is not None
    assert downloads == [gguf_path, mmproj_path]
    assert verifies == [(gguf_path, spec.sha256), (mmproj_path, "mmproj-sha")]
    slot = _local_slot()
    assert slot["mmproj_path"] == str(mmproj_path)
    assert slot["mmproj_sha256"] == "mmproj-sha"


def test_ensure_artifacts_installed_returns_binary_gguf_and_optional_mmproj(
    tmp_path, monkeypatch
):
    binary = tmp_path / "llama-server"
    gguf = tmp_path / "model.gguf"
    mmproj = tmp_path / "mmproj.gguf"
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda model_id: {
            "binary_installed": True,
            "model_installed": True,
            "ram_sufficient": True,
            "binary_path": str(binary),
            "model_path": str(gguf),
            "mmproj_path": str(mmproj),
        },
    )

    assert local_install.ensure_artifacts_installed(LOCAL_MODEL) == (
        binary,
        gguf,
        mmproj,
    )


def test_ensure_artifacts_installed_ignores_low_memory_when_artifacts_exist(
    tmp_path, monkeypatch
):
    binary = tmp_path / "llama-server"
    gguf = tmp_path / "model.gguf"
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda model_id: {
            "binary_installed": True,
            "model_installed": True,
            "ram_sufficient": False,
            "binary_path": str(binary),
            "model_path": str(gguf),
            "mmproj_path": None,
        },
    )

    assert local_install.ensure_artifacts_installed(LOCAL_MODEL) == (
        binary,
        gguf,
        None,
    )


def test_inspect_readiness_reports_ram_sufficient_for_low_or_unknown_memory(
    tmp_path, monkeypatch
):
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(
        memory.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=1 * 1024**3, total=16 * 1024**3),
    )

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    assert readiness["ram_sufficient"] is True


def test_inspect_readiness_reports_gpu_available_with_hardware(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(
        local_vulkan,
        "detect_gpus",
        lambda: [
            local_vulkan.VulkanDevice(
                1,
                "NVIDIA GeForce GTX 1660 Ti",
                local_vulkan.VK_TYPE_DISCRETE,
                6390,
            )
        ],
    )

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    assert readiness["gpu_available"] is True


def test_inspect_readiness_reports_gpu_unavailable_without_hardware(
    tmp_path, monkeypatch
):
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    assert readiness["gpu_available"] is False


def test_inspect_readiness_honors_vulkan_device_override(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    devices = [
        local_vulkan.VulkanDevice(
            0,
            "Intel(R) Graphics",
            local_vulkan.VK_TYPE_INTEGRATED,
            23814,
        ),
        local_vulkan.VulkanDevice(
            1,
            "llvmpipe (LLVM)",
            local_vulkan.VK_TYPE_CPU,
            0,
        ),
    ]
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: devices)
    local_install._write_local_metadata({"vulkan_device_index": "0"})

    assert local_install.gpu_device_override() == 0
    assert local_install.inspect_readiness(LOCAL_MODEL)["gpu_available"] is True

    local_install._write_local_metadata({"vulkan_device_index": "1"})

    assert local_install.inspect_readiness(LOCAL_MODEL)["gpu_available"] is False


def test_inspect_readiness_ignores_stale_model_path_after_model_change(
    tmp_path, monkeypatch
):
    # A record left by a prior model's install (different model_id, gguf under a
    # different model dir) must NOT be trusted: a LOCAL_MODEL change without a
    # reinstall would otherwise pair the stale gguf with the new model's mmproj
    # and abort llama-server with an n_embd text/projector mismatch. Readiness
    # recomputes both artifact paths from the selected model's spec.
    _init_journal(tmp_path, monkeypatch)
    stale_dir = local_install.model_dir("local/old-coder-7b")
    stale_dir.mkdir(parents=True, exist_ok=True)
    stale_gguf = stale_dir / "coder-7b-Q4_K_M.gguf"
    stale_gguf.write_text("stale", encoding="utf-8")
    local_install._write_local_metadata(
        {"model_id": "local/old-coder-7b", "model_path": str(stale_gguf)}
    )

    # Stage the selected model's artifacts in its own directory.
    gguf = local_install.model_path(LOCAL_MODEL)
    gguf.parent.mkdir(parents=True, exist_ok=True)
    gguf.write_text("qwen", encoding="utf-8")
    mmproj = local_install.mmproj_path(LOCAL_MODEL)
    assert mmproj is not None
    mmproj.write_text("mmproj", encoding="utf-8")

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    assert readiness["model_id"] == LOCAL_MODEL
    assert readiness["model_path"] == str(gguf)
    assert readiness["mmproj_path"] == str(mmproj)
    assert Path(readiness["model_path"]).parent == local_install.model_dir(LOCAL_MODEL)
    assert readiness["model_path"] != str(stale_gguf)
    assert readiness["model_installed"] is True


def test_inspect_readiness_not_installed_off_stale_record(tmp_path, monkeypatch):
    # With only the prior model's artifacts on disk and the selected model not
    # staged, readiness must report not-installed rather than claiming installed
    # off the stale record's gguf.
    _init_journal(tmp_path, monkeypatch)
    stale_dir = local_install.model_dir("local/old-coder-7b")
    stale_dir.mkdir(parents=True, exist_ok=True)
    stale_gguf = stale_dir / "coder-7b-Q4_K_M.gguf"
    stale_gguf.write_text("stale", encoding="utf-8")
    local_install._write_local_metadata(
        {"model_id": "local/old-coder-7b", "model_path": str(stale_gguf)}
    )

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    assert readiness["model_installed"] is False
    assert readiness["gguf_installed"] is False
    assert readiness["model_path"] == str(local_install.model_path(LOCAL_MODEL))


def test_install_llama_server_failure_writes_canonical_failed(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    pin = {
        "release_tag": "v1",
        "filename": "llama.tar.gz",
        "sha256": "abc123",
        "binary_name": "llama-server",
    }
    monkeypatch.setattr(
        local_install, "llama_server_artifact_key", lambda: "test-platform"
    )
    monkeypatch.setattr(local_install, "pin_for_current_platform", lambda: pin)

    def fake_download(_url, _dest, **_kwargs):
        raise RuntimeError("network broke")

    monkeypatch.setattr(local_install, "_download_file", fake_download)

    with pytest.raises(RuntimeError, match="network broke"):
        local_install.install_llama_server()

    status = _local_status()
    assert status["install_state"] == "failed"
    assert status["install_error"] == "network broke"
    slot = _local_slot()
    assert slot["install_state"] == "failed"
    assert slot["install_error"] == "network broke"
    assert "state" not in slot
