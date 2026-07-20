# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from solstone.think.models import LOCAL_MODEL
from solstone.think.providers import (
    fit_report,
    local_cuda,
    local_install,
    local_vulkan,
    memory,
)
from solstone.think.providers.artifact_proof import (
    ProofResult,
    ReadinessOutcome,
    artifact_manifest_path,
    prove_manifest,
)
from solstone.think.providers.install_state import (
    begin_or_replace_install_attempt,
    canonical_fingerprint,
    fingerprint_sha256,
    read_install_status,
    transition_state,
    write_install_status,
)
from solstone.think.providers.local import LOCAL_MODEL_SPECS
from solstone.think.providers.local_endpoint import resolve_local_endpoint


def _init_journal(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "journal.json").write_text(
        json.dumps({"providers": {}}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))


def _local_status() -> dict:
    return read_install_status(name="local")


def _write_provider_local_config(tmp_path: Path, updates: dict[str, object]) -> None:
    path = tmp_path / "config" / "journal.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    provider_config = config.setdefault("providers", {}).setdefault("local", {})
    provider_config.update(updates)
    path.write_text(json.dumps(config) + "\n", encoding="utf-8")


def _fake_local_readiness(
    *,
    binary_installed: bool,
    model_installed: bool,
    binary_path: Path,
    model_path: Path,
    mmproj_path: Path | None = None,
    ram_sufficient: bool = True,
    backend: str = "vulkan",
    backend_reason: str = "test vulkan",
) -> ReadinessOutcome:
    missing_binary = not binary_installed
    missing_model = not model_installed
    status = (
        "ready" if not missing_binary and not missing_model else "missing-or-mismatched"
    )
    reason_code = (
        "ready"
        if status == "ready"
        else "binary_missing"
        if missing_binary
        else "model_missing"
    )
    binary_status = "ready" if binary_installed else "missing-or-mismatched"
    model_status = "ready" if model_installed else "missing-or-mismatched"
    return ReadinessOutcome(
        provider=local_install.LOCAL_PROVIDER_NAME,
        status=status,
        reason_code=reason_code,
        target={"model_id": LOCAL_MODEL},
        install={
            "install_state": "idle",
            "install_error": None,
            "error_code": None,
            "attempt_id": None,
            "progress_bytes_received": None,
            "progress_bytes_total": None,
            "last_transition_at": None,
            "last_progress_at": None,
        },
        host={
            "ram_sufficient": ram_sufficient,
            "gpu_available": True,
            "gpu_probe_ok": True,
            "backend": backend,
            "backend_reason": backend_reason,
        },
        artifacts={
            "binary_installed": binary_installed,
            "model_installed": model_installed,
            "binary_path": str(binary_path),
            "model_path": str(model_path),
            "mmproj_path": str(mmproj_path) if mmproj_path is not None else None,
        },
        proof={
            "binary": {
                "status": binary_status,
                "reason_code": "ready" if binary_installed else "manifest_missing",
                "cache_hit": False,
            },
            "model": {
                "status": model_status,
                "reason_code": "ready" if model_installed else "manifest_missing",
                "cache_hit": False,
            },
        },
    )


def _write_ready_vulkan_manifest(
    *,
    artifact_key: str | None = None,
    pin: dict[str, str] | None = None,
) -> None:
    resolved_pin = pin or local_install.pin_for_current_platform()
    resolved_key = artifact_key or local_install.llama_server_artifact_key()
    local_install._write_vulkan_manifest(
        artifact_key=resolved_key,
        pin=resolved_pin,
        attempt_status=None,
        fingerprint=local_install.target_fingerprint(LOCAL_MODEL),
    )


def _write_ready_model_manifest(model_id: str = LOCAL_MODEL) -> None:
    local_install._write_model_manifest(
        model_id=model_id,
        attempt_status=None,
        fingerprint=local_install.target_fingerprint(model_id),
    )


def _fit(severity: fit_report.FitSeverity) -> fit_report.FitReport:
    return fit_report.FitReport(
        artifact="test local",
        checks=(fit_report.FitCheck("test", severity, f"{severity} detail"),),
    )


def _covered_nvidia_probe(
    *,
    compute_cap: str = "sm_121",
    driver_cuda_version: int = 13,
    vram_mib: int = 24564,
) -> local_cuda.NvidiaProbe:
    return local_cuda.NvidiaProbe(
        index=0,
        compute_cap=compute_cap,
        driver_cuda_version=driver_cuda_version,
        vram_mib=vram_mib,
        tiering_memory_mib=vram_mib,
        memory_source=local_cuda.MEMORY_SOURCE_NVIDIA_VRAM,
        detected=True,
    )


def _patch_backend_inputs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    compute_cap: str,
    driver_cuda_version: int,
    trust: local_cuda.ArtifactTrust,
    persisted_installed_cuda: bool = False,
) -> None:
    monkeypatch.setattr(
        local_cuda,
        "probe_nvidia_gpu",
        lambda: _covered_nvidia_probe(
            compute_cap=compute_cap,
            driver_cuda_version=driver_cuda_version,
        ),
    )
    monkeypatch.setattr(
        local_install,
        "probe_cuda_runtime_artifact_trust",
        lambda _pin, **_kwargs: trust,
    )
    monkeypatch.setattr(
        local_install,
        "has_persisted_installed_cuda_target",
        lambda **_kwargs: persisted_installed_cuda,
    )


def _force_cuda_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_backend_inputs(
        monkeypatch,
        compute_cap="sm_121",
        driver_cuda_version=13,
        trust=local_cuda.ArtifactTrust.TRUSTED,
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("ready", local_cuda.ArtifactTrust.TRUSTED),
        ("missing-or-mismatched", local_cuda.ArtifactTrust.ABSENT),
        ("proof-unavailable", local_cuda.ArtifactTrust.UNAVAILABLE),
    ],
)
@pytest.mark.real_local_backend_probe
def test_probe_cuda_runtime_artifact_trust_maps_proof_status(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected: local_cuda.ArtifactTrust,
) -> None:
    monkeypatch.setattr(
        local_install,
        "cuda_artifact_pin_for_current_platform",
        lambda _pin=None: None,
    )
    monkeypatch.setattr(
        local_install,
        "_prove_cuda_runtime_artifact",
        lambda _pin, **_kwargs: ProofResult(status, "test"),
    )

    assert (
        local_install.probe_cuda_runtime_artifact_trust(local_install.CUDA_SERVER_PIN)
        == expected
    )


