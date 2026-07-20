# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Install and inspect bundled local provider artifacts.

This module owns local provider artifact acquisition. It performs no network
access at import time.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from solstone.think.journal_config import read_journal_config
from solstone.think.models import LOCAL_MODEL
from solstone.think.providers.artifact_proof import (
    ProofResult,
    ReadinessOutcome,
    artifact_manifest_path,
    build_manifest,
    prove_manifest,
    publish_staged_tree,
    write_manifest,
)
from solstone.think.providers.install_lease import (
    InstallLease,
    acquire_install_lease,
    assert_install_lease_owned,
)
from solstone.think.providers.install_state import (
    IN_FLIGHT_STATES,
    InstallStatus,
    InstallStatusMalformedError,
    assert_install_attempt_current,
    begin_or_replace_install_attempt,
    bump_progress,
    canonical_fingerprint,
    fingerprint_sha256,
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
    ArtifactTrust,
)
from solstone.think.providers.memory import assess_memory
from solstone.think.utils import get_journal

LOG = logging.getLogger(__name__)
LOCAL_PROVIDER_NAME = "local"
_PROBE_TIMEOUT_SECONDS = 10
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_OCI_SIDECAR_NAME = ".oci-install.json"


@dataclass(frozen=True)
class CudaArtifactPin:
    url: str
    sha256: str
    size_bytes: int
    release_tag: str
    upstream_image_digest: str
    llama_cpp_revision: str
    repack_revision: str


@dataclass(frozen=True)
class CudaServerPin:
    cuda_version: int
    embedded_arch_set: frozenset[str]
    binary_name: str
    device_flag_value: str
    visible_devices_env: str
    shared_wanted_files: tuple[str, ...]
    cpu_wanted_files_by_arch: dict[str, tuple[str, ...]]
    artifacts_by_key: dict[str, CudaArtifactPin]

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

