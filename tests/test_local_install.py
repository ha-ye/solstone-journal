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
from solstone.think.providers import (
    fit_report,
    local_cuda,
    local_install,
    local_vulkan,
    memory,
    oci_image,
)
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


def _fit(severity: fit_report.FitSeverity) -> fit_report.FitReport:
    return fit_report.FitReport(
        artifact="test local",
        checks=(fit_report.FitCheck("test", severity, f"{severity} detail"),),
    )


@pytest.fixture(autouse=True)
def _default_vulkan_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        local_cuda,
        "resolve_local_backend",
        lambda _pin: local_cuda.BackendChoice("vulkan", "test vulkan"),
    )


def _write_probe_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "probe.sh"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return script


class _FakeStream:
    def __init__(
        self,
        chunks: list[bytes],
        chunk_times: list[float],
        total: int | None,
        clock: list[float],
    ) -> None:
        self._chunks = chunks
        self._chunk_times = chunk_times
        self.headers = {"content-length": str(total)} if total is not None else {}
        self._clock = clock

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        for chunk, t in zip(self._chunks, self._chunk_times, strict=True):
            self._clock[0] = t
            yield chunk


def _download_with_fake_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chunks: list[bytes],
    chunk_times: list[float],
) -> tuple[Path, list[tuple[int, int | None]]]:
    clock = [0.0]
    total = sum(len(chunk) for chunk in chunks)
    calls: list[tuple[int, int | None]] = []
    monkeypatch.setattr(local_install.time, "monotonic", lambda: clock[0])

    def fake_stream(method, url, **_kwargs):
        assert method == "GET"
        assert url == "https://example.test/artifact"
        return _FakeStream(chunks, chunk_times, total, clock)

    def record_progress(received: int, reported_total: int | None) -> None:
        calls.append((received, reported_total))

    monkeypatch.setattr("httpx.stream", fake_stream)
    dest = tmp_path / "artifact.bin"
    local_install._download_file(
        "https://example.test/artifact",
        dest,
        on_progress=record_progress,
    )
    return dest, calls


def test_install_hint_literal() -> None:
    assert local_install.install_hint() == "journal install-provider local"


def test_download_file_rate_limits_many_progress_chunks(tmp_path, monkeypatch):
    chunks = [b"x"] * 20
    chunk_times = [index * 0.01 for index in range(len(chunks))]

    _dest, calls = _download_with_fake_stream(
        tmp_path, monkeypatch, chunks, chunk_times
    )

    total = sum(len(chunk) for chunk in chunks)
    assert calls == [(1, total), (total, total)]
    assert len(calls) < len(chunks)


def test_download_file_emits_first_progress_promptly(tmp_path, monkeypatch):
    chunks = [b"abc", b"de"]
    chunk_times = [0.4, 0.5]

    _dest, calls = _download_with_fake_stream(
        tmp_path, monkeypatch, chunks, chunk_times
    )

    assert calls[0] == (len(chunks[0]), sum(len(chunk) for chunk in chunks))


def test_download_file_emits_interval_crossing_progress(tmp_path, monkeypatch):
    chunks = [b"a", b"bb", b"ccc", b"dddd", b"eeeee"]
    chunk_times = [0.0, 0.1, 0.2, 1.5, 1.6]

    _dest, calls = _download_with_fake_stream(
        tmp_path, monkeypatch, chunks, chunk_times
    )

    total = sum(len(chunk) for chunk in chunks)
    boundary_received = sum(len(chunk) for chunk in chunks[:4])
    assert calls == [(1, total), (boundary_received, total), (total, total)]


def test_download_file_emits_final_progress_once_with_dedupe(tmp_path, monkeypatch):
    chunks = [b"aa", b"bbb"]

    _dest, inside_window_calls = _download_with_fake_stream(
        tmp_path,
        monkeypatch,
        chunks,
        [0.0, 0.2],
    )
    total = sum(len(chunk) for chunk in chunks)
    assert inside_window_calls[-1] == (total, total)
    assert inside_window_calls.count((total, total)) == 1

    _dest, last_chunk_emit_calls = _download_with_fake_stream(
        tmp_path,
        monkeypatch,
        chunks,
        [0.0, 1.5],
    )
    assert len(last_chunk_emit_calls) == 2
    assert last_chunk_emit_calls.count((total, total)) == 1