@pytest.mark.real_local_backend_probe
def test_probe_cuda_runtime_artifact_trust_uses_present_pin_without_local_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_proof(_pin, **_kwargs):
        raise AssertionError("present platform pin should short-circuit proof")

    monkeypatch.setattr(local_install, "_prove_cuda_runtime_artifact", fail_proof)

    assert (
        local_install.probe_cuda_runtime_artifact_trust(local_install.CUDA_SERVER_PIN)
        == local_cuda.ArtifactTrust.TRUSTED
    )


@pytest.mark.real_local_backend_probe
def test_probe_cuda_runtime_artifact_trust_contains_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_proof(_pin, **_kwargs):
        raise ValueError("required artifact is not a regular file")

    monkeypatch.setattr(
        local_install,
        "cuda_artifact_pin_for_current_platform",
        lambda _pin=None: None,
    )
    monkeypatch.setattr(local_install, "_prove_cuda_runtime_artifact", fail_proof)

    trust = local_install.probe_cuda_runtime_artifact_trust(
        local_install.CUDA_SERVER_PIN
    )

    assert trust == local_cuda.ArtifactTrust.UNAVAILABLE
    assert "trust probe failed" in caplog.text


@pytest.mark.real_local_backend_probe
def test_probe_cuda_runtime_artifact_trust_absent_without_platform_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(local_install.platform, "machine", lambda: "x86_64")
    pin = replace(local_install.CUDA_SERVER_PIN, artifacts_by_key={})

    assert (
        local_install.probe_cuda_runtime_artifact_trust(pin, journal_path=tmp_path)
        == local_cuda.ArtifactTrust.ABSENT
    )


@pytest.mark.real_local_backend_probe
def test_has_persisted_installed_cuda_target_reads_installed_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_journal(tmp_path, monkeypatch)
    cuda_target = {
        "provider": "local",
        "runtime": "llama.cpp",
        "backend": "cuda",
        "model_pin": {"model_id": LOCAL_MODEL},
    }
    vulkan_target = {**cuda_target, "backend": "vulkan"}

    status = begin_or_replace_install_attempt("local", cuda_target)
    write_install_status(transition_state(status, new_state="installed"))
    assert (
        local_install.has_persisted_installed_cuda_target(journal_path=tmp_path) is True
    )

    status = begin_or_replace_install_attempt("local", vulkan_target)
    write_install_status(transition_state(status, new_state="installed"))
    assert (
        local_install.has_persisted_installed_cuda_target(journal_path=tmp_path)
        is False
    )


@pytest.mark.real_local_backend_probe
def test_has_persisted_installed_cuda_target_treats_bad_status_as_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_journal(tmp_path, monkeypatch)
    path = tmp_path / "health" / "providers" / "local.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")

    assert (
        local_install.has_persisted_installed_cuda_target(journal_path=tmp_path)
        is False
    )


def test_target_fingerprint_uses_cuda_when_platform_pin_present_on_covered_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_journal(tmp_path, monkeypatch)
    _patch_backend_inputs(
        monkeypatch,
        compute_cap="sm_86",
        driver_cuda_version=14,
        trust=local_cuda.ArtifactTrust.TRUSTED,
    )

    fingerprint = local_install.target_fingerprint(LOCAL_MODEL)

    assert fingerprint["backend"] == "cuda"
    assert (
        fingerprint["backend_reason"]
        == "compute_cap sm_86 covered; driver CUDA 14 >= 13"
    )


def test_target_fingerprint_holds_cuda_when_trust_unavailable_and_cuda_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_journal(tmp_path, monkeypatch)
    _patch_backend_inputs(
        monkeypatch,
        compute_cap="sm_89",
        driver_cuda_version=15,
        trust=local_cuda.ArtifactTrust.UNAVAILABLE,
        persisted_installed_cuda=True,
    )

    fingerprint = local_install.target_fingerprint(LOCAL_MODEL)

    assert fingerprint["backend"] == "cuda"
    assert fingerprint["backend_reason"] == (
        "compute_cap sm_89 covered; driver CUDA 15 >= 13"
    )


