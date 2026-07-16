# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Install and inspect bundled local provider artifacts.

This module is the sole writer for ``providers.bundled.local`` install state.
It performs no network access at import time.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from solstone.think.journal_config import read_journal_config, write_journal_config
from solstone.think.models import LOCAL_MODEL
from solstone.think.providers.install_state import (
    IN_FLIGHT_STATES,
    InstallStatus,
    bump_progress,
    read_install_status,
    transition_state,
    write_install_status,
)
from solstone.think.providers.local import (
    LOCAL_MODEL_SPECS,
    LocalProviderError,
    normalize_model_id,
)
from solstone.think.providers.local_cuda import (
    CUDA_EMBEDDED_ARCH_SET,
    CUDA_MIN_DRIVER_VERSION,
)
from solstone.think.providers.memory import assess_memory
from solstone.think.providers.oci_image import OciSignaturePolicy
from solstone.think.utils import get_journal

LOG = logging.getLogger(__name__)
LOCAL_PROVIDER_NAME = "local"
_PROBE_TIMEOUT_SECONDS = 10
_PROGRESS_MIN_INTERVAL_SECONDS = 1.0  # rate-limit durable install-progress writes to ~1/sec (download throughput fix)
_LOCAL_METADATA_KEYS = frozenset(
    {
        "binary_artifact",
        "binary_sha256",
        "binary_path",
        "model_id",
        "model_path",
        "model_sha256",
        "mmproj_path",
        "mmproj_sha256",
        "vulkan_device_index",
    }
)


@dataclass(frozen=True)
class CudaServerPin:
    image_ref: str
    cuda_version: int
    embedded_arch_set: frozenset[str]
    binary_name: str
    device_flag_value: str
    visible_devices_env: str
    shared_wanted_files: tuple[str, ...]
    cpu_wanted_files_by_arch: dict[str, tuple[str, ...]]
    signature_policy: OciSignaturePolicy | None = None

    def wanted_files_for_arch(self, arch: str) -> tuple[str, ...]:
        cpu_wanted_files = self.cpu_wanted_files_by_arch.get(arch)
        if cpu_wanted_files is None:
            raise LocalProviderError(
                "unsupported_platform",
                f"No CUDA wanted-files set for OCI architecture {arch}",
            )
        return self.shared_wanted_files + cpu_wanted_files


@dataclass(frozen=True)
class LocalArtifacts:
    backend: str
    backend_reason: str
    binary_path: Path
    lib_dir: Path | None
    gguf_path: Path
    mmproj_path: Path | None


LLAMA_SERVER_PINS: dict[str, dict[str, str]] = {
    "aarch64-apple-darwin": {
        "release_tag": "b9291",
        "filename": "llama-b9291-bin-macos-arm64.tar.gz",
        "sha256": "0e985f87dd71f96a9cb9ebc3ad26f8388030342d000e7e82d4a38d14913373ff",
        "binary_name": "llama-server",
    },
    "x86_64-unknown-linux-gnu": {
        "release_tag": "b9291",
        "filename": "llama-b9291-bin-ubuntu-vulkan-x64.tar.gz",
        "sha256": "7e3bf4202bedc71c2c9fbfbe02d10075b8d596bb963e7ab006663582dc2e92c2",
        "binary_name": "llama-server",
    },
    "aarch64-unknown-linux-gnu": {
        "release_tag": "b9291",
        "filename": "llama-b9291-bin-ubuntu-vulkan-arm64.tar.gz",
        "sha256": "c88f06cc72f746d7cbbd69b705f0788488d8b9fe9051995a5e59b3b8b1e8fe61",
        "binary_name": "llama-server",
    },
}