def test_download_file_writes_dest_and_replaces_tmp(tmp_path, monkeypatch):
    chunks = [b"ab", b"cd", b"ef"]
    chunk_times = [0.0, 0.1, 0.2]

    dest, _calls = _download_with_fake_stream(
        tmp_path, monkeypatch, chunks, chunk_times
    )

    assert dest.read_bytes() == b"".join(chunks)
    assert not dest.with_suffix(dest.suffix + ".tmp").exists()


@pytest.mark.parametrize(
    ("machine", "arch"),
    [
        ("x86_64", "amd64"),
        ("amd64", "amd64"),
        ("x64", "amd64"),
        ("aarch64", "arm64"),
        ("arm64", "arm64"),
    ],
)
def test_oci_arch_mapping(machine: str, arch: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(local_install.platform, "machine", lambda: machine)

    assert local_install._oci_arch() == arch


def test_oci_arch_unsupported_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(local_install.platform, "machine", lambda: "riscv64")

    with pytest.raises(local_install.LocalProviderError) as exc_info:
        local_install._oci_arch()

    assert exc_info.value.reason_code == "unsupported_platform"


def test_cuda_binary_paths_include_index_digest(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    digest = local_install.CUDA_SERVER_PIN.image_ref.split("@sha256:", 1)[1]

    assert local_install.cuda_binary_dir() == (
        tmp_path
        / "cache"
        / "providers"
        / "local"
        / "cuda"
        / local_install.llama_server_artifact_key()
        / digest
    )
    assert local_install.cuda_binary_path() == (
        local_install.cuda_binary_dir() / local_install.CUDA_SERVER_PIN.binary_name
    )


@pytest.mark.parametrize(
    ("arch", "expected_cpu", "unexpected_cpu", "cpu_count"),
    [
        ("amd64", "libggml-cpu-haswell.so", "libggml-cpu-armv8.0_1.so", 14),
        ("arm64", "libggml-cpu-armv8.0_1.so", "libggml-cpu-haswell.so", 8),
    ],
)
def test_cuda_server_pin_wanted_files_are_arch_specific(
    arch: str,
    expected_cpu: str,
    unexpected_cpu: str,
    cpu_count: int,
) -> None:
    wanted_files = local_install.CUDA_SERVER_PIN.wanted_files_for_arch(arch)
    cpu_files = [name for name in wanted_files if name.startswith("libggml-cpu-")]

    assert "llama-server" in wanted_files
    assert "libcudart.so.13" in wanted_files
    assert "libcublas.so.13" in wanted_files
    assert expected_cpu in wanted_files
    assert unexpected_cpu not in wanted_files
    assert len(cpu_files) == cpu_count


def test_cuda_server_pin_wanted_files_reject_unknown_arch() -> None:
    with pytest.raises(local_install.LocalProviderError) as exc_info:
        local_install.CUDA_SERVER_PIN.wanted_files_for_arch("ppc64le")

    assert exc_info.value.reason_code == "unsupported_platform"


def test_llama_server_pins_are_complete_immutable_artifacts() -> None:
    expected = {
        "aarch64-apple-darwin": {
            "release_tag": "b10068",
            "filename": "llama-b10068-bin-macos-arm64.tar.gz",
            "sha256": "13aa2d40c76ad1dcb8ebeec5f0d2814bf3b2f84a66935c7d4dc6f7cca8e38d68",
            "binary_name": "llama-server",
        },
        "x86_64-unknown-linux-gnu": {
            "release_tag": "b10068",
            "filename": "llama-b10068-bin-ubuntu-vulkan-x64.tar.gz",
            "sha256": "713641920dce6c8efb953ebc9ffa309977e200cec5e182e6ad0e8b086203cdc3",
            "binary_name": "llama-server",
        },
        "aarch64-unknown-linux-gnu": {
            "release_tag": "b10068",
            "filename": "llama-b10068-bin-ubuntu-vulkan-arm64.tar.gz",
            "sha256": "c3c49e6e124a574165ca28317be021b1a12a2ea06977e3eb7daee3eb443eb186",
            "binary_name": "llama-server",
        },
    }
    pins = local_install.LLAMA_SERVER_PINS

    assert set(pins) == set(expected)
    for key, expected_pin in expected.items():
        assert pins[key] == expected_pin


def test_install_llama_server_relocates_binary_and_libraries(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    pin = local_install.pin_for_current_platform()
    if local_install.llama_server_artifact_key() == "x86_64-unknown-linux-gnu":
        assert pin["filename"] == "llama-b10068-bin-ubuntu-vulkan-x64.tar.gz"
        assert (
            pin["sha256"]
            == "713641920dce6c8efb953ebc9ffa309977e200cec5e182e6ad0e8b086203cdc3"
        )
    artifact_key = local_install.llama_server_artifact_key()
    install_dir = local_install.binary_install_dir(artifact_key, pin)
    binary_path = local_install.binary_path_for_pin(artifact_key, pin)
    inner_name = "llama-b10068"
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


def test_install_llama_server_sha256_mismatch_fails_closed_before_extract(
    tmp_path, monkeypatch
):
    _init_journal(tmp_path, monkeypatch)
    expected_urls = {
        "aarch64-apple-darwin": (
            "https://github.com/ggml-org/llama.cpp/releases/download/b10068/"
            "llama-b10068-bin-macos-arm64.tar.gz"
        ),
        "x86_64-unknown-linux-gnu": (
            "https://github.com/ggml-org/llama.cpp/releases/download/b10068/"
            "llama-b10068-bin-ubuntu-vulkan-x64.tar.gz"
        ),
        "aarch64-unknown-linux-gnu": (
            "https://github.com/ggml-org/llama.cpp/releases/download/b10068/"
            "llama-b10068-bin-ubuntu-vulkan-arm64.tar.gz"
        ),
    }
    artifact_key = local_install.llama_server_artifact_key()
    pin = local_install.pin_for_current_platform()
    install_dir = local_install.binary_install_dir(artifact_key, pin)
    binary_path = local_install.binary_path_for_pin(artifact_key, pin)
    inner_name = "llama-b10068"
    fixture_root = tmp_path / "fixture" / inner_name
    fixture_root.mkdir(parents=True)
    (fixture_root / pin["binary_name"]).write_bytes(b"not the pinned server")
    fixture_tarball = tmp_path / pin["filename"]
    with tarfile.open(fixture_tarball, "w:gz") as archive:
        archive.add(fixture_root, arcname=inner_name)
    download_urls: list[str] = []

    def fake_download(url, dest, **_kwargs):
        download_urls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture_tarball, dest)

    monkeypatch.setattr(local_install, "_download_file", fake_download)

    with pytest.raises(local_install.LocalProviderError) as exc_info:
        local_install.install_llama_server()

    assert exc_info.value.reason_code == "sha256_mismatch"
    assert download_urls == [expected_urls[artifact_key]]
    status = _local_status()
    assert status["install_state"] == "failed"
    assert status["install_error"] is not None
    assert "sha256 mismatch" in status["install_error"]
    slot = _local_slot()
    assert slot["binary_artifact"] == pin["filename"]
    assert "binary_sha256" not in slot
    assert "binary_path" not in slot
    assert not binary_path.exists()
    assert not (install_dir / inner_name).exists()
    assert sorted(child.name for child in install_dir.iterdir()) == [pin["filename"]]


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


@pytest.mark.parametrize(
    ("machine", "arch", "expected_cpu", "unexpected_cpu"),
    [
        ("x86_64", "amd64", "libggml-cpu-haswell.so", "libggml-cpu-armv8.0_1.so"),
        ("arm64", "arm64", "libggml-cpu-armv8.0_1.so", "libggml-cpu-haswell.so"),
    ],
)
def test_install_llama_server_cuda_uses_arch_specific_oci_wanted_files(
    tmp_path,
    monkeypatch,
    machine: str,
    arch: str,
    expected_cpu: str,
    unexpected_cpu: str,
):
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(local_install.platform, "machine", lambda: machine)
    monkeypatch.setattr(
        local_cuda,
        "resolve_local_backend",
        lambda _pin: local_cuda.BackendChoice("cuda", "test cuda"),
    )
    metadata_calls: list[dict[str, str]] = []
    pull_calls: list[tuple[str, str, tuple[str, ...], Path]] = []

    def fake_pull_and_install(
        image_ref: str,
        arch: str,
        wanted_files: tuple[str, ...],
        target_dir: Path,
        *,
        policy: oci_image.OciSignaturePolicy | None = None,
    ) -> oci_image.OciInstallResult:
        assert policy is local_install.CUDA_SERVER_PIN.signature_policy
        target_dir.mkdir(parents=True, exist_ok=True)
        binary = target_dir / local_install.CUDA_SERVER_PIN.binary_name
        binary.write_text("binary", encoding="utf-8")
        pull_calls.append((image_ref, arch, wanted_files, target_dir))
        return oci_image.OciInstallResult(
            target_dir=target_dir,
            files={},
            already_present=False,
        )

    monkeypatch.setattr(oci_image, "pull_and_install", fake_pull_and_install)
    monkeypatch.setattr(
        local_install,
        "_write_local_metadata",
        lambda updates: metadata_calls.append(updates),
    )

    result = local_install.install_llama_server()

    wanted_files = local_install.CUDA_SERVER_PIN.wanted_files_for_arch(arch)
    assert result["install_state"] == "installed"
    assert pull_calls == [
        (
            local_install.CUDA_SERVER_PIN.image_ref,
            arch,
            wanted_files,
            local_install.cuda_binary_dir(),
        )
    ]
    assert expected_cpu in pull_calls[0][2]
    assert unexpected_cpu not in pull_calls[0][2]
    assert metadata_calls == []
    assert local_install.cuda_binary_path().stat().st_mode & 0o111


def test_install_llama_server_vulkan_choice_does_not_pull_oci(tmp_path, monkeypatch):
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

    monkeypatch.setattr(
        local_install, "llama_server_artifact_key", lambda: "test-platform"
    )
    monkeypatch.setattr(local_install, "pin_for_current_platform", lambda: pin)
    monkeypatch.setattr(local_install, "_download_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(local_install, "_verify_sha256", lambda _path, _expected: None)
    monkeypatch.setattr(
        local_install, "_safe_extract_tarball", lambda _tarball, _dest: None
    )
    monkeypatch.setattr(
        local_install, "_find_extracted_binary", lambda _dest, _name: final_path
    )
    monkeypatch.setattr(local_install, "_chmod_executable", lambda _path: None)
    monkeypatch.setattr(local_install, "_clear_macos_quarantine", lambda _path: None)
    monkeypatch.setattr(
        oci_image,
        "pull_and_install",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OCI pull not expected")
        ),
    )

    result = local_install.install_llama_server()

    assert result["install_state"] == "installed"
    assert _local_slot()["binary_path"] == str(final_path)


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


def test_install_local_blocks_before_downloads(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda model_id: {"binary_installed": False, "model_installed": False},
    )
    monkeypatch.setattr(
        fit_report, "build_local_fit_report", lambda model_id: _fit("blocked")
    )
    monkeypatch.setattr(
        local_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
    )
    monkeypatch.setattr(
        oci_image,
        "pull_and_install",
        lambda *_args, **_kwargs: pytest.fail("OCI pull should not start"),
    )

    with pytest.raises(local_install.LocalProviderError) as exc_info:
        local_install.install_local(LOCAL_MODEL)

    assert exc_info.value.reason_code == "host_unfit"


def test_install_local_warning_continues_to_download(tmp_path, monkeypatch):
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
    downloads: list[Path] = []

    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda model_id: {"binary_installed": False, "model_installed": False},
    )
    monkeypatch.setattr(
        fit_report, "build_local_fit_report", lambda model_id: _fit("warning")
    )
    monkeypatch.setattr(
        local_install, "llama_server_artifact_key", lambda: "test-platform"
    )
    monkeypatch.setattr(local_install, "pin_for_current_platform", lambda: pin)

    def fake_download(_url, dest, **_kwargs):
        downloads.append(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"artifact")

    monkeypatch.setattr(local_install, "_download_file", fake_download)
    monkeypatch.setattr(local_install, "_verify_sha256", lambda _path, _expected: None)
    monkeypatch.setattr(
        local_install, "_safe_extract_tarball", lambda _tarball, _dest: None
    )
    monkeypatch.setattr(
        local_install, "_find_extracted_binary", lambda _dest, _name: final_path
    )
    monkeypatch.setattr(local_install, "_chmod_executable", lambda _path: None)
    monkeypatch.setattr(local_install, "_clear_macos_quarantine", lambda _path: None)
    monkeypatch.setattr(
        oci_image,
        "pull_and_install",
        lambda *_args, **_kwargs: pytest.fail("OCI pull should not start"),
    )

    assert local_install.install_local(LOCAL_MODEL)["install_state"] == "installed"

    assert downloads


def test_install_local_ready_short_circuits_before_fit_report(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda model_id: {"binary_installed": True, "model_installed": True},
    )
    monkeypatch.setattr(
        fit_report,
        "build_local_fit_report",
        lambda model_id: pytest.fail("fit report should not run"),
    )
    monkeypatch.setattr(
        local_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
    )
    monkeypatch.setattr(
        oci_image,
        "pull_and_install",
        lambda *_args, **_kwargs: pytest.fail("OCI pull should not start"),
    )

    result = local_install.install_local(LOCAL_MODEL)

    assert result["name"] == local_install.LOCAL_PROVIDER_NAME
    assert result["install_state"] == "installed"


def test_install_local_reinstalls_runtime_when_binary_record_stale(
    tmp_path, monkeypatch
):
    _init_journal(tmp_path, monkeypatch)
    gguf = local_install.model_path(LOCAL_MODEL)
    gguf.parent.mkdir(parents=True, exist_ok=True)
    gguf.write_text("qwen", encoding="utf-8")
    mmproj = local_install.mmproj_path(LOCAL_MODEL)
    assert mmproj is not None
    mmproj.write_text("mmproj", encoding="utf-8")
    canonical = local_install.binary_path_for_pin()
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"llama-server")
    canonical.chmod(0o755)
    local_install._write_local_metadata(
        {
            "binary_artifact": "llama-stale-bin-ubuntu-x64.tar.gz",
            "binary_sha256": "deadbeef" * 8,
            "binary_path": str(canonical),
        }
    )
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])
    monkeypatch.setattr(
        fit_report, "build_local_fit_report", lambda model_id: _fit("ok")
    )
    calls: list[str] = []

    def fake_install_llama_server():
        calls.append("llama_server")
        return {"install_state": "installed"}

    def fake_install_model(model_id: str):
        calls.append("model")
        return {"install_state": "installed", "model_id": model_id}

    monkeypatch.setattr(
        local_install, "install_llama_server", fake_install_llama_server
    )
    monkeypatch.setattr(local_install, "install_model", fake_install_model)

    result = local_install.install_local(LOCAL_MODEL)

    assert result == {"install_state": "installed", "model_id": LOCAL_MODEL}
    assert calls == ["llama_server", "model"]


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
            "backend": "vulkan",
            "backend_reason": "test vulkan",
        },
    )

    assert local_install.ensure_artifacts_installed(
        LOCAL_MODEL
    ) == local_install.LocalArtifacts(
        backend="vulkan",
        backend_reason="test vulkan",
        binary_path=binary,
        lib_dir=None,
        gguf_path=gguf,
        mmproj_path=mmproj,
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
            "backend": "vulkan",
            "backend_reason": "test vulkan",
        },
    )

    assert local_install.ensure_artifacts_installed(
        LOCAL_MODEL
    ) == local_install.LocalArtifacts(
        backend="vulkan",
        backend_reason="test vulkan",
        binary_path=binary,
        lib_dir=None,
        gguf_path=gguf,
        mmproj_path=None,
    )