def test_target_fingerprint_uses_cuda_when_runtime_pin_is_trusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_journal(tmp_path, monkeypatch)
    _patch_backend_inputs(
        monkeypatch,
        compute_cap="sm_121",
        driver_cuda_version=16,
        trust=local_cuda.ArtifactTrust.TRUSTED,
        persisted_installed_cuda=False,
    )

    fingerprint = local_install.target_fingerprint(LOCAL_MODEL)

    assert fingerprint["backend"] == "cuda"
    assert fingerprint["backend_reason"] == (
        "compute_cap sm_121 covered; driver CUDA 16 >= 13"
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
    total = sum(len(chunk) for chunk in chunks)
    calls: list[tuple[int, int | None]] = []

    def fake_stream(method, url, **_kwargs):
        assert method == "GET"
        assert url == "https://example.test/artifact"
        return _FakeStream(chunks, chunk_times, total, [0.0])

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


def test_download_file_reports_each_progress_chunk(tmp_path, monkeypatch):
    chunks = [b"x"] * 20
    chunk_times = [index * 0.01 for index in range(len(chunks))]

    _dest, calls = _download_with_fake_stream(
        tmp_path, monkeypatch, chunks, chunk_times
    )

    total = sum(len(chunk) for chunk in chunks)
    assert calls == [(index, total) for index in range(1, len(chunks) + 1)]


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
    expected = []
    received = 0
    for chunk in chunks:
        received += len(chunk)
        expected.append((received, total))
    assert calls == expected


def test_download_file_emits_each_final_chunk_once(tmp_path, monkeypatch):
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
def test_cuda_runtime_arch_mapping(
    machine: str, arch: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(local_install.platform, "machine", lambda: machine)

    assert local_install._cuda_runtime_arch() == arch


def test_cuda_runtime_arch_unsupported_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(local_install.platform, "machine", lambda: "riscv64")

    with pytest.raises(local_install.LocalProviderError) as exc_info:
        local_install._cuda_runtime_arch()

    assert exc_info.value.reason_code == "unsupported_platform"


def test_cuda_binary_paths_include_tarball_sha256(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    artifact_pin = local_install.require_cuda_artifact_pin_for_current_platform()

    assert local_install.cuda_binary_dir() == (
        tmp_path
        / "cache"
        / "providers"
        / "local"
        / "cuda"
        / local_install.llama_server_artifact_key()
        / artifact_pin.sha256
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


def test_cuda_server_artifact_pins_are_complete_immutable_artifacts() -> None:
    expected = {
        "x86_64-unknown-linux-gnu": local_install.CudaArtifactPin(
            url=(
                "https://updates.solstone.app/runtimes/llama-cuda13/b10068/"
                "llama-b10068-bin-linux-cuda13-amd64-sol1.tar.gz"
            ),
            sha256="3727630e6ac79953f5c652fddcfd7100da98c55d773c0aec115a55f40f3aafea",
            size_bytes=550238443,
            release_tag="b10068",
            upstream_image_digest=(
                "sha256:"
                "5bd5290bd35cfde893d0dcbd9811723c16d89575927d537b5f21becbfbab2f63"
            ),
            llama_cpp_revision="571d0d540df04f25298d0e159e520d9fc62ed121",
            repack_revision="sol1",
        ),
        "aarch64-unknown-linux-gnu": local_install.CudaArtifactPin(
            url=(
                "https://updates.solstone.app/runtimes/llama-cuda13/b10068/"
                "llama-b10068-bin-linux-cuda13-arm64-sol1.tar.gz"
            ),
            sha256="6de68319db40e8c0eb45dc4bd3a45a16971dbdc128f2b621b19bef5dae87d064",
            size_bytes=654508507,
            release_tag="b10068",
            upstream_image_digest=(
                "sha256:"
                "5bd5290bd35cfde893d0dcbd9811723c16d89575927d537b5f21becbfbab2f63"
            ),
            llama_cpp_revision="571d0d540df04f25298d0e159e520d9fc62ed121",
            repack_revision="sol1",
        ),
    }

    assert local_install.CUDA_SERVER_PIN.artifacts_by_key == expected
    for key, artifact_pin in local_install.CUDA_SERVER_PIN.artifacts_by_key.items():
        assert artifact_pin.url.startswith("https://updates.solstone.app/runtimes/")
        assert len(artifact_pin.sha256) == 64
        assert set(artifact_pin.sha256) <= set("0123456789abcdef")
        assert artifact_pin.size_bytes > 0
        assert (
            artifact_pin.release_tag
            == local_install.LLAMA_SERVER_PINS[key]["release_tag"]
        )


def _write_cuda_runtime_tarball(
    tmp_path: Path,
    *,
    arch: str = "amd64",
    missing: tuple[str, ...] = (),
    traversal: bool = False,
) -> Path:
    source = tmp_path / "cuda-source"
    source.mkdir()
    missing_set = set(missing)
    for name in local_install.CUDA_SERVER_PIN.wanted_files_for_arch(arch):
        if name in missing_set:
            continue
        path = source / name
        path.write_text(name, encoding="utf-8")
    if "licenses/" not in missing_set:
        licenses = source / "licenses"
        licenses.mkdir()
        (licenses / "LICENSE").write_text("license", encoding="utf-8")
    if "provenance.json" not in missing_set:
        (source / "provenance.json").write_text("{}\n", encoding="utf-8")

    tarball = tmp_path / "cuda-runtime.tar.gz"
    with tarfile.open(tarball, "w:gz") as archive:
        for path in sorted(source.rglob("*")):
            archive.add(path, arcname=path.relative_to(source).as_posix())
        if traversal:
            escape = tmp_path / "escape-source"
            escape.write_text("bad", encoding="utf-8")
            archive.add(escape, arcname="../escape")
    return tarball


def _patch_tarball_download(
    monkeypatch: pytest.MonkeyPatch,
    tarball: Path,
) -> None:
    def fake_download(_url: str, dest: Path, **kwargs: object) -> None:
        shutil.copyfile(tarball, dest)
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            on_progress(tarball.stat().st_size, tarball.stat().st_size)

    monkeypatch.setattr(local_install, "_download_file", fake_download)


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
        assert not (install_dir / pin["filename"]).exists()

    result = local_install.install_llama_server()

    assert result["install_state"] == "verifying"
    assert prove_manifest(
        artifact_manifest_path(install_dir),
        provider="local",
        pin_identity=local_install._vulkan_pin_identity(artifact_key, pin),
    ).ready
    assert_flat_layout()
    assert len(quarantine_calls) == 1
    assert quarantine_calls[0].parent == install_dir.parent

    result = local_install.install_llama_server()

    assert result["install_state"] == "verifying"
    assert_flat_layout()
    assert len(quarantine_calls) == 2
    assert all(path.parent == install_dir.parent for path in quarantine_calls)


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
    assert status["error_code"] == "sha256_mismatch"
    assert "sha256 mismatch" in status["install_error"]
    assert not artifact_manifest_path(install_dir).exists()
    assert not binary_path.exists()
    assert not (install_dir / inner_name).exists()
    assert not install_dir.exists()


def test_install_llama_server_extract_failure_preserves_prior_tree(
    tmp_path, monkeypatch
):
    _init_journal(tmp_path, monkeypatch)
    artifact_key = local_install.llama_server_artifact_key()
    pin = local_install.pin_for_current_platform()
    install_dir = local_install.binary_install_dir(artifact_key, pin)
    binary = local_install.binary_path_for_pin(artifact_key, pin)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"old binary")
    binary.chmod(0o755)
    _write_ready_vulkan_manifest(artifact_key=artifact_key, pin=pin)
    old_manifest = artifact_manifest_path(install_dir).read_text(encoding="utf-8")

    def fake_download(_url, dest, **_kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"archive")

    monkeypatch.setattr(local_install, "_download_file", fake_download)
    monkeypatch.setattr(local_install, "_verify_sha256", lambda _path, _expected: None)
    monkeypatch.setattr(
        local_install,
        "_safe_extract_tarball",
        lambda _tarball, _dest: (_ for _ in ()).throw(RuntimeError("extract broke")),
    )

    with pytest.raises(RuntimeError, match="extract broke"):
        local_install.install_llama_server()

    assert binary.read_bytes() == b"old binary"
    assert (
        artifact_manifest_path(install_dir).read_text(encoding="utf-8") == old_manifest
    )


def test_install_llama_server_manifest_failure_preserves_prior_tree(
    tmp_path, monkeypatch
):
    _init_journal(tmp_path, monkeypatch)
    artifact_key = local_install.llama_server_artifact_key()
    pin = local_install.pin_for_current_platform()
    install_dir = local_install.binary_install_dir(artifact_key, pin)
    binary = local_install.binary_path_for_pin(artifact_key, pin)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"old binary")
    binary.chmod(0o755)
    _write_ready_vulkan_manifest(artifact_key=artifact_key, pin=pin)
    old_manifest = artifact_manifest_path(install_dir).read_text(encoding="utf-8")
    fixture_root = tmp_path / "fixture" / "inner"
    fixture_root.mkdir(parents=True)
    (fixture_root / pin["binary_name"]).write_bytes(b"new binary")
    fixture_tarball = tmp_path / pin["filename"]
    with tarfile.open(fixture_tarball, "w:gz") as archive:
        archive.add(fixture_root, arcname="inner")

    def fake_download(_url, dest, **_kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture_tarball, dest)

    monkeypatch.setattr(local_install, "_download_file", fake_download)
    monkeypatch.setattr(local_install, "_verify_sha256", lambda _path, _expected: None)
    monkeypatch.setattr(
        local_install,
        "_write_vulkan_manifest",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("manifest broke")),
    )

    with pytest.raises(RuntimeError, match="manifest broke"):
        local_install.install_llama_server()

    assert binary.read_bytes() == b"old binary"
    assert (
        artifact_manifest_path(install_dir).read_text(encoding="utf-8") == old_manifest
    )


def test_install_llama_server_publish_failure_preserves_prior_tree(
    tmp_path, monkeypatch
):
    _init_journal(tmp_path, monkeypatch)
    artifact_key = local_install.llama_server_artifact_key()
    pin = local_install.pin_for_current_platform()
    install_dir = local_install.binary_install_dir(artifact_key, pin)
    binary = local_install.binary_path_for_pin(artifact_key, pin)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"old binary")
    binary.chmod(0o755)
    _write_ready_vulkan_manifest(artifact_key=artifact_key, pin=pin)
    old_manifest = artifact_manifest_path(install_dir).read_text(encoding="utf-8")
    fixture_root = tmp_path / "fixture" / "inner"
    fixture_root.mkdir(parents=True)
    (fixture_root / pin["binary_name"]).write_bytes(b"new binary")
    fixture_tarball = tmp_path / pin["filename"]
    with tarfile.open(fixture_tarball, "w:gz") as archive:
        archive.add(fixture_root, arcname="inner")

    def fake_download(_url, dest, **_kwargs):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture_tarball, dest)

    monkeypatch.setattr(local_install, "_download_file", fake_download)
    monkeypatch.setattr(local_install, "_verify_sha256", lambda _path, _expected: None)
    monkeypatch.setattr(
        local_install,
        "publish_staged_tree",
        lambda _staging, _install_dir: (_ for _ in ()).throw(
            RuntimeError("publish broke")
        ),
    )

    with pytest.raises(RuntimeError, match="publish broke"):
        local_install.install_llama_server()

    assert binary.read_bytes() == b"old binary"
    assert (
        artifact_manifest_path(install_dir).read_text(encoding="utf-8") == old_manifest
    )


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
    observed: list[tuple[str, str]] = []

    monkeypatch.setattr(
        local_install, "llama_server_artifact_key", lambda: "test-platform"
    )
    monkeypatch.setattr(local_install, "pin_for_current_platform", lambda: pin)

    def fake_download(_url, _dest, **_kwargs):
        observed.append(("download", _local_status()["install_state"]))
        _dest.parent.mkdir(parents=True, exist_ok=True)
        _dest.write_bytes(b"artifact")
        _dest.parent.mkdir(parents=True, exist_ok=True)
        _dest.write_bytes(b"artifact")

    def fake_verify(_path, _expected):
        observed.append(("verify", _local_status()["install_state"]))

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
    assert observed[1][1] == "verifying"
    assert result["install_state"] == "verifying"
    assert prove_manifest(
        artifact_manifest_path(final_path.parent),
        provider=local_install.LOCAL_PROVIDER_NAME,
        pin_identity=local_install._vulkan_pin_identity("test-platform", pin),
    ).ready