CUDA_SERVER_PIN = CudaServerPin(
    # TODO(AC10): confirm pinned server-cuda13 digest (build b9853) on the
    # Spark GB10.
    image_ref=(
        "ghcr.io/ggml-org/llama.cpp@sha256:"
        "bc998878c040cf2095b4c5cf3b1cf56df3984053e2a2650e5c4c66a4953e10cb"
    ),
    cuda_version=CUDA_MIN_DRIVER_VERSION,
    embedded_arch_set=CUDA_EMBEDDED_ARCH_SET,
    binary_name="llama-server",
    # TODO(AC10): confirm CUDA device flag + visible-devices env on the CUDA build.
    device_flag_value="CUDA0",
    visible_devices_env="CUDA_VISIBLE_DEVICES",
    shared_wanted_files=(
        "llama-server",
        "libllama-server-impl.so",
        "libllama-common.so.0",
        "libmtmd.so.0",
        "libllama.so.0",
        "libggml.so.0",
        "libggml-base.so.0",
        "libggml-cuda.so",
        "libcudart.so.13",
        "libcublas.so.13",
        "libcublasLt.so.13",
    ),
    # Keep the CPU dispatcher libs explicit per upstream image arch. The CUDA
    # runtime libs share basenames; only the libggml-cpu-* variants differ.
    cpu_wanted_files_by_arch={
        "amd64": (
            "libggml-cpu-x64.so",
            "libggml-cpu-sse42.so",
            "libggml-cpu-sandybridge.so",
            "libggml-cpu-ivybridge.so",
            "libggml-cpu-piledriver.so",
            "libggml-cpu-haswell.so",
            "libggml-cpu-skylakex.so",
            "libggml-cpu-cannonlake.so",
            "libggml-cpu-cascadelake.so",
            "libggml-cpu-icelake.so",
            "libggml-cpu-cooperlake.so",
            "libggml-cpu-zen4.so",
            "libggml-cpu-alderlake.so",
            "libggml-cpu-sapphirerapids.so",
        ),
        "arm64": (
            "libggml-cpu-armv8.0_1.so",
            "libggml-cpu-armv8.2_1.so",
            "libggml-cpu-armv8.2_2.so",
            "libggml-cpu-armv8.2_3.so",
            "libggml-cpu-armv8.6_1.so",
            "libggml-cpu-armv8.6_2.so",
            "libggml-cpu-armv9.2_1.so",
            "libggml-cpu-armv9.2_2.so",
        ),
    },
    # TODO(AC10): narrow this to the exact upstream workflow identity after
    # validating the pinned CUDA image signature on hardware.
    signature_policy=OciSignaturePolicy(
        certificate_identity_regexp=(
            r"^https://github\.com/ggml-org/llama\.cpp/\.github/workflows/"
            r".+@refs/(heads|tags)/.+$"
        ),
        oidc_issuer="https://token.actions.githubusercontent.com",
    ),
)


def llama_server_artifact_key() -> str:
    machine = platform.machine().lower()
    if machine in {"amd64", "x64"}:
        machine = "x86_64"
    elif machine == "arm64":
        machine = "aarch64"

    if sys.platform == "darwin":
        return f"{machine}-apple-darwin"
    if sys.platform.startswith("linux"):
        return f"{machine}-unknown-linux-gnu"
    return f"{machine}-{sys.platform}"


def pin_for_current_platform() -> dict[str, str]:
    key = llama_server_artifact_key()
    pin = LLAMA_SERVER_PINS.get(key)
    if not pin:
        raise LocalProviderError(
            "unsupported_platform",
            f"No pinned llama-server artifact for platform {key}",
        )
    return pin


def cache_root() -> Path:
    return Path(get_journal()) / "cache" / "providers" / LOCAL_PROVIDER_NAME


def binary_install_dir(
    artifact_key: str | None = None,
    pin: dict[str, str] | None = None,
) -> Path:
    artifact_key = artifact_key or llama_server_artifact_key()
    pin = pin or pin_for_current_platform()
    return cache_root() / "bin" / artifact_key / pin["release_tag"]


def binary_path_for_pin(
    artifact_key: str | None = None,
    pin: dict[str, str] | None = None,
) -> Path:
    pin = pin or pin_for_current_platform()
    return binary_install_dir(artifact_key, pin) / pin["binary_name"]


def _oci_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64", "x64"}:
        return "amd64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    raise LocalProviderError(
        "unsupported_platform",
        f"No OCI platform architecture mapping for machine {machine}",
    )


def _cuda_digest_hex() -> str:
    return CUDA_SERVER_PIN.image_ref.rsplit("@sha256:", 1)[1]


def cuda_binary_dir() -> Path:
    return cache_root() / "cuda" / llama_server_artifact_key() / _cuda_digest_hex()


def cuda_binary_path() -> Path:
    return cuda_binary_dir() / CUDA_SERVER_PIN.binary_name