def test_ensure_artifacts_installed_returns_cuda_lib_dir(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    binary = tmp_path / "llama-server"
    gguf = tmp_path / "model.gguf"
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda model_id: {
            "binary_installed": True,
            "model_installed": True,
            "ram_sufficient": True,
            "binary_path": str(binary),
            "model_path": str(gguf),
            "mmproj_path": None,
            "backend": "cuda",
            "backend_reason": "test cuda",
        },
    )

    assert local_install.ensure_artifacts_installed(
        LOCAL_MODEL
    ) == local_install.LocalArtifacts(
        backend="cuda",
        backend_reason="test cuda",
        binary_path=binary,
        lib_dir=local_install.cuda_binary_dir(),
        gguf_path=gguf,
        mmproj_path=None,
    )


@pytest.mark.parametrize(
    ("binary_installed", "model_installed", "reason_code"),
    [(False, True, "binary_missing"), (True, False, "model_missing")],
)
def test_ensure_artifacts_installed_raises_for_missing_artifacts(
    tmp_path,
    monkeypatch,
    binary_installed,
    model_installed,
    reason_code,
):
    binary = tmp_path / "llama-server"
    gguf = tmp_path / "model.gguf"
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda model_id: {
            "binary_installed": binary_installed,
            "model_installed": model_installed,
            "ram_sufficient": True,
            "binary_path": str(binary),
            "model_path": str(gguf),
            "mmproj_path": None,
            "backend": "vulkan",
            "backend_reason": "test vulkan",
        },
    )

    with pytest.raises(local_install.LocalProviderError) as exc_info:
        local_install.ensure_artifacts_installed(LOCAL_MODEL)

    assert exc_info.value.reason_code == reason_code


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