@pytest.mark.parametrize(
    ("machine", "arch", "expected_cpu", "unexpected_cpu"),
    [
        ("x86_64", "amd64", "libggml-cpu-haswell.so", "libggml-cpu-armv8.0_1.so"),
        ("arm64", "arm64", "libggml-cpu-armv8.0_1.so", "libggml-cpu-haswell.so"),
    ],
)
def test_install_llama_server_cuda_extracts_flat_tarball_and_writes_manifest(
    tmp_path,
    monkeypatch,
    machine: str,
    arch: str,
    expected_cpu: str,
    unexpected_cpu: str,
):
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(local_install.platform, "machine", lambda: machine)
    _force_cuda_backend(monkeypatch)
    tarball = _write_cuda_runtime_tarball(tmp_path, arch=arch)
    _patch_tarball_download(monkeypatch, tarball)
    monkeypatch.setattr(local_install, "_verify_sha256", lambda _path, _expected: None)

    result = local_install.install_llama_server()

    wanted_files = local_install.CUDA_SERVER_PIN.wanted_files_for_arch(arch)
    assert result["install_state"] == "verifying"
    assert expected_cpu in wanted_files
    assert unexpected_cpu not in wanted_files
    for name in wanted_files:
        assert (local_install.cuda_binary_dir() / name).is_file()
    assert (local_install.cuda_binary_dir() / "licenses" / "LICENSE").is_file()
    assert (local_install.cuda_binary_dir() / "provenance.json").is_file()
    assert not (local_install.cuda_binary_dir() / ".oci-install.json").exists()
    assert local_install.cuda_binary_path().stat().st_mode & 0o111

    artifact_pin = local_install.require_cuda_artifact_pin_for_current_platform()
    proof = prove_manifest(
        artifact_manifest_path(local_install.cuda_binary_dir()),
        provider=local_install.LOCAL_PROVIDER_NAME,
        pin_identity=local_install._cuda_pin_identity(
            arch,
            wanted_files,
            artifact_pin=artifact_pin,
        ),
    )
    assert proof.ready
    manifest = json.loads(
        artifact_manifest_path(local_install.cuda_binary_dir()).read_text(
            encoding="utf-8"
        )
    )
    inventory_paths = {entry["relative_path"] for entry in manifest["inventory"]}
    assert set(wanted_files) <= inventory_paths
    assert {"licenses/LICENSE", "provenance.json"} <= inventory_paths


