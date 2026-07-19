# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Install and inspect bundled parakeet.cpp provider artifacts.

This module owns parakeet.cpp provider artifact acquisition. It performs no
network access at import time.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from solstone.think import parakeet_readiness
from solstone.think.providers.artifact_proof import (
    ReadinessOutcome,
    artifact_manifest_path,
    build_manifest,
    prove_manifest,
    write_manifest,
)
from solstone.think.providers.install_lease import InstallLease, acquire_install_lease
from solstone.think.providers.install_state import (
    IN_FLIGHT_STATES,
    InstallStatus,
    assert_install_attempt_current,
    begin_or_replace_install_attempt,
    bump_progress,
    canonical_fingerprint,
    fingerprint_sha256,
    read_install_status,
    transition_state,
    write_install_status,
)
from solstone.think.utils import get_journal

LOG = logging.getLogger(__name__)
PARAKEET_PROVIDER_NAME = "parakeet"
_PROBE_TIMEOUT_SECONDS = 10

PARAKEET_SERVER_PINS: dict[tuple[str, str], dict[str, str]] = {
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


@dataclass(frozen=True)
class ParakeetModelSpec:
    repo: str
    filename: str
    revision: str
    sha256: str
    size_bytes: int


PARAKEET_MODEL_SPEC = ParakeetModelSpec(
    repo=parakeet_readiness.PARAKEET_CPP_MODEL_REPO,
    filename=parakeet_readiness.PARAKEET_CPP_MODEL_FILENAME,
    revision=parakeet_readiness.PARAKEET_CPP_MODEL_REVISION,
    sha256="4d69a4a6683f4f2d952bad794c1357ca6eb628027695b4699c5a9ad4cd07d757",
    size_bytes=940663680,
)


class ParakeetProviderError(RuntimeError):
    """Parakeet provider failure with a recovery reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _validate_backend(backend: str) -> str:
    if backend not in parakeet_readiness.PARAKEET_CPP_BINARY_BACKENDS:
        valid = ", ".join(parakeet_readiness.PARAKEET_CPP_BINARY_BACKENDS)
        raise ParakeetProviderError(
            "unsupported_backend", f"parakeet backend must be one of: {valid}"
        )
    return backend


def _pin_for_backend(artifact_key: str, backend: str) -> dict[str, str]:
    backend = _validate_backend(backend)
    pin = PARAKEET_SERVER_PINS.get((artifact_key, backend))
    if pin is None:
        raise ParakeetProviderError(
            "unsupported_platform",
            f"No pinned parakeet artifact for {artifact_key}/{backend}",
        )
    return pin


def parakeet_server_artifact_key() -> str:
    try:
        return parakeet_readiness.parakeet_cpp_artifact_key()
    except RuntimeError as exc:
        raise ParakeetProviderError("unsupported_platform", str(exc)) from exc


def cache_root(journal_path: str | Path | None = None) -> Path:
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return parakeet_readiness.parakeet_cpp_cache_root(root)


def binary_install_dir(backend: str, journal_path: str | Path | None = None) -> Path:
    return parakeet_readiness.parakeet_cpp_binary_install_dir(
        cache_root(journal_path),
        parakeet_server_artifact_key(),
        _validate_backend(backend),
    )


def binary_path(backend: str, journal_path: str | Path | None = None) -> Path:
    return parakeet_readiness.parakeet_cpp_binary_path(
        cache_root(journal_path),
        parakeet_server_artifact_key(),
        _validate_backend(backend),
    )


def model_dir(journal_path: str | Path | None = None) -> Path:
    return parakeet_readiness.parakeet_cpp_model_dir(cache_root(journal_path))


def model_path(journal_path: str | Path | None = None) -> Path:
    return parakeet_readiness.parakeet_cpp_model_path(cache_root(journal_path))


def install_hint() -> str:
    return "journal install-provider parakeet"


def _read_parakeet_status(
    journal_path: str | Path | None = None,
) -> InstallStatus:
    return read_install_status(name=PARAKEET_PROVIDER_NAME, journal_path=journal_path)


def _write_parakeet_status(
    status: InstallStatus,
    journal_path: str | Path | None = None,
) -> InstallStatus:
    write_install_status(status, journal_path=journal_path)
    return status


def _record_parakeet_progress(
    received: int,
    total: int | None,
    *,
    journal_path: str | Path | None = None,
) -> None:
    status = _read_parakeet_status(journal_path)
    if status["install_state"] not in IN_FLIGHT_STATES:
        return
    _write_parakeet_status(
        bump_progress(status, received=received, total=total),
        journal_path=journal_path,
    )


def _binary_pin_identity(artifact_key: str, backend: str) -> dict[str, Any]:
    pin = _pin_for_backend(artifact_key, backend)
    return {
        "unit": "parakeet-server",
        "artifact_key": artifact_key,
        "backend": backend,
        "release_tag": parakeet_readiness.PARAKEET_CPP_RELEASE_TAG,
        "filename": pin["filename"],
        "sha256": pin["sha256"],
        "binary_name": parakeet_readiness.PARAKEET_CPP_BINARY_NAME,
    }


def _model_pin_identity() -> dict[str, Any]:
    spec = PARAKEET_MODEL_SPEC
    return {
        "unit": "parakeet-model",
        "repo": spec.repo,
        "filename": spec.filename,
        "revision": spec.revision,
        "sha256": spec.sha256,
    }


def target_fingerprint(
    *,
    journal_path: str | Path | None = None,
) -> dict[str, Any]:
    artifact_key = parakeet_server_artifact_key()
    return {
        "provider": PARAKEET_PROVIDER_NAME,
        "runtime": "parakeet.cpp",
        "artifact_key": artifact_key,
        "binary_pins": [
            _binary_pin_identity(artifact_key, backend)
            for backend in parakeet_readiness.PARAKEET_CPP_BINARY_BACKENDS
        ],
        "model_pin": _model_pin_identity(),
        "cache_root": str(cache_root(journal_path)),
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


def _runtime_inventory(
    root: Path,
    *,
    backend: str,
    exclude_names: set[str],
) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == artifact_manifest_path(root).name:
            continue
        if path.name in exclude_names:
            continue
        role = (
            f"runtime_binary_{backend}"
            if path.name == parakeet_readiness.PARAKEET_CPP_BINARY_NAME
            else f"runtime_support_{backend}"
        )
        inventory.append(_manifest_entry(path, root, role))
    return inventory


def _write_binary_manifest(
    *,
    backend: str,
    attempt_status: InstallStatus | None,
    fingerprint: dict[str, Any] | None,
    journal_path: str | Path | None,
) -> None:
    artifact_key = parakeet_server_artifact_key()
    pin = _pin_for_backend(artifact_key, backend)
    root = binary_install_dir(backend, journal_path)
    fingerprint = fingerprint or target_fingerprint(journal_path=journal_path)
    manifest = build_manifest(
        provider=PARAKEET_PROVIDER_NAME,
        unit=f"parakeet-server-{backend}",
        target_fingerprint_sha256=_manifest_target_sha(attempt_status, fingerprint),
        source={"pin_identity": _binary_pin_identity(artifact_key, backend)},
        inventory=_runtime_inventory(
            root, backend=backend, exclude_names={pin["filename"]}
        ),
        attempt_id=attempt_status["attempt_id"] if attempt_status else None,
    )
    write_manifest(artifact_manifest_path(root), manifest)


def _write_model_manifest(
    *,
    attempt_status: InstallStatus | None,
    fingerprint: dict[str, Any] | None,
    journal_path: str | Path | None,
) -> None:
    root = model_dir(journal_path)
    fingerprint = fingerprint or target_fingerprint(journal_path=journal_path)
    manifest = build_manifest(
        provider=PARAKEET_PROVIDER_NAME,
        unit="parakeet-model",
        target_fingerprint_sha256=_manifest_target_sha(attempt_status, fingerprint),
        source={"pin_identity": _model_pin_identity()},
        inventory=[_manifest_entry(model_path(journal_path), root, "model")],
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
        raise ParakeetProviderError(
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
                if chunk:
                    handle.write(chunk)
                    received += len(chunk)
                    if on_progress is not None:
                        on_progress(received, total)
    tmp.rename(dest)


def _safe_extract_tarball(tarball: Path, dest: Path) -> None:
    import tarfile

    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with tarfile.open(tarball, "r:*") as archive:
        for member in archive.getmembers():
            target = (dest / member.name).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise ParakeetProviderError(
                    "archive_path_traversal",
                    f"Unsafe tar member path: {member.name}",
                )
            if member.issym() or member.islnk():
                linkname = Path(member.linkname)
                if linkname.is_absolute():
                    raise ParakeetProviderError(
                        "archive_path_traversal",
                        f"Unsafe tar link target: {member.name} -> {member.linkname}",
                    )
                link_base = target.parent if member.issym() else dest_resolved
                link_target = (link_base / linkname).resolve()
                if (
                    link_target != dest_resolved
                    and dest_resolved not in link_target.parents
                ):
                    raise ParakeetProviderError(
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
        raise ParakeetProviderError(
            "binary_missing",
            f"Extracted archive did not contain {binary_name}",
        )
    if len(matches) > 1:
        matches.sort(key=lambda path: len(path.parts))
    return matches[0]


def _chmod_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


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


def _install_parakeet_server_unlocked(
    backend: str,
    journal_path: str | Path | None = None,
    *,
    attempt_status: InstallStatus | None = None,
    fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    backend = _validate_backend(backend)
    artifact_key = parakeet_server_artifact_key()
    pin = _pin_for_backend(artifact_key, backend)
    url = (
        "https://github.com/mudler/parakeet.cpp/releases/download/"
        f"{parakeet_readiness.PARAKEET_CPP_RELEASE_TAG}/{pin['filename']}"
    )
    install_dir = binary_install_dir(backend, journal_path)
    tarball = install_dir / pin["filename"]

    try:
        _write_parakeet_status(
            transition_state(
                _read_parakeet_status(journal_path), new_state="downloading"
            ),
            journal_path,
        )
        _download_file(
            url,
            tarball,
            on_progress=lambda received, total: _record_parakeet_progress(
                received, total, journal_path=journal_path
            ),
        )
        _write_parakeet_status(
            transition_state(
                _read_parakeet_status(journal_path), new_state="verifying"
            ),
            journal_path,
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
        extracted = _find_extracted_binary(
            install_dir, parakeet_readiness.PARAKEET_CPP_BINARY_NAME
        )
        final_path = binary_path(backend, journal_path)
        if attempt_status is not None:
            assert_install_attempt_current(attempt_status, journal_path=journal_path)
        inner_dir = extracted.parent
        if inner_dir != install_dir:
            for item in inner_dir.iterdir():
                shutil.move(str(item), str(install_dir / item.name))
            inner_dir.rmdir()
        _chmod_executable(final_path)
        _write_binary_manifest(
            backend=backend,
            attempt_status=attempt_status,
            fingerprint=fingerprint,
            journal_path=journal_path,
        )
        return _read_parakeet_status(journal_path)
    except Exception as exc:
        _write_parakeet_status(
            transition_state(
                _read_parakeet_status(journal_path),
                new_state="failed",
                error=str(exc),
            ),
            journal_path,
        )
        raise


def install_parakeet_server(
    backend: str,
    *,
    journal_path: str | Path | None = None,
) -> dict[str, Any]:
    return _install_parakeet_server_unlocked(backend, journal_path)


def _install_model_unlocked(
    journal_path: str | Path | None = None,
    *,
    attempt_status: InstallStatus | None = None,
    fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = PARAKEET_MODEL_SPEC
    url = f"https://huggingface.co/{spec.repo}/resolve/{spec.revision}/{spec.filename}"
    dest = model_path(journal_path)

    try:
        _write_parakeet_status(
            transition_state(
                _read_parakeet_status(journal_path), new_state="downloading"
            ),
            journal_path,
        )
        _download_file(
            url,
            dest,
            on_progress=lambda received, total: _record_parakeet_progress(
                received, total, journal_path=journal_path
            ),
        )
        _write_parakeet_status(
            transition_state(
                _read_parakeet_status(journal_path), new_state="verifying"
            ),
            journal_path,
        )
        _verify_sha256(dest, spec.sha256)
        if attempt_status is not None:
            assert_install_attempt_current(attempt_status, journal_path=journal_path)
        _write_model_manifest(
            attempt_status=attempt_status,
            fingerprint=fingerprint,
            journal_path=journal_path,
        )
        return _read_parakeet_status(journal_path)
    except Exception as exc:
        _write_parakeet_status(
            transition_state(
                _read_parakeet_status(journal_path),
                new_state="failed",
                error=str(exc),
            ),
            journal_path,
        )
        raise


def install_model(
    *,
    journal_path: str | Path | None = None,
) -> dict[str, Any]:
    return _install_model_unlocked(journal_path)


def install_parakeet(
    *,
    force: bool = False,
    journal_path: str | Path | None = None,
    lease: InstallLease | None = None,
    attempt_status: InstallStatus | None = None,
) -> dict[str, Any]:
    fingerprint = target_fingerprint(journal_path=journal_path)
    owned_lease = lease is None
    if lease is None:
        lease = acquire_install_lease(PARAKEET_PROVIDER_NAME, journal_path=journal_path)
        if lease is None:
            raise ParakeetProviderError(
                "install_busy", "Parakeet provider install is already running."
            )
    try:
        if attempt_status is None:
            attempt_status = begin_or_replace_install_attempt(
                PARAKEET_PROVIDER_NAME,
                fingerprint,
                initial_state="resolving",
                owner={"entry": "install_parakeet"},
                journal_path=journal_path,
            )
        readiness = inspect_readiness(journal_path)
        if readiness.status in {"proof-unavailable", "host-ineligible"}:
            current = assert_install_attempt_current(
                attempt_status, journal_path=journal_path
            )
            return _write_parakeet_status(
                transition_state(
                    current,
                    new_state="failed",
                    error=readiness.reason_code,
                    error_code=readiness.reason_code,
                ),
                journal_path,
            )
        from solstone.think.providers import fit_report

        if force or not readiness.ready:
            report = fit_report.build_parakeet_fit_report(journal_path)
            rendered = fit_report.render_fit_report(report)
            if report.overall == "blocked":
                raise ParakeetProviderError("host_unfit", rendered)
            if report.overall == "warning":
                LOG.warning("parakeet.cpp host fit warning:\n%s", rendered)
            for backend in parakeet_readiness.PARAKEET_CPP_BINARY_BACKENDS:
                if (
                    force
                    or readiness.proof[f"binary_{backend}"]["status"]
                    == "missing-or-mismatched"
                ):
                    _install_parakeet_server_unlocked(
                        backend,
                        journal_path,
                        attempt_status=attempt_status,
                        fingerprint=fingerprint,
                    )
            if force or readiness.proof["model"]["status"] == "missing-or-mismatched":
                _install_model_unlocked(
                    journal_path,
                    attempt_status=attempt_status,
                    fingerprint=fingerprint,
                )

        final_readiness = inspect_readiness(journal_path)
        current = assert_install_attempt_current(
            attempt_status, journal_path=journal_path
        )
        if final_readiness.ready:
            return _write_parakeet_status(
                transition_state(current, new_state="installed"),
                journal_path,
            )
        return _write_parakeet_status(
            transition_state(
                current,
                new_state="failed",
                error=final_readiness.reason_code,
                error_code=final_readiness.reason_code,
            ),
            journal_path,
        )
    except Exception as exc:
        try:
            current = assert_install_attempt_current(
                attempt_status, journal_path=journal_path
            )
            _write_parakeet_status(
                transition_state(
                    current,
                    new_state="failed",
                    error=str(exc),
                    error_code=getattr(exc, "reason_code", None),
                ),
                journal_path,
            )
        except Exception:
            pass
        raise
    finally:
        if owned_lease:
            lease.release()


def _proof_result_payload(result: Any) -> dict[str, Any]:
    return {
        "status": result.status,
        "reason_code": result.reason_code,
        "cache_hit": bool(getattr(result, "cache_hit", False)),
    }


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


def inspect_readiness(journal_path: str | Path | None = None) -> ReadinessOutcome:
    status = _read_parakeet_status(journal_path)
    artifact_key = parakeet_server_artifact_key()
    cpu_path = binary_path("cpu", journal_path)
    vulkan_path = binary_path("vulkan", journal_path)
    gguf_path = model_path(journal_path)
    cpu_proof = prove_manifest(
        artifact_manifest_path(binary_install_dir("cpu", journal_path)),
        provider=PARAKEET_PROVIDER_NAME,
        pin_identity=_binary_pin_identity(artifact_key, "cpu"),
        journal_path=journal_path,
    )
    vulkan_proof = prove_manifest(
        artifact_manifest_path(binary_install_dir("vulkan", journal_path)),
        provider=PARAKEET_PROVIDER_NAME,
        pin_identity=_binary_pin_identity(artifact_key, "vulkan"),
        journal_path=journal_path,
    )
    model_proof = prove_manifest(
        artifact_manifest_path(model_dir(journal_path)),
        provider=PARAKEET_PROVIDER_NAME,
        pin_identity=_model_pin_identity(),
        journal_path=journal_path,
    )
    cpu_payload = _proof_result_payload(cpu_proof)
    vulkan_payload = _proof_result_payload(vulkan_proof)
    model_payload = _proof_result_payload(model_proof)
    readiness_status, reason_code = _combined_artifact_status(
        cpu_payload, vulkan_payload, model_payload
    )
    binary_status, binary_reason_code = _combined_artifact_status(
        cpu_payload, vulkan_payload
    )
    return ReadinessOutcome(
        provider=PARAKEET_PROVIDER_NAME,
        status=readiness_status,  # type: ignore[arg-type]
        reason_code=reason_code,
        target={
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
        host={},
        artifacts={
            "binary_installed": cpu_proof.ready and vulkan_proof.ready,
            "binary_cpu_installed": cpu_proof.ready,
            "binary_vulkan_installed": vulkan_proof.ready,
            "model_installed": model_proof.ready,
            "binary_path_cpu": str(cpu_path),
            "binary_path_vulkan": str(vulkan_path),
            "model_path": str(gguf_path),
        },
        proof={
            "binary": {
                "status": binary_status,
                "reason_code": binary_reason_code,
                "cache_hit": cpu_payload["cache_hit"] and vulkan_payload["cache_hit"],
            },
            "binary_cpu": cpu_payload,
            "binary_vulkan": vulkan_payload,
            "model": model_payload,
        },
    )


def ensure_artifacts_installed(
    backend: str,
    *,
    journal_path: str | Path | None = None,
) -> tuple[Path, Path]:
    backend = _validate_backend(backend)
    readiness = inspect_readiness(journal_path)
    if not readiness.artifacts["binary_installed"]:
        raise ParakeetProviderError(
            "binary_missing", "Parakeet server binaries are not installed."
        )
    if not readiness.artifacts["model_installed"]:
        raise ParakeetProviderError("model_missing", "Parakeet model is not installed.")
    return Path(readiness.artifacts[f"binary_path_{backend}"]), Path(
        readiness.artifacts["model_path"]
    )


__all__ = [
    "PARAKEET_MODEL_SPEC",
    "PARAKEET_PROVIDER_NAME",
    "PARAKEET_SERVER_PINS",
    "ParakeetModelSpec",
    "ParakeetProviderError",
    "binary_install_dir",
    "binary_path",
    "cache_root",
    "ensure_artifacts_installed",
    "inspect_readiness",
    "install_hint",
    "install_model",
    "install_parakeet",
    "install_parakeet_server",
    "model_dir",
    "model_path",
    "parakeet_server_artifact_key",
    "probe_binary_runnable",
]