@pytest.mark.parametrize("sidecar_ok", [True, False])
def test_inspect_readiness_cuda_uses_sidecar_full_set(
    tmp_path,
    monkeypatch,
    sidecar_ok,
):
    from solstone.think.providers import oci_image

    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(
        local_cuda,
        "resolve_local_backend",
        lambda _pin: local_cuda.BackendChoice("cuda", "test cuda"),
    )
    binary = local_install.cuda_binary_path()
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755)
    verify_calls: list[tuple[str, str, tuple[str, ...], Path]] = []

    def fake_verify(
        image_ref: str,
        arch: str,
        wanted_files: tuple[str, ...],
        target_dir: Path,
    ) -> bool:
        verify_calls.append((image_ref, arch, wanted_files, target_dir))
        return sidecar_ok

    monkeypatch.setattr(oci_image, "verify_sidecar_install", fake_verify)
    monkeypatch.setattr(
        local_vulkan,
        "detect_gpus",
        lambda: (_ for _ in ()).throw(
            AssertionError("Vulkan probe not expected for CUDA readiness")
        ),
    )

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    wanted_files = local_install.CUDA_SERVER_PIN.wanted_files_for_arch(
        local_install._oci_arch()
    )
    assert readiness["backend"] == "cuda"
    assert readiness["backend_reason"] == "test cuda"
    assert readiness["binary_path"] == str(binary)
    assert readiness["binary_installed"] is sidecar_ok
    assert readiness["gpu_available"] is True
    assert readiness["gpu_probe_ok"] is True
    assert verify_calls == [
        (
            local_install.CUDA_SERVER_PIN.image_ref,
            local_install._oci_arch(),
            wanted_files,
            local_install.cuda_binary_dir(),
        )
    ]


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
    assert readiness["backend"] == "vulkan"
    assert readiness["backend_reason"] == "test vulkan"


def test_inspect_readiness_reports_gpu_unavailable_without_hardware(
    tmp_path, monkeypatch
):
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    assert readiness["gpu_available"] is False


def test_inspect_readiness_stale_non_cuda_binary_record_reports_not_installed(
    tmp_path, monkeypatch
):
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])
    canonical = local_install.binary_path_for_pin()
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"llama-server")
    canonical.chmod(0o755)
    local_install._write_local_metadata(
        {
            "binary_artifact": "llama-stale-bin-ubuntu-x64.tar.gz",
            "binary_sha256": "deadbeef" * 8,
            "binary_path": str(canonical),
        }
    )

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    assert readiness["binary_installed"] is False
    assert readiness["binary_path"] == str(canonical)


def test_inspect_readiness_matching_non_cuda_binary_record_reports_installed(
    tmp_path, monkeypatch
):
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])
    pin = local_install.pin_for_current_platform()
    canonical = local_install.binary_path_for_pin()
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"llama-server")
    canonical.chmod(0o755)
    local_install._write_local_metadata(
        {
            "binary_artifact": pin["filename"],
            "binary_sha256": pin["sha256"],
            "binary_path": str(canonical),
        }
    )

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    assert readiness["binary_installed"] is True


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