def test_install_llama_server_cuda_sha256_mismatch_fails_closed(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    _force_cuda_backend(monkeypatch)
    tarball = _write_cuda_runtime_tarball(tmp_path)
    _patch_tarball_download(monkeypatch, tarball)

    with pytest.raises(local_install.LocalProviderError) as exc_info:
        local_install.install_llama_server()

    assert exc_info.value.reason_code == "sha256_mismatch"
    status = _local_status()
    assert status["install_state"] == "failed"
    assert status["error_code"] == "sha256_mismatch"
    assert not local_install.cuda_binary_dir().exists()


@pytest.mark.parametrize(
    ("missing", "expected_detail"),
    [
        (("libllama.so.0",), "libllama.so.0"),
        (("licenses/",), "licenses/"),
    ],
)
def test_install_llama_server_cuda_required_files_fail_closed(
    tmp_path,
    monkeypatch,
    missing: tuple[str, ...],
    expected_detail: str,
):
    _init_journal(tmp_path, monkeypatch)
    _force_cuda_backend(monkeypatch)
    tarball = _write_cuda_runtime_tarball(tmp_path, missing=missing)
    _patch_tarball_download(monkeypatch, tarball)
    monkeypatch.setattr(local_install, "_verify_sha256", lambda _path, _expected: None)

    with pytest.raises(local_install.LocalProviderError) as exc_info:
        local_install.install_llama_server()

    assert exc_info.value.reason_code == "cuda_runtime_incomplete"
    assert expected_detail in str(exc_info.value)
    assert not local_install.cuda_binary_dir().exists()


def test_install_llama_server_cuda_rejects_traversal_member(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    _force_cuda_backend(monkeypatch)
    tarball = _write_cuda_runtime_tarball(tmp_path, traversal=True)
    _patch_tarball_download(monkeypatch, tarball)
    monkeypatch.setattr(local_install, "_verify_sha256", lambda _path, _expected: None)

    with pytest.raises(local_install.LocalProviderError) as exc_info:
        local_install.install_llama_server()

    assert exc_info.value.reason_code == "archive_path_traversal"
    assert not (tmp_path / "escape").exists()
    assert not local_install.cuda_binary_dir().exists()


def test_install_llama_server_cuda_removes_legacy_oci_tree_after_publish_only(
    tmp_path,
    monkeypatch,
):
    _init_journal(tmp_path, monkeypatch)
    _force_cuda_backend(monkeypatch)
    artifact_key = local_install.llama_server_artifact_key()
    legacy_digest = "a" * 64
    legacy_dir = (
        tmp_path
        / "cache"
        / "providers"
        / "local"
        / "cuda"
        / artifact_key
        / legacy_digest
    )
    legacy_dir.mkdir(parents=True)
    (legacy_dir / ".oci-install.json").write_text(
        json.dumps(
            {
                "image_ref": f"ghcr.io/acme/runtime@sha256:{legacy_digest}",
                "arch": "amd64",
                "files": {"llama-server": "b" * 64},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (legacy_dir / "llama-server").write_text("legacy", encoding="utf-8")
    vulkan_dir = local_install.binary_install_dir()
    vulkan_dir.mkdir(parents=True)
    (vulkan_dir / "llama-server").write_text("vulkan", encoding="utf-8")
    tarball = _write_cuda_runtime_tarball(tmp_path)
    _patch_tarball_download(monkeypatch, tarball)
    monkeypatch.setattr(local_install, "_verify_sha256", lambda _path, _expected: None)

    local_install.install_llama_server()

    assert not legacy_dir.exists()
    assert (vulkan_dir / "llama-server").read_text(encoding="utf-8") == "vulkan"
    assert local_install.cuda_binary_path().is_file()


def test_install_llama_server_cuda_failure_leaves_legacy_oci_tree(
    tmp_path,
    monkeypatch,
):
    _init_journal(tmp_path, monkeypatch)
    _force_cuda_backend(monkeypatch)
    artifact_key = local_install.llama_server_artifact_key()
    legacy_digest = "a" * 64
    legacy_dir = (
        tmp_path
        / "cache"
        / "providers"
        / "local"
        / "cuda"
        / artifact_key
        / legacy_digest
    )
    legacy_dir.mkdir(parents=True)
    (legacy_dir / ".oci-install.json").write_text(
        json.dumps(
            {
                "image_ref": f"ghcr.io/acme/runtime@sha256:{legacy_digest}",
                "arch": "amd64",
                "files": {"llama-server": "b" * 64},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    tarball = _write_cuda_runtime_tarball(tmp_path, missing=("licenses/",))
    _patch_tarball_download(monkeypatch, tarball)
    monkeypatch.setattr(local_install, "_verify_sha256", lambda _path, _expected: None)

    with pytest.raises(local_install.LocalProviderError):
        local_install.install_llama_server()

    assert legacy_dir.exists()


def test_install_llama_server_cuda_preserves_partial_legacy_oci_sidecar(
    tmp_path,
    monkeypatch,
) -> None:
    _init_journal(tmp_path, monkeypatch)
    _force_cuda_backend(monkeypatch)
    artifact_key = local_install.llama_server_artifact_key()
    legacy_digest = "a" * 64
    legacy_dir = (
        tmp_path
        / "cache"
        / "providers"
        / "local"
        / "cuda"
        / artifact_key
        / legacy_digest
    )
    legacy_dir.mkdir(parents=True)
    (legacy_dir / ".oci-install.json").write_text(
        json.dumps(
            {
                "image_ref": f"ghcr.io/acme/runtime@sha256:{legacy_digest}",
                "files": {"llama-server": "b" * 64},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    tarball = _write_cuda_runtime_tarball(tmp_path)
    _patch_tarball_download(monkeypatch, tarball)
    monkeypatch.setattr(local_install, "_verify_sha256", lambda _path, _expected: None)

    local_install.install_llama_server()

    assert legacy_dir.exists()
    assert local_install.cuda_binary_path().is_file()


def test_install_llama_server_cuda_cleanup_failure_does_not_fail_published_install(
    tmp_path,
    monkeypatch,
) -> None:
    _init_journal(tmp_path, monkeypatch)
    _force_cuda_backend(monkeypatch)
    tarball = _write_cuda_runtime_tarball(tmp_path)
    _patch_tarball_download(monkeypatch, tarball)
    monkeypatch.setattr(local_install, "_verify_sha256", lambda _path, _expected: None)
    real_publish = local_install.publish_staged_tree
    real_resolve = Path.resolve

    def publish_and_break_cleanup(staging: Path, target: Path) -> None:
        real_publish(staging, target)

        def fail_target_resolve(path: Path, *args: object, **kwargs: object) -> Path:
            if path == target:
                raise OSError("resolve failed")
            return real_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", fail_target_resolve)

    monkeypatch.setattr(local_install, "publish_staged_tree", publish_and_break_cleanup)

    result = local_install.install_llama_server()

    assert result["install_state"] == "verifying"
    assert _local_status()["install_state"] == "verifying"
    assert local_install.cuda_binary_path().is_file()


@pytest.mark.parametrize("flow", ["llama_server", "model", "install_local"])
def test_local_install_owner_paths_have_no_oci_registry_or_cosign_entrypoint(
    tmp_path,
    monkeypatch,
    flow: str,
) -> None:
    source = Path(local_install.__file__).read_text(encoding="utf-8")
    assert "solstone.think.providers import oci_image" not in source
    assert "pull_and_install" not in source
    assert "verify_image_signature" not in source
    assert "cosign" not in source

    def reject_oci_registry_url(url: object) -> None:
        parsed = urlparse(str(url))
        if (
            parsed.hostname == "ghcr.io"
            or parsed.path.startswith("/v2/")
            or "scope=repository:" in parsed.query
        ):
            raise AssertionError(f"OCI registry access must not run: {url}")

    def fail_cosign(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        if cmd and cmd[0] == "cosign":
            raise AssertionError("cosign must not run in the owner install path")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    class RegistryTrapClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> RegistryTrapClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, url: object, *_args: object, **_kwargs: object) -> object:
            reject_oci_registry_url(url)
            raise AssertionError(f"unexpected httpx.Client.get during install: {url}")

        def stream(
            self, _method: str, url: object, *_args: object, **_kwargs: object
        ) -> object:
            reject_oci_registry_url(url)
            raise AssertionError(
                f"unexpected httpx.Client.stream during install: {url}"
            )

    def fail_httpx_stream(
        _method: str, url: object, *_args: object, **_kwargs: object
    ) -> object:
        reject_oci_registry_url(url)
        raise AssertionError(f"unexpected live httpx.stream during install: {url}")

    _init_journal(tmp_path, monkeypatch)
    _force_cuda_backend(monkeypatch)
    tarball = _write_cuda_runtime_tarball(tmp_path)

    def fake_download(url: str, dest: Path, **kwargs: object) -> None:
        reject_oci_registry_url(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.name.endswith(".tar.gz"):
            shutil.copyfile(tarball, dest)
        else:
            dest.write_text(dest.name, encoding="utf-8")
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            on_progress(dest.stat().st_size, dest.stat().st_size)

    monkeypatch.setattr(local_install, "_download_file", fake_download)
    monkeypatch.setattr(local_install, "_verify_sha256", lambda _path, _expected: None)
    monkeypatch.setattr(subprocess, "run", fail_cosign)
    monkeypatch.setattr("httpx.Client", RegistryTrapClient)
    monkeypatch.setattr("httpx.stream", fail_httpx_stream)
    monkeypatch.setattr(
        fit_report, "build_local_fit_report", lambda _model_id: _fit("ok")
    )

    if flow == "llama_server":
        local_install.install_llama_server()
    elif flow == "model":
        local_install.install_model(LOCAL_MODEL)
    else:
        readiness_calls = 0

        def fake_readiness(model_id: str) -> ReadinessOutcome:
            nonlocal readiness_calls
            readiness_calls += 1
            return _fake_local_readiness(
                binary_installed=readiness_calls > 1,
                model_installed=readiness_calls > 1,
                binary_path=local_install.cuda_binary_path(),
                model_path=local_install.model_path(model_id),
                mmproj_path=local_install.mmproj_path(model_id),
                backend="cuda",
                backend_reason="test cuda",
            )

        monkeypatch.setattr(local_install, "inspect_readiness", fake_readiness)
        local_install.install_local(LOCAL_MODEL)


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
    observed: list[tuple[str, str]] = []

    def fake_download(_url, _dest, **_kwargs):
        observed.append(("download", _local_status()["install_state"]))
        _dest.parent.mkdir(parents=True, exist_ok=True)
        _dest.write_bytes(b"artifact")

    def fake_verify(_path, _expected):
        observed.append(("verify", _local_status()["install_state"]))

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
    assert observed[2][1] == "verifying"
    assert result["install_state"] == "verifying"
    assert prove_manifest(
        artifact_manifest_path(local_install.model_dir(spec.model_id)),
        provider=local_install.LOCAL_PROVIDER_NAME,
        pin_identity=local_install._model_pin_identity(spec.model_id),
    ).ready


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
    assert prove_manifest(
        artifact_manifest_path(local_install.model_dir(LOCAL_MODEL)),
        provider=local_install.LOCAL_PROVIDER_NAME,
        pin_identity=local_install._model_pin_identity(LOCAL_MODEL),
    ).ready


def test_install_local_blocks_before_downloads(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda model_id: _fake_local_readiness(
            binary_installed=False,
            model_installed=False,
            binary_path=tmp_path / "llama-server",
            model_path=tmp_path / "model.gguf",
        ),
    )
    monkeypatch.setattr(
        fit_report, "build_local_fit_report", lambda model_id: _fit("blocked")
    )
    monkeypatch.setattr(
        local_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("download should not start"),
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
    readiness_calls = 0

    def fake_readiness(model_id: str) -> ReadinessOutcome:
        nonlocal readiness_calls
        readiness_calls += 1
        return _fake_local_readiness(
            binary_installed=readiness_calls > 1,
            model_installed=readiness_calls > 1,
            binary_path=final_path,
            model_path=local_install.model_path(model_id),
            mmproj_path=local_install.mmproj_path(model_id),
        )

    monkeypatch.setattr(local_install, "inspect_readiness", fake_readiness)
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

    assert local_install.install_local(LOCAL_MODEL)["install_state"] == "installed"

    assert downloads


def test_install_local_ready_short_circuits_before_fit_report(tmp_path, monkeypatch):
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(
        local_install,
        "inspect_readiness",
        lambda model_id: _fake_local_readiness(
            binary_installed=True,
            model_installed=True,
            binary_path=tmp_path / "llama-server",
            model_path=tmp_path / "model.gguf",
        ),
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

    result = local_install.install_local(LOCAL_MODEL)

    assert result["provider"] == local_install.LOCAL_PROVIDER_NAME
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
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])
    monkeypatch.setattr(
        fit_report, "build_local_fit_report", lambda model_id: _fit("ok")
    )
    calls: list[str] = []
    readiness_calls = 0

    def fake_readiness(model_id: str) -> ReadinessOutcome:
        nonlocal readiness_calls
        readiness_calls += 1
        return _fake_local_readiness(
            binary_installed=readiness_calls > 1,
            model_installed=readiness_calls > 1,
            binary_path=local_install.binary_path_for_pin(),
            model_path=local_install.model_path(model_id),
            mmproj_path=local_install.mmproj_path(model_id),
        )

    def fake_install_llama_server(**_kwargs):
        calls.append("llama_server")
        return {"install_state": "verifying"}

    def fake_install_model(model_id: str, **_kwargs):
        calls.append("model")
        return {"install_state": "verifying", "model_id": model_id}

    monkeypatch.setattr(local_install, "inspect_readiness", fake_readiness)
    monkeypatch.setattr(
        local_install, "install_llama_server", fake_install_llama_server
    )
    monkeypatch.setattr(local_install, "install_model", fake_install_model)

    result = local_install.install_local(LOCAL_MODEL)

    assert result["install_state"] == "installed"
    assert calls == ["llama_server", "model"]


def test_install_local_replaces_failed_cuda_attempt_with_current_cuda_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_journal(tmp_path, monkeypatch)
    _patch_backend_inputs(
        monkeypatch,
        compute_cap="sm_89",
        driver_cuda_version=15,
        trust=local_cuda.ArtifactTrust.TRUSTED,
    )
    stale_cuda = {
        "provider": "local",
        "runtime": "llama.cpp",
        "backend": "cuda",
        "backend_reason": "old cuda",
        "runtime_pin": local_install._cuda_pin_identity(),
        "model_pin": local_install._model_pin_identity(LOCAL_MODEL),
    }
    stale_status = begin_or_replace_install_attempt(
        "local",
        stale_cuda,
        initial_state="resolving",
    )
    write_install_status(
        transition_state(
            stale_status,
            new_state="failed",
            error="the pinned image has no matching signature",
            error_code="signature_verify_failed",
        )
    )
    stale_sha = fingerprint_sha256(canonical_fingerprint(stale_cuda))
    monkeypatch.setattr(
        fit_report,
        "build_local_fit_report",
        lambda model_id: _fit("ok"),
    )
    calls: list[str] = []
    readiness_calls = 0

    def fake_readiness(model_id: str) -> ReadinessOutcome:
        nonlocal readiness_calls
        readiness_calls += 1
        return _fake_local_readiness(
            binary_installed=readiness_calls > 1,
            model_installed=readiness_calls > 1,
            binary_path=local_install.binary_path_for_pin(),
            model_path=local_install.model_path(model_id),
            mmproj_path=local_install.mmproj_path(model_id),
        )

    def fake_install_llama_server(**_kwargs):
        calls.append("llama_server")
        return {"install_state": "verifying"}

    def fake_install_model(model_id: str, **_kwargs):
        calls.append("model")
        return {"install_state": "verifying", "model_id": model_id}

    monkeypatch.setattr(local_install, "inspect_readiness", fake_readiness)
    monkeypatch.setattr(
        local_install,
        "install_llama_server",
        fake_install_llama_server,
    )
    monkeypatch.setattr(local_install, "install_model", fake_install_model)

    result = local_install.install_local(LOCAL_MODEL)

    status = _local_status()
    target = json.loads(str(status["target_fingerprint_json"]))
    assert result["install_state"] == "installed"
    assert status["target_fingerprint_sha256"] != stale_sha
    assert target["backend"] == "cuda"
    assert target["backend_reason"] == "compute_cap sm_89 covered; driver CUDA 15 >= 13"
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
        lambda model_id: _fake_local_readiness(
            binary_installed=True,
            model_installed=True,
            binary_path=binary,
            model_path=gguf,
            mmproj_path=mmproj,
            backend="vulkan",
            backend_reason="test vulkan",
        ),
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
        lambda model_id: _fake_local_readiness(
            binary_installed=True,
            model_installed=True,
            binary_path=binary,
            model_path=gguf,
            ram_sufficient=False,
            backend="vulkan",
            backend_reason="test vulkan",
        ),
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
        lambda model_id: _fake_local_readiness(
            binary_installed=True,
            model_installed=True,
            binary_path=binary,
            model_path=gguf,
            backend="cuda",
            backend_reason="test cuda",
        ),
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
        lambda model_id: _fake_local_readiness(
            binary_installed=binary_installed,
            model_installed=model_installed,
            binary_path=binary,
            model_path=gguf,
        ),
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

    assert readiness.host["ram_sufficient"] is True


@pytest.mark.parametrize("manifest_ok", [True, False])
def test_inspect_readiness_cuda_uses_manifest_full_set(
    tmp_path,
    monkeypatch,
    manifest_ok,
):
    _init_journal(tmp_path, monkeypatch)
    _force_cuda_backend(monkeypatch)
    binary = local_install.cuda_binary_path()
    binary.parent.mkdir(parents=True, exist_ok=True)
    arch = local_install._cuda_runtime_arch()
    wanted_files = local_install.CUDA_SERVER_PIN.wanted_files_for_arch(arch)
    for name in wanted_files:
        member = binary.parent / name
        member.write_text(name, encoding="utf-8")
        member.chmod(0o755)
    licenses = binary.parent / "licenses"
    licenses.mkdir()
    (licenses / "LICENSE").write_text("license", encoding="utf-8")
    (binary.parent / "provenance.json").write_text("{}\n", encoding="utf-8")
    artifact_pin = local_install.require_cuda_artifact_pin_for_current_platform()
    local_install._write_cuda_manifest(
        artifact_key=local_install.llama_server_artifact_key(),
        artifact_pin=artifact_pin,
        arch=arch,
        wanted_files=wanted_files,
        attempt_status=None,
        fingerprint=local_install.target_fingerprint(LOCAL_MODEL),
        root=binary.parent,
    )
    if not manifest_ok:
        (binary.parent / wanted_files[-1]).unlink()
    monkeypatch.setattr(
        local_vulkan,
        "detect_gpus",
        lambda: (_ for _ in ()).throw(
            AssertionError("Vulkan probe not expected for CUDA readiness")
        ),
    )

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    assert readiness.host["backend"] == "cuda"
    assert readiness.host["backend_reason"] == (
        "compute_cap sm_121 covered; driver CUDA 13 >= 13"
    )
    assert readiness.artifacts["binary_path"] == str(binary)
    assert readiness.artifacts["binary_installed"] is manifest_ok
    assert readiness.host["gpu_available"] is True
    assert readiness.host["gpu_probe_ok"] is True
    assert readiness.proof["cuda"]["status"] == (
        "ready" if manifest_ok else "missing-or-mismatched"
    )


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

    assert readiness.host["gpu_available"] is True
    assert readiness.host["backend"] == "vulkan"
    assert readiness.host["backend_reason"] == "no NVIDIA GPU detected"


def test_inspect_readiness_reports_gpu_unavailable_without_hardware(
    tmp_path, monkeypatch
):
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    assert readiness.host["gpu_available"] is False


def test_inspect_readiness_stale_non_cuda_binary_record_reports_not_installed(
    tmp_path, monkeypatch
):
    _init_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(local_vulkan, "detect_gpus", lambda: [])
    canonical = local_install.binary_path_for_pin()
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"llama-server")
    canonical.chmod(0o755)

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    assert readiness.artifacts["binary_installed"] is False
    assert readiness.artifacts["binary_path"] == str(canonical)
    assert readiness.proof["binary"]["status"] == "missing-or-mismatched"


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
    _write_ready_vulkan_manifest(pin=pin)

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    assert readiness.artifacts["binary_installed"] is True


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
    _write_provider_local_config(tmp_path, {"vulkan_device_index": "0"})

    assert local_install.gpu_device_override() == 0
    assert resolve_local_endpoint().is_bundled is True
    assert local_install.inspect_readiness(LOCAL_MODEL).host["gpu_available"] is True

    _write_provider_local_config(tmp_path, {"vulkan_device_index": "1"})

    assert local_install.inspect_readiness(LOCAL_MODEL).host["gpu_available"] is False


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

    # Stage the selected model's artifacts in its own directory.
    gguf = local_install.model_path(LOCAL_MODEL)
    gguf.parent.mkdir(parents=True, exist_ok=True)
    gguf.write_text("qwen", encoding="utf-8")
    mmproj = local_install.mmproj_path(LOCAL_MODEL)
    assert mmproj is not None
    mmproj.write_text("mmproj", encoding="utf-8")
    _write_ready_model_manifest(LOCAL_MODEL)

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    assert readiness.artifacts["model_id"] == LOCAL_MODEL
    assert readiness.artifacts["model_path"] == str(gguf)
    assert readiness.artifacts["mmproj_path"] == str(mmproj)
    assert Path(readiness.artifacts["model_path"]).parent == local_install.model_dir(
        LOCAL_MODEL
    )
    assert readiness.artifacts["model_path"] != str(stale_gguf)
    assert readiness.artifacts["model_installed"] is True


def test_inspect_readiness_not_installed_off_stale_record(tmp_path, monkeypatch):
    # With only the prior model's artifacts on disk and the selected model not
    # staged, readiness must report not-installed rather than claiming installed
    # off the stale record's gguf.
    _init_journal(tmp_path, monkeypatch)
    stale_dir = local_install.model_dir("local/old-coder-7b")
    stale_dir.mkdir(parents=True, exist_ok=True)
    stale_gguf = stale_dir / "coder-7b-Q4_K_M.gguf"
    stale_gguf.write_text("stale", encoding="utf-8")

    readiness = local_install.inspect_readiness(LOCAL_MODEL)

    assert readiness.artifacts["model_installed"] is False
    assert readiness.artifacts["gguf_installed"] is False
    assert readiness.artifacts["model_path"] == str(
        local_install.model_path(LOCAL_MODEL)
    )


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