def model_dir(model_id: str) -> Path:
    safe_id = model_id.replace("/", "__")
    return cache_root() / "models" / safe_id


def model_path(model_id: str) -> Path:
    spec = LOCAL_MODEL_SPECS[normalize_model_id(model_id)]
    return model_dir(spec.model_id) / spec.filename


def mmproj_path(model_id: str) -> Path | None:
    spec = LOCAL_MODEL_SPECS[normalize_model_id(model_id)]
    if spec.mmproj_filename is None:
        return None
    return model_dir(spec.model_id) / spec.mmproj_filename


def install_hint() -> str:
    return "journal install-provider local"


def _read_local_status() -> InstallStatus:
    return read_install_status(scope="bundled", name=LOCAL_PROVIDER_NAME)


def _write_local_status(status: InstallStatus) -> InstallStatus:
    write_install_status(status, scope="bundled")
    return status


def _write_local_metadata(updates: dict[str, str]) -> None:
    unknown_keys = sorted(set(updates) - _LOCAL_METADATA_KEYS)
    if unknown_keys:
        raise ValueError(f"unknown local install metadata key: {unknown_keys[0]}")

    config = read_journal_config()
    slot = (
        config.setdefault("providers", {})
        .setdefault("bundled", {})
        .setdefault(LOCAL_PROVIDER_NAME, {})
    )
    for key, value in updates.items():
        slot[key] = value
    write_journal_config(config)


def gpu_device_override() -> int | None:
    config = read_journal_config()
    record = config.get("providers", {}).get("bundled", {}).get(LOCAL_PROVIDER_NAME, {})
    if not isinstance(record, dict):
        return None
    value = record.get("vulkan_device_index")
    if value is None or isinstance(value, bool):
        return None
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if index >= 0 else None


def _record_local_progress(received: int, total: int | None) -> None:
    status = _read_local_status()
    if status["install_state"] not in IN_FLIGHT_STATES:
        return
    _write_local_status(bump_progress(status, received=received, total=total))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_sha256(path: Path, expected: str) -> None:
    actual = _sha256_file(path)
    if actual != expected:
        raise LocalProviderError(
            "sha256_mismatch",
            f"sha256 mismatch for {path.name}: expected {expected}, got {actual}",
        )


def _download_file(
    url: str,
    dest: Path,
    *,
    timeout_s: float = 600.0,
    on_progress: Callable[[int, int | None], None] | None = None,
) -> None:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with httpx.stream("GET", url, timeout=timeout_s, follow_redirects=True) as response:
        response.raise_for_status()
        total_header = response.headers.get("content-length")
        total = int(total_header) if total_header and total_header.isdigit() else None
        received = 0
        last_emit = time.monotonic()
        last_emitted_received = -1
        first_chunk = True
        with tmp.open("wb") as handle:
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                handle.write(chunk)
                received += len(chunk)
                if on_progress is None:
                    continue
                now = time.monotonic()
                if first_chunk or now - last_emit >= _PROGRESS_MIN_INTERVAL_SECONDS:
                    on_progress(received, total)
                    last_emit = now
                    last_emitted_received = received
                    first_chunk = False
    if on_progress is not None and received != last_emitted_received:
        on_progress(received, total)
    tmp.replace(dest)


def _safe_extract_tarball(tarball: Path, dest: Path) -> None:
    import tarfile

    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with tarfile.open(tarball, "r:*") as archive:
        for member in archive.getmembers():
            target = (dest / member.name).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise LocalProviderError(
                    "archive_path_traversal",
                    f"Unsafe tar member path: {member.name}",
                )
            if member.issym() or member.islnk():
                linkname = Path(member.linkname)
                if linkname.is_absolute():
                    raise LocalProviderError(
                        "archive_path_traversal",
                        f"Unsafe tar link target: {member.name} -> {member.linkname}",
                    )
                link_base = target.parent if member.issym() else dest_resolved
                link_target = (link_base / linkname).resolve()
                if (
                    link_target != dest_resolved
                    and dest_resolved not in link_target.parents
                ):
                    raise LocalProviderError(
                        "archive_path_traversal",
                        f"Unsafe tar link target: {member.name} -> {member.linkname}",
                    )
        archive.extractall(dest, filter="data")