CUDA_SERVER_PIN = CudaServerPin(
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
    artifacts_by_key={
        "x86_64-unknown-linux-gnu": CudaArtifactPin(
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
        "aarch64-unknown-linux-gnu": CudaArtifactPin(
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
    },
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


def cuda_artifact_pin_for_current_platform(
    pin: CudaServerPin | None = None,
) -> CudaArtifactPin | None:
    server_pin = pin or CUDA_SERVER_PIN
    return server_pin.artifacts_by_key.get(llama_server_artifact_key())


def require_cuda_artifact_pin_for_current_platform(
    pin: CudaServerPin | None = None,
) -> CudaArtifactPin:
    key = llama_server_artifact_key()
    server_pin = pin or CUDA_SERVER_PIN
    artifact_pin = server_pin.artifacts_by_key.get(key)
    if artifact_pin is None:
        raise LocalProviderError(
            "unsupported_platform",
            f"No pinned CUDA llama-server artifact for platform {key}",
        )
    return artifact_pin


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


def _cuda_binary_dir_for_pin(
    artifact_key: str,
    artifact_pin: CudaArtifactPin,
) -> Path:
    return cache_root() / "cuda" / artifact_key / artifact_pin.sha256


def cuda_binary_dir() -> Path:
    artifact_key = llama_server_artifact_key()
    artifact_pin = require_cuda_artifact_pin_for_current_platform()
    return _cuda_binary_dir_for_pin(artifact_key, artifact_pin)


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
    return read_install_status(name=LOCAL_PROVIDER_NAME)


def _write_local_status(status: InstallStatus) -> InstallStatus:
    write_install_status(status)
    return status


def gpu_device_override() -> int | None:
    config = read_journal_config()
    record = config.get("providers", {}).get(LOCAL_PROVIDER_NAME, {})
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


def _model_pin_identity(model_id: str) -> dict[str, Any]:
    spec = LOCAL_MODEL_SPECS[normalize_model_id(model_id)]
    return {
        "unit": "local-model",
        "model_id": spec.model_id,
        "repo": spec.repo,
        "revision": spec.revision,
        "filename": spec.filename,
        "sha256": spec.sha256,
        "mmproj_filename": spec.mmproj_filename,
        "mmproj_sha256": spec.mmproj_sha256,
    }


def _vulkan_pin_identity(
    artifact_key: str | None = None,
    pin: dict[str, str] | None = None,
) -> dict[str, Any]:
    artifact_key = artifact_key or llama_server_artifact_key()
    pin = pin or pin_for_current_platform()
    return {
        "unit": "llama-server-vulkan",
        "artifact_key": artifact_key,
        "release_tag": pin["release_tag"],
        "filename": pin["filename"],
        "sha256": pin["sha256"],
        "binary_name": pin["binary_name"],
    }


def _cuda_pin_identity(
    arch: str | None = None,
    wanted_files: tuple[str, ...] | None = None,
    artifact_key: str | None = None,
    artifact_pin: CudaArtifactPin | None = None,
) -> dict[str, Any]:
    artifact_key = artifact_key or llama_server_artifact_key()
    artifact_pin = artifact_pin or require_cuda_artifact_pin_for_current_platform()
    arch = arch or _oci_arch()
    wanted_files = wanted_files or CUDA_SERVER_PIN.wanted_files_for_arch(arch)
    return {
        "unit": "llama-server-cuda",
        "artifact_key": artifact_key,
        "url": artifact_pin.url,
        "sha256": artifact_pin.sha256,
        "size_bytes": artifact_pin.size_bytes,
        "release_tag": artifact_pin.release_tag,
        "upstream_image_digest": artifact_pin.upstream_image_digest,
        "llama_cpp_revision": artifact_pin.llama_cpp_revision,
        "repack_revision": artifact_pin.repack_revision,
        "arch": arch,
        "binary_name": CUDA_SERVER_PIN.binary_name,
        "wanted_files": list(wanted_files),
    }


def _prove_cuda_runtime_artifact(
    pin: CudaServerPin,
    *,
    journal_path: str | Path | None = None,
) -> ProofResult:
    artifact_key = llama_server_artifact_key()
    artifact_pin = cuda_artifact_pin_for_current_platform(pin)
    if artifact_pin is None:
        return ProofResult(
            status="missing-or-mismatched",
            reason_code="cuda_runtime_pin_missing",
            cache_hit=False,
        )
    arch = _oci_arch()
    wanted_files = pin.wanted_files_for_arch(arch)
    return prove_manifest(
        artifact_manifest_path(_cuda_binary_dir_for_pin(artifact_key, artifact_pin)),
        provider=LOCAL_PROVIDER_NAME,
        pin_identity=_cuda_pin_identity(
            arch,
            wanted_files,
            artifact_key=artifact_key,
            artifact_pin=artifact_pin,
        ),
        journal_path=journal_path,
    )


def probe_cuda_runtime_artifact_trust(
    pin: CudaServerPin,
    *,
    journal_path: str | Path | None = None,
) -> ArtifactTrust:
    if cuda_artifact_pin_for_current_platform(pin) is not None:
        return ArtifactTrust.TRUSTED
    try:
        result = _prove_cuda_runtime_artifact(pin, journal_path=journal_path)
    except Exception:
        LOG.warning(
            "CUDA runtime artifact trust probe failed; treating proof as unavailable",
            exc_info=True,
        )
        return ArtifactTrust.UNAVAILABLE
    if result.status == "ready":
        return ArtifactTrust.TRUSTED
    if result.status == "missing-or-mismatched":
        return ArtifactTrust.ABSENT
    if result.status == "proof-unavailable":
        return ArtifactTrust.UNAVAILABLE
    LOG.warning(
        "CUDA runtime artifact proof returned unknown status: %s", result.status
    )
    return ArtifactTrust.UNAVAILABLE


def has_persisted_installed_cuda_target(
    *,
    journal_path: str | Path | None = None,
) -> bool:
    try:
        status = read_install_status(
            name=LOCAL_PROVIDER_NAME,
            journal_path=journal_path,
        )
        target_json = status["target_fingerprint_json"]
        if status["install_state"] != "installed" or target_json is None:
            return False
        target = json.loads(target_json)
    except (InstallStatusMalformedError, ValueError):
        LOG.warning(
            "could not read persisted CUDA install target; not holding CUDA backend",
            exc_info=True,
        )
        return False
    return (
        isinstance(target, dict)
        and target.get("provider") == LOCAL_PROVIDER_NAME
        and target.get("backend") == "cuda"
    )


def target_fingerprint(model_id: str = LOCAL_MODEL) -> dict[str, Any]:
    from solstone.think.providers import local_cuda

    selected_model = normalize_model_id(model_id)
    choice = local_cuda.resolve_local_backend(CUDA_SERVER_PIN)
    runtime_pin = (
        _cuda_pin_identity(
            artifact_pin=require_cuda_artifact_pin_for_current_platform()
        )
        if choice.backend == "cuda"
        else _vulkan_pin_identity()
    )
    return {
        "provider": LOCAL_PROVIDER_NAME,
        "runtime": "llama.cpp",
        "backend": choice.backend,
        "backend_reason": choice.reason,
        "runtime_pin": runtime_pin,
        "model_pin": _model_pin_identity(selected_model),
    }


def _fingerprint_sha_for_target(fingerprint: dict[str, Any]) -> str:
    return fingerprint_sha256(canonical_fingerprint(fingerprint))


def _manifest_target_sha(
    attempt_status: InstallStatus | None,
    fingerprint: dict[str, Any],
) -> str:
    if attempt_status is not None and attempt_status["target_fingerprint_sha256"]:
        return str(attempt_status["target_fingerprint_sha256"])
    return _fingerprint_sha_for_target(fingerprint)


def _manifest_entry(path: Path, root: Path, role: str) -> dict[str, Any]:
    return {
        "relative_path": path.relative_to(root).as_posix(),
        "role": role,
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _runtime_inventory(root: Path, *, exclude_names: set[str]) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == artifact_manifest_path(root).name:
            continue
        if path.name in exclude_names:
            continue
        role = "runtime_binary" if path.name == "llama-server" else "runtime_support"
        inventory.append(_manifest_entry(path, root, role))
    return inventory


def _write_vulkan_manifest(
    *,
    artifact_key: str,
    pin: dict[str, str],
    attempt_status: InstallStatus | None,
    fingerprint: dict[str, Any] | None = None,
    root: Path | None = None,
) -> None:
    install_dir = root or binary_install_dir(artifact_key, pin)
    fingerprint = fingerprint or target_fingerprint()
    manifest = build_manifest(
        provider=LOCAL_PROVIDER_NAME,
        unit="llama-server-vulkan",
        target_fingerprint_sha256=_manifest_target_sha(attempt_status, fingerprint),
        source={"pin_identity": _vulkan_pin_identity(artifact_key, pin)},
        inventory=_runtime_inventory(install_dir, exclude_names={pin["filename"]}),
        attempt_id=attempt_status["attempt_id"] if attempt_status else None,
    )
    write_manifest(artifact_manifest_path(install_dir), manifest)


def _write_cuda_manifest(
    *,
    artifact_key: str,
    artifact_pin: CudaArtifactPin,
    arch: str,
    wanted_files: tuple[str, ...],
    attempt_status: InstallStatus | None,
    fingerprint: dict[str, Any] | None = None,
    root: Path | None = None,
) -> None:
    install_dir = root or _cuda_binary_dir_for_pin(artifact_key, artifact_pin)
    fingerprint = fingerprint or target_fingerprint()
    manifest = build_manifest(
        provider=LOCAL_PROVIDER_NAME,
        unit="llama-server-cuda",
        target_fingerprint_sha256=_manifest_target_sha(attempt_status, fingerprint),
        source={
            "pin_identity": _cuda_pin_identity(
                arch,
                wanted_files,
                artifact_key=artifact_key,
                artifact_pin=artifact_pin,
            )
        },
        inventory=_runtime_inventory(install_dir, exclude_names=set()),
        attempt_id=attempt_status["attempt_id"] if attempt_status else None,
    )
    write_manifest(artifact_manifest_path(install_dir), manifest)


def _write_model_manifest(
    *,
    model_id: str,
    attempt_status: InstallStatus | None,
    fingerprint: dict[str, Any] | None = None,
) -> None:
    spec = LOCAL_MODEL_SPECS[normalize_model_id(model_id)]
    root = model_dir(spec.model_id)
    inventory = [_manifest_entry(model_path(spec.model_id), root, "model")]
    projector = mmproj_path(spec.model_id)
    if projector is not None:
        inventory.append(_manifest_entry(projector, root, "projector"))
    fingerprint = fingerprint or target_fingerprint(spec.model_id)
    manifest = build_manifest(
        provider=LOCAL_PROVIDER_NAME,
        unit="local-model",
        target_fingerprint_sha256=_manifest_target_sha(attempt_status, fingerprint),
        source={"pin_identity": _model_pin_identity(spec.model_id)},
        inventory=inventory,
        attempt_id=attempt_status["attempt_id"] if attempt_status else None,
    )
    write_manifest(artifact_manifest_path(root), manifest)


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
        with tmp.open("wb") as handle:
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                handle.write(chunk)
                received += len(chunk)
                if on_progress is not None:
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


def _download_verify_extract_tarball(
    *,
    url: str,
    filename: str,
    sha256: str,
    staging: Path,
    on_progress: Callable[[int, int | None], None],
) -> None:
    tarball = staging / filename
    _download_file(url, tarball, on_progress=on_progress)
    _write_local_status(transition_state(_read_local_status(), new_state="verifying"))
    _verify_sha256(tarball, sha256)
    _safe_extract_tarball(tarball, staging)
    tarball.unlink(missing_ok=True)


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


def _cuda_artifact_filename(artifact_pin: CudaArtifactPin) -> str:
    return artifact_pin.url.rsplit("/", 1)[-1]


def _verify_cuda_runtime_tree(
    root: Path,
    *,
    wanted_files: tuple[str, ...],
) -> None:
    missing: list[str] = []
    for wanted in wanted_files:
        if not (root / wanted).is_file():
            missing.append(wanted)
    if not (root / "licenses").is_dir():
        missing.append("licenses/")
    if not (root / "provenance.json").is_file():
        missing.append("provenance.json")
    if missing:
        raise LocalProviderError(
            "cuda_runtime_incomplete",
            "CUDA runtime artifact is missing required paths: " + ", ".join(missing),
        )


def _is_legacy_cuda_oci_tree(path: Path) -> bool:
    if not path.is_dir() or path.is_symlink():
        return False
    if not _HEX64_RE.fullmatch(path.name):
        return False
    sidecar = path / _LEGACY_OCI_SIDECAR_NAME
    if not sidecar.is_file():
        return False
    try:
        record = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    image_ref = record.get("image_ref")
    files = record.get("files")
    return (
        isinstance(image_ref, str)
        and image_ref.endswith(f"@sha256:{path.name}")
        and isinstance(files, dict)
    )


def _cleanup_legacy_cuda_oci_dirs(
    *,
    artifact_key: str,
    keep_dir: Path,
) -> None:
    root = cache_root() / "cuda" / artifact_key
    if not root.is_dir():
        return
    keep_resolved = keep_dir.resolve()
    for candidate in root.iterdir():
        try:
            if not candidate.is_dir():
                continue
            if candidate.resolve() == keep_resolved:
                continue
            if not _is_legacy_cuda_oci_tree(candidate):
                continue
            shutil.rmtree(candidate)
        except Exception:
            LOG.warning(
                "failed to remove legacy CUDA OCI install tree: %s",
                candidate,
                exc_info=True,
            )


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


def install_llama_server(
    *,
    attempt_status: InstallStatus | None = None,
    fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from solstone.think.providers import local_cuda

    choice = local_cuda.resolve_local_backend(CUDA_SERVER_PIN)
    if choice.backend == "cuda":
        return _install_cuda_llama_server(
            attempt_status=attempt_status,
            fingerprint=fingerprint,
        )

    artifact_key = llama_server_artifact_key()
    pin = pin_for_current_platform()
    url = (
        "https://github.com/ggml-org/llama.cpp/releases/download/"
        f"{pin['release_tag']}/{pin['filename']}"
    )
    install_dir = binary_install_dir(artifact_key, pin)
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{install_dir.name}.staging-", dir=install_dir.parent)
    )

    try:
        _write_local_status(
            transition_state(_read_local_status(), new_state="downloading")
        )
        _download_verify_extract_tarball(
            url=url,
            filename=pin["filename"],
            sha256=pin["sha256"],
            staging=staging,
            on_progress=_record_local_progress,
        )
        extracted = _find_extracted_binary(staging, pin["binary_name"])
        final_path = staging / pin["binary_name"]
        if attempt_status is not None:
            assert_install_attempt_current(attempt_status)
        inner_dir = extracted.parent
        if inner_dir != staging:
            for item in inner_dir.iterdir():
                shutil.move(str(item), str(staging / item.name))
            inner_dir.rmdir()
        _chmod_executable(final_path)
        _clear_macos_quarantine(staging)
        _write_vulkan_manifest(
            artifact_key=artifact_key,
            pin=pin,
            attempt_status=attempt_status,
            fingerprint=fingerprint,
            root=staging,
        )
        if attempt_status is not None:
            assert_install_attempt_current(attempt_status)
        publish_staged_tree(staging, install_dir)
        return _read_local_status()
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        _write_local_status(
            transition_state(
                _read_local_status(),
                new_state="failed",
                error=str(exc),
                error_code=getattr(exc, "reason_code", None),
            )
        )
        raise


def _install_cuda_llama_server(
    *,
    attempt_status: InstallStatus | None = None,
    fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_key = llama_server_artifact_key()
    artifact_pin = require_cuda_artifact_pin_for_current_platform(CUDA_SERVER_PIN)
    install_dir = _cuda_binary_dir_for_pin(artifact_key, artifact_pin)
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{install_dir.name}.staging-", dir=install_dir.parent)
    )
    try:
        arch = _oci_arch()
        wanted_files = CUDA_SERVER_PIN.wanted_files_for_arch(arch)
        _write_local_status(
            transition_state(_read_local_status(), new_state="downloading")
        )
        _download_verify_extract_tarball(
            url=artifact_pin.url,
            filename=_cuda_artifact_filename(artifact_pin),
            sha256=artifact_pin.sha256,
            staging=staging,
            on_progress=_record_local_progress,
        )
        _verify_cuda_runtime_tree(staging, wanted_files=wanted_files)
        final_path = staging / CUDA_SERVER_PIN.binary_name
        if attempt_status is not None:
            assert_install_attempt_current(attempt_status)
        _chmod_executable(final_path)
        _clear_macos_quarantine(staging)
        _write_cuda_manifest(
            artifact_key=artifact_key,
            artifact_pin=artifact_pin,
            arch=arch,
            wanted_files=wanted_files,
            attempt_status=attempt_status,
            fingerprint=fingerprint,
            root=staging,
        )
        if attempt_status is not None:
            assert_install_attempt_current(attempt_status)
        publish_staged_tree(staging, install_dir)
        _cleanup_legacy_cuda_oci_dirs(artifact_key=artifact_key, keep_dir=install_dir)
        return _read_local_status()
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        _write_local_status(
            transition_state(
                _read_local_status(),
                new_state="failed",
                error=str(exc),
                error_code=getattr(exc, "reason_code", None),
            )
        )
        raise


def install_model(
    model_id: str = LOCAL_MODEL,
    *,
    attempt_status: InstallStatus | None = None,
    fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = LOCAL_MODEL_SPECS[normalize_model_id(model_id)]
    url = f"https://huggingface.co/{spec.repo}/resolve/{spec.revision}/{spec.filename}"
    dest = model_path(spec.model_id)
    mmproj_dest = mmproj_path(spec.model_id)

    try:
        _write_local_status(
            transition_state(_read_local_status(), new_state="downloading")
        )
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
        if spec.mmproj_sha256 and mmproj_dest is not None:
            _verify_sha256(mmproj_dest, spec.mmproj_sha256)
        if attempt_status is not None:
            assert_install_attempt_current(attempt_status)
        _write_model_manifest(
            model_id=spec.model_id,
            attempt_status=attempt_status,
            fingerprint=fingerprint,
        )
        return _read_local_status()
    except Exception as exc:
        _write_local_status(
            transition_state(
                _read_local_status(),
                new_state="failed",
                error=str(exc),
                error_code=getattr(exc, "reason_code", None),
            )
        )
        raise


def install_local(
    model_id: str = LOCAL_MODEL,
    *,
    lease: InstallLease | None = None,
    attempt_status: InstallStatus | None = None,
) -> dict[str, Any]:
    from solstone.think.providers import fit_report

    selected_model = normalize_model_id(model_id)
    fingerprint = target_fingerprint(selected_model)
    owned_lease = lease is None
    if lease is None:
        lease = acquire_install_lease(LOCAL_PROVIDER_NAME)
        if lease is None:
            raise LocalProviderError(
                "install_busy", "Local provider install is already running."
            )
    lease = assert_install_lease_owned(lease, LOCAL_PROVIDER_NAME)

    try:
        if attempt_status is None:
            attempt_status = begin_or_replace_install_attempt(
                LOCAL_PROVIDER_NAME,
                fingerprint,
                initial_state="resolving",
                owner={"entry": "install_local"},
            )
        readiness = inspect_readiness(selected_model)
        if readiness.status in {"proof-unavailable", "host-ineligible"}:
            current = assert_install_attempt_current(attempt_status)
            return _write_local_status(
                transition_state(
                    current,
                    new_state="failed",
                    error=readiness.reason_code,
                    error_code=readiness.reason_code,
                )
            )

        if not readiness.ready:
            report = fit_report.build_local_fit_report(selected_model)
            rendered = fit_report.render_fit_report(report)
            if report.overall == "blocked":
                raise LocalProviderError("host_unfit", rendered)
            if report.overall == "warning":
                LOG.warning("local provider host fit warning:\n%s", rendered)

            if readiness.proof["binary"]["status"] == "missing-or-mismatched":
                assert_install_lease_owned(lease, LOCAL_PROVIDER_NAME)
                install_llama_server(
                    attempt_status=attempt_status,
                    fingerprint=fingerprint,
                )
            if readiness.proof["model"]["status"] == "missing-or-mismatched":
                assert_install_lease_owned(lease, LOCAL_PROVIDER_NAME)
                install_model(
                    selected_model,
                    attempt_status=attempt_status,
                    fingerprint=fingerprint,
                )

        final_readiness = inspect_readiness(selected_model)
        assert_install_lease_owned(lease, LOCAL_PROVIDER_NAME)
        current = assert_install_attempt_current(attempt_status)
        if final_readiness.ready:
            return _write_local_status(transition_state(current, new_state="installed"))
        return _write_local_status(
            transition_state(
                current,
                new_state="failed",
                error=final_readiness.reason_code,
                error_code=final_readiness.reason_code,
            )
        )
    except Exception as exc:
        try:
            current = assert_install_attempt_current(attempt_status)
            _write_local_status(
                transition_state(
                    current,
                    new_state="failed",
                    error=str(exc),
                    error_code=getattr(exc, "reason_code", None),
                )
            )
        except Exception:
            pass
        raise
    finally:
        if owned_lease:
            lease.release()


def _proof_payload(
    status: str, reason_code: str, *, cache_hit: bool = False
) -> dict[str, Any]:
    return {"status": status, "reason_code": reason_code, "cache_hit": cache_hit}


def _proof_result_payload(result: Any) -> dict[str, Any]:
    return _proof_payload(
        result.status,
        result.reason_code,
        cache_hit=bool(getattr(result, "cache_hit", False)),
    )


def _combined_artifact_status(
    *proofs: dict[str, Any],
) -> tuple[str, str]:
    for proof in proofs:
        if proof["status"] == "proof-unavailable":
            return "proof-unavailable", str(proof["reason_code"])
    for proof in proofs:
        if proof["status"] == "missing-or-mismatched":
            return "missing-or-mismatched", str(proof["reason_code"])
    return "ready", "ready"


def inspect_artifacts(model_id: str | None = None) -> dict[str, Any]:
    selected_model = normalize_model_id(model_id or LOCAL_MODEL)
    spec = LOCAL_MODEL_SPECS[selected_model]
    gguf_path = model_path(selected_model)
    resolved_mmproj = mmproj_path(selected_model)

    pin = pin_for_current_platform()
    vulkan_binary_path = binary_path_for_pin(pin=pin)
    vulkan_proof = prove_manifest(
        artifact_manifest_path(binary_install_dir(pin=pin)),
        provider=LOCAL_PROVIDER_NAME,
        pin_identity=_vulkan_pin_identity(pin=pin),
    )
    vulkan_payload = _proof_result_payload(vulkan_proof)

    cuda_artifact_pin = cuda_artifact_pin_for_current_platform(CUDA_SERVER_PIN)
    cuda_binary = (
        _cuda_binary_dir_for_pin(llama_server_artifact_key(), cuda_artifact_pin)
        / CUDA_SERVER_PIN.binary_name
        if cuda_artifact_pin is not None
        else None
    )
    cuda_proof = _prove_cuda_runtime_artifact(CUDA_SERVER_PIN)
    cuda_payload = _proof_result_payload(cuda_proof)
    model_proof = prove_manifest(
        artifact_manifest_path(model_dir(selected_model)),
        provider=LOCAL_PROVIDER_NAME,
        pin_identity=_model_pin_identity(selected_model),
    )
    model_payload = _proof_result_payload(model_proof)

    return {
        "binary_installed": vulkan_proof.ready or cuda_proof.ready,
        "model_installed": model_proof.ready,
        "gguf_installed": model_proof.ready,
        "mmproj_installed": model_proof.ready,
        "vulkan_binary_installed": vulkan_proof.ready,
        "cuda_binary_installed": cuda_proof.ready,
        "vulkan_binary_path": str(vulkan_binary_path),
        "cuda_binary_path": str(cuda_binary) if cuda_binary is not None else None,
        "binary_path": str(vulkan_binary_path),
        "model_path": str(gguf_path),
        "mmproj_path": str(resolved_mmproj) if resolved_mmproj is not None else None,
        "model_id": selected_model,
        "min_ram_bytes": spec.min_ram_bytes,
        "vulkan_proof": vulkan_payload,
        "cuda_proof": cuda_payload,
        "model_proof": model_payload,
    }


def inspect_readiness(model_id: str | None = None) -> ReadinessOutcome:
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

    binary_proof = (
        artifacts["cuda_proof"]
        if choice.backend == "cuda"
        else artifacts["vulkan_proof"]
    )
    artifact_status, artifact_reason = _combined_artifact_status(
        binary_proof,
        artifacts["model_proof"],
    )
    ram_sufficient = memory_verdict.severity != "blocked"
    if artifact_status != "ready":
        readiness_status = artifact_status
        reason_code = artifact_reason
    elif not ram_sufficient:
        readiness_status = "host-ineligible"
        reason_code = "ram_insufficient"
    elif not gpu_probe_ok:
        readiness_status = "host-ineligible"
        reason_code = "gpu_probe_failed"
    elif not gpu_available:
        readiness_status = "host-ineligible"
        reason_code = "gpu_unavailable"
    else:
        readiness_status = "ready"
        reason_code = "ready"

    return ReadinessOutcome(
        provider=LOCAL_PROVIDER_NAME,
        status=readiness_status,  # type: ignore[arg-type]
        reason_code=reason_code,
        target={
            "model_id": artifacts["model_id"],
            "target_fingerprint_json": status["target_fingerprint_json"],
            "target_fingerprint_sha256": status["target_fingerprint_sha256"],
        },
        install={
            "install_state": status["install_state"],
            "install_error": status["install_error"],
            "error_code": status["error_code"],
            "attempt_id": status["attempt_id"],
            "progress_bytes_received": status["progress_bytes_received"],
            "progress_bytes_total": status["progress_bytes_total"],
            "last_transition_at": status["last_transition_at"],
            "last_progress_at": status["last_progress_at"],
        },
        host={
            "ram_sufficient": ram_sufficient,
            "gpu_available": gpu_available,
            "gpu_probe_ok": gpu_probe_ok,
            "backend": choice.backend,
            "backend_reason": choice.reason,
        },
        artifacts={
            **artifacts,
            "binary_installed": binary_installed,
            "binary_path": str(binary_path),
        },
        proof={
            "binary": binary_proof,
            "model": artifacts["model_proof"],
            "vulkan": artifacts["vulkan_proof"],
            "cuda": artifacts["cuda_proof"],
        },
    )


def ensure_artifacts_installed(model_id: str) -> LocalArtifacts:
    selected_model = normalize_model_id(model_id)
    readiness = inspect_readiness(selected_model)
    if not readiness.artifacts["binary_installed"]:
        raise LocalProviderError("binary_missing", "Local runtime is not installed.")
    if not readiness.artifacts["model_installed"]:
        raise LocalProviderError(
            "model_missing", "Local model files are not installed."
        )
    mmproj = readiness.artifacts.get("mmproj_path")
    return LocalArtifacts(
        backend=str(readiness.host["backend"]),
        backend_reason=str(readiness.host["backend_reason"]),
        binary_path=Path(readiness.artifacts["binary_path"]),
        lib_dir=cuda_binary_dir() if readiness.host["backend"] == "cuda" else None,
        gguf_path=Path(readiness.artifacts["model_path"]),
        mmproj_path=Path(mmproj) if mmproj else None,
    )


__all__ = [
    "CUDA_SERVER_PIN",
    "LLAMA_SERVER_PINS",
    "CudaArtifactPin",
    "CudaServerPin",
    "LocalArtifacts",
    "has_persisted_installed_cuda_target",
    "llama_server_artifact_key",
    "pin_for_current_platform",
    "cuda_artifact_pin_for_current_platform",
    "require_cuda_artifact_pin_for_current_platform",
    "binary_path_for_pin",
    "cuda_binary_dir",
    "cuda_binary_path",
    "model_path",
    "mmproj_path",
    "install_llama_server",
    "install_model",
    "install_local",
    "install_hint",
    "probe_cuda_runtime_artifact_trust",
    "probe_binary_runnable",
    "gpu_device_override",
    "inspect_readiness",
    "ensure_artifacts_installed",
    "target_fingerprint",
]