def _find_extracted_binary(dest: Path, binary_name: str) -> Path:
    direct = dest / binary_name
    if direct.exists():
        return direct
    matches = [path for path in dest.rglob(binary_name) if path.is_file()]
    if not matches:
        raise LocalProviderError(
            "binary_missing",
            f"Extracted archive did not contain {binary_name}",
        )
    if len(matches) > 1:
        matches.sort(key=lambda path: len(path.parts))
    return matches[0]


def _chmod_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _clear_macos_quarantine(path: Path) -> None:
    if sys.platform != "darwin":
        return
    import subprocess

    try:
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", str(path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return


def probe_binary_runnable(binary_path: str | Path) -> tuple[bool, str | None]:
    import subprocess

    try:
        completed = subprocess.run(
            [str(binary_path), "--version"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {_PROBE_TIMEOUT_SECONDS}s"
    except Exception as exc:
        return False, str(exc)

    if completed.returncode == 0:
        return True, None

    detail = (
        (completed.stderr or "").strip()
        or (completed.stdout or "").strip()
        or f"exited with status {completed.returncode}"
    )
    return False, detail


def install_llama_server() -> dict[str, Any]:
    from solstone.think.providers import local_cuda

    choice = local_cuda.resolve_local_backend(CUDA_SERVER_PIN)
    if choice.backend == "cuda":
        return _install_cuda_llama_server()

    artifact_key = llama_server_artifact_key()
    pin = pin_for_current_platform()
    url = (
        "https://github.com/ggml-org/llama.cpp/releases/download/"
        f"{pin['release_tag']}/{pin['filename']}"
    )
    install_dir = binary_install_dir(artifact_key, pin)
    tarball = install_dir / pin["filename"]

    try:
        _write_local_status(
            transition_state(_read_local_status(), new_state="downloading")
        )
        _write_local_metadata({"binary_artifact": pin["filename"]})
        _download_file(url, tarball, on_progress=_record_local_progress)
        _write_local_status(
            transition_state(_read_local_status(), new_state="verifying")
        )
        _verify_sha256(tarball, pin["sha256"])
        if install_dir.exists():
            for child in install_dir.iterdir():
                if child != tarball:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
        _safe_extract_tarball(tarball, install_dir)
        extracted = _find_extracted_binary(install_dir, pin["binary_name"])
        final_path = binary_path_for_pin(artifact_key, pin)
        inner_dir = extracted.parent
        if inner_dir != install_dir:
            for item in inner_dir.iterdir():
                shutil.move(str(item), str(install_dir / item.name))
            inner_dir.rmdir()
        _chmod_executable(final_path)
        _clear_macos_quarantine(install_dir)
        _write_local_metadata(
            {
                "binary_artifact": pin["filename"],
                "binary_sha256": pin["sha256"],
                "binary_path": str(final_path),
            }
        )
        return _write_local_status(
            transition_state(_read_local_status(), new_state="installed")
        )
    except Exception as exc:
        _write_local_status(
            transition_state(_read_local_status(), new_state="failed", error=str(exc))
        )
        raise


def _install_cuda_llama_server() -> dict[str, Any]:
    from solstone.think.providers import oci_image

    try:
        arch = _oci_arch()
        wanted_files = CUDA_SERVER_PIN.wanted_files_for_arch(arch)
        _write_local_status(
            transition_state(_read_local_status(), new_state="downloading")
        )
        oci_image.pull_and_install(
            CUDA_SERVER_PIN.image_ref,
            arch,
            wanted_files,
            cuda_binary_dir(),
            policy=CUDA_SERVER_PIN.signature_policy,
        )
        _write_local_status(
            transition_state(_read_local_status(), new_state="verifying")
        )
        _chmod_executable(cuda_binary_path())
        return _write_local_status(
            transition_state(_read_local_status(), new_state="installed")
        )
    except Exception as exc:
        _write_local_status(
            transition_state(_read_local_status(), new_state="failed", error=str(exc))
        )
        raise


def install_model(model_id: str = LOCAL_MODEL) -> dict[str, Any]:
    spec = LOCAL_MODEL_SPECS[normalize_model_id(model_id)]
    url = f"https://huggingface.co/{spec.repo}/resolve/{spec.revision}/{spec.filename}"
    dest = model_path(spec.model_id)
    mmproj_dest = mmproj_path(spec.model_id)

    try:
        _write_local_status(
            transition_state(_read_local_status(), new_state="downloading")
        )
        _write_local_metadata({"model_id": spec.model_id})
        _download_file(url, dest, on_progress=_record_local_progress)
        if spec.mmproj_filename and mmproj_dest is not None:
            mmproj_url = (
                f"https://huggingface.co/{spec.repo}/resolve/"
                f"{spec.revision}/{spec.mmproj_filename}"
            )
            _download_file(mmproj_url, mmproj_dest)
        _write_local_status(
            transition_state(_read_local_status(), new_state="verifying")
        )
        _verify_sha256(dest, spec.sha256)
        metadata = {
            "model_id": spec.model_id,
            "model_path": str(dest),
            "model_sha256": spec.sha256,
        }
        if spec.mmproj_sha256 and mmproj_dest is not None:
            _verify_sha256(mmproj_dest, spec.mmproj_sha256)
            metadata["mmproj_path"] = str(mmproj_dest)
            metadata["mmproj_sha256"] = spec.mmproj_sha256
        _write_local_metadata(metadata)
        return _write_local_status(
            transition_state(_read_local_status(), new_state="installed")
        )
    except Exception as exc:
        _write_local_status(
            transition_state(_read_local_status(), new_state="failed", error=str(exc))
        )
        raise


def install_local(model_id: str = LOCAL_MODEL) -> dict[str, Any]:
    from solstone.think.providers import fit_report

    selected_model = normalize_model_id(model_id)
    readiness = inspect_readiness(selected_model)
    if readiness["binary_installed"] and readiness["model_installed"]:
        return _write_local_status(
            transition_state(_read_local_status(), new_state="installed")
        )

    report = fit_report.build_local_fit_report(selected_model)
    rendered = fit_report.render_fit_report(report)
    if report.overall == "blocked":
        raise LocalProviderError("host_unfit", rendered)
    if report.overall == "warning":
        LOG.warning("local provider host fit warning:\n%s", rendered)

    install_llama_server()
    return install_model(selected_model)


def inspect_artifacts(model_id: str | None = None) -> dict[str, Any]:
    from solstone.think.providers import oci_image

    config = read_journal_config()
    record = config.get("providers", {}).get("bundled", {}).get(LOCAL_PROVIDER_NAME, {})
    if not isinstance(record, dict):
        record = {}
    selected_model = normalize_model_id(
        model_id or record.get("model_id") or LOCAL_MODEL
    )
    spec = LOCAL_MODEL_SPECS[selected_model]

    # The persisted record is a cache keyed by model_id. Trust a recorded
    # artifact path only when the record is for the selected model AND the path
    # lives under that model's directory; otherwise it is stale (e.g. left by a
    # prior model's install before a LOCAL_MODEL change) and must be ignored so
    # we recompute from the spec. Never pair a recorded path from one model with
    # a freshly-recomputed path from another — a mixed gguf/mmproj pair aborts
    # llama-server at spawn with an n_embd text/projector mismatch.
    expected_dir = model_dir(selected_model)

    def _trusted_record_path(value: str | None) -> Path | None:
        if not value or record.get("model_id") != selected_model:
            return None
        candidate = Path(value)
        return candidate if candidate.parent == expected_dir else None

    gguf_path = _trusted_record_path(record.get("model_path")) or model_path(
        selected_model
    )
    resolved_mmproj = _trusted_record_path(record.get("mmproj_path")) or mmproj_path(
        selected_model
    )
    mmproj_installed = resolved_mmproj is None or resolved_mmproj.exists()

    pin = pin_for_current_platform()
    vulkan_binary_path = binary_path_for_pin(pin=pin)
    recorded_binary_path = record.get("binary_path")
    vulkan_binary_installed = (
        record.get("binary_artifact") == pin["filename"]
        and record.get("binary_sha256") == pin["sha256"]
        and recorded_binary_path is not None
        and Path(recorded_binary_path) == vulkan_binary_path
        and vulkan_binary_path.exists()
        and os.access(vulkan_binary_path, os.X_OK)
    )

    arch = _oci_arch()
    cuda_binary = cuda_binary_path()
    cuda_binary_installed = (
        oci_image.verify_sidecar_install(
            CUDA_SERVER_PIN.image_ref,
            arch,
            CUDA_SERVER_PIN.wanted_files_for_arch(arch),
            cuda_binary_dir(),
        )
        and cuda_binary.exists()
        and os.access(cuda_binary, os.X_OK)
    )

    return {
        "binary_installed": vulkan_binary_installed or cuda_binary_installed,
        "model_installed": gguf_path.exists() and mmproj_installed,
        "gguf_installed": gguf_path.exists(),
        "mmproj_installed": mmproj_installed,
        "vulkan_binary_installed": vulkan_binary_installed,
        "cuda_binary_installed": cuda_binary_installed,
        "vulkan_binary_path": str(vulkan_binary_path),
        "cuda_binary_path": str(cuda_binary),
        "binary_path": str(vulkan_binary_path),
        "model_path": str(gguf_path),
        "mmproj_path": str(resolved_mmproj) if resolved_mmproj is not None else None,
        "model_id": selected_model,
        "min_ram_bytes": spec.min_ram_bytes,
    }


def inspect_readiness(model_id: str | None = None) -> dict[str, Any]:
    from solstone.think.providers import local_cuda

    choice = local_cuda.resolve_local_backend(CUDA_SERVER_PIN)
    status = _read_local_status()
    artifacts = inspect_artifacts(model_id)
    memory_verdict = assess_memory(
        int(artifacts["min_ram_bytes"]), block_below_floor=False
    )

    if choice.backend == "cuda":
        binary_path = Path(str(artifacts["cuda_binary_path"]))
        binary_installed = bool(artifacts["cuda_binary_installed"])
        gpu_available = True
        gpu_probe_ok = True
    else:
        from solstone.think.providers import local_vulkan

        binary_path = Path(str(artifacts["vulkan_binary_path"]))
        binary_installed = bool(artifacts["vulkan_binary_installed"])
        selected_gpu = local_vulkan.select_device(
            local_vulkan.detect_gpus(), override_index=gpu_device_override()
        )
        gpu_available = selected_gpu is not None
        gpu_probe_ok = local_vulkan.gpu_probe_ok()

    return {
        "install_state": status["install_state"],
        "binary_installed": binary_installed,
        "model_installed": artifacts["model_installed"],
        "gguf_installed": artifacts["gguf_installed"],
        "mmproj_installed": artifacts["mmproj_installed"],
        "ram_sufficient": memory_verdict.severity != "blocked",
        "gpu_available": gpu_available,
        "gpu_probe_ok": gpu_probe_ok,
        "binary_path": str(binary_path),
        "model_path": artifacts["model_path"],
        "mmproj_path": artifacts["mmproj_path"],
        "model_id": artifacts["model_id"],
        "install_error": status["install_error"],
        "backend": choice.backend,
        "backend_reason": choice.reason,
    }


def ensure_artifacts_installed(model_id: str) -> LocalArtifacts:
    selected_model = normalize_model_id(model_id)
    readiness = inspect_readiness(selected_model)
    if not readiness["binary_installed"]:
        raise LocalProviderError("binary_missing", "Local runtime is not installed.")
    if not readiness["model_installed"]:
        raise LocalProviderError(
            "model_missing", "Local model files are not installed."
        )
    mmproj = readiness.get("mmproj_path")
    return LocalArtifacts(
        backend=str(readiness["backend"]),
        backend_reason=str(readiness["backend_reason"]),
        binary_path=Path(readiness["binary_path"]),
        lib_dir=cuda_binary_dir() if readiness["backend"] == "cuda" else None,
        gguf_path=Path(readiness["model_path"]),
        mmproj_path=Path(mmproj) if mmproj else None,
    )


__all__ = [
    "CUDA_SERVER_PIN",
    "LLAMA_SERVER_PINS",
    "CudaServerPin",
    "LocalArtifacts",
    "llama_server_artifact_key",
    "pin_for_current_platform",
    "binary_path_for_pin",
    "cuda_binary_dir",
    "cuda_binary_path",
    "model_path",
    "mmproj_path",
    "install_llama_server",
    "install_model",
    "install_local",
    "install_hint",
    "probe_binary_runnable",
    "gpu_device_override",
    "inspect_readiness",
    "ensure_artifacts_installed",
]
