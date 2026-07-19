# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Provider artifact manifests, typed readiness outcomes, and proof cache."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from solstone.think.journal_io.atomic import atomic_replace
from solstone.think.journal_io.locking import hold_lock
from solstone.think.providers.install_state import (
    PROVIDERS,
    canonical_fingerprint,
    fingerprint_sha256,
)
from solstone.think.utils import get_journal

ReadinessStatus = Literal[
    "ready",
    "missing-or-mismatched",
    "proof-unavailable",
    "host-ineligible",
]
MANIFEST_NAME = ".solstone-provider-manifest.json"
MANIFEST_SCHEMA_VERSION = 1
PROOF_CACHE_SCHEMA_VERSION = 1
PRIVATE_MODE = 0o600
EXPECTED_HASH_UNAVAILABLE = "expected_hash_unavailable"
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReadinessOutcome:
    provider: str
    status: ReadinessStatus
    reason_code: str
    target: dict[str, Any]
    install: dict[str, Any]
    host: dict[str, Any]
    artifacts: dict[str, Any]
    proof: dict[str, Any]

    @property
    def ready(self) -> bool:
        return self.status == "ready"


@dataclass(frozen=True)
class ProofResult:
    status: ReadinessStatus
    reason_code: str
    cache_hit: bool = False

    @property
    def ready(self) -> bool:
        return self.status == "ready"


class ProofUnavailableError(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def artifact_manifest_path(artifact_dir: Path) -> Path:
    return Path(artifact_dir) / MANIFEST_NAME


def mlx_manifest_dir(
    repo: str,
    revision: str,
    *,
    journal_path: str | Path | None = None,
) -> Path:
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return root / "cache" / "providers" / "local" / "mlx" / _safe_repo(repo) / revision


def mlx_snapshot_manifest_path(
    repo: str,
    revision: str,
    *,
    journal_path: str | Path | None = None,
) -> Path:
    return mlx_manifest_dir(repo, revision, journal_path=journal_path) / (
        "snapshot.manifest.json"
    )


def mlx_variant_manifest_path(
    repo: str,
    revision: str,
    *,
    journal_path: str | Path | None = None,
) -> Path:
    return mlx_manifest_dir(repo, revision, journal_path=journal_path) / (
        "variant-solstone-budget1120.manifest.json"
    )


def proof_cache_path(
    provider: str,
    *,
    journal_path: str | Path | None = None,
) -> Path:
    _validate_provider(provider)
    root = Path(journal_path) if journal_path is not None else Path(get_journal())
    return root / "health" / "providers" / f"{provider}.proof-cache.json"


def build_manifest(
    *,
    provider: str,
    unit: str,
    target_fingerprint_sha256: str,
    source: dict[str, Any],
    inventory: list[dict[str, Any]],
    external_root: Path | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    _validate_provider(provider)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "provider": provider,
        "unit": unit,
        "target_fingerprint_sha256": target_fingerprint_sha256,
        "created_by_attempt_id": attempt_id,
        "external_root": str(external_root) if external_root is not None else None,
        "source": _normalize_source(source),
        "inventory": sorted(
            [_normalize_inventory_entry(entry) for entry in inventory],
            key=lambda entry: (
                str(entry.get("role", "")),
                str(entry.get("relative_path", "")),
            ),
        ),
    }


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    _validate_manifest(manifest)
    atomic_replace(
        path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        mode=PRIVATE_MODE,
    )


def read_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("provider artifact manifest must be an object")
    _validate_manifest(data)
    return data


def publish_staged_tree(staging: Path, target_dir: Path) -> None:
    """Atomically publish a staged tree with aside rollback."""
    staging = Path(staging)
    target_dir = Path(target_dir)
    if not (staging / MANIFEST_NAME).is_file():
        raise FileNotFoundError(staging / MANIFEST_NAME)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    aside: Path | None = None
    published = False
    try:
        if target_dir.exists():
            aside = Path(tempfile.mkdtemp(dir=target_dir.parent))
            target_dir.replace(aside)
        try:
            staging.replace(target_dir)
            published = True
        except Exception:
            _restore_aside(aside, target_dir)
            raise
        if aside is not None:
            shutil.rmtree(aside, ignore_errors=True)
            aside = None
    finally:
        if not published and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if not published and aside is not None and aside.exists():
            shutil.rmtree(aside, ignore_errors=True)


def prove_manifest(
    manifest_path: Path,
    *,
    provider: str,
    pin_identity: dict[str, Any],
    journal_path: str | Path | None = None,
) -> ProofResult:
    """Prove a manifest and inventory, using an affirmative cache."""
    _validate_provider(provider)
    manifest_path = Path(manifest_path)
    try:
        manifest_stat = _required_file_stat(
            manifest_path,
            io_reason_code="manifest_io_error",
        )
    except FileNotFoundError:
        # A legacy tree without a manifest is repair-needed, not undecidable:
        # startup migrations exit successfully and later POST bootstrap may repair it.
        return ProofResult("missing-or-mismatched", "manifest_missing")
    except ValueError:
        return ProofResult("missing-or-mismatched", "manifest_malformed")
    except ProofUnavailableError as exc:
        return ProofResult("proof-unavailable", exc.reason_code)
    except OSError as exc:
        _log_unavailable("manifest stat failed", exc)
        return ProofResult("proof-unavailable", "manifest_io_error")
    try:
        manifest = read_manifest(manifest_path)
    except OSError as exc:
        _log_unavailable("manifest read failed", exc)
        return ProofResult("proof-unavailable", "manifest_io_error")
    except (json.JSONDecodeError, ValueError):
        return ProofResult("missing-or-mismatched", "manifest_malformed")
    if manifest["provider"] != provider:
        return ProofResult("missing-or-mismatched", "manifest_provider_mismatch")
    if _normalize_pin_identity(pin_identity) != manifest["source"].get("pin_identity"):
        return ProofResult("missing-or-mismatched", "manifest_pin_mismatch")

    root = _manifest_root(manifest_path, manifest)
    try:
        file_fingerprints = _inventory_file_fingerprints(root, manifest)
    except FileNotFoundError:
        return ProofResult("missing-or-mismatched", "inventory_member_missing")
    except ProofUnavailableError as exc:
        return ProofResult("proof-unavailable", exc.reason_code)
    except ValueError as exc:
        if exc.args == (EXPECTED_HASH_UNAVAILABLE,):
            return ProofResult("missing-or-mismatched", "expected_hash_unavailable")
        LOG.debug("provider artifact manifest inventory is defective: %s", exc)
        return ProofResult("missing-or-mismatched", "inventory_malformed")

    cache_path = proof_cache_path(provider, journal_path=journal_path)
    cache = _read_proof_cache(cache_path)
    manifest_identity = _stat_identity_from_result(manifest_path, manifest_stat)
    manifest_identity_key = canonical_fingerprint(manifest_identity)
    manifest_hash = cache.get("manifest_hashes", {}).get(manifest_identity_key)
    if not isinstance(manifest_hash, str):
        try:
            manifest_hash = _sha256_file(manifest_path)
        except OSError as exc:
            _log_unavailable("manifest hash failed", exc)
            return ProofResult("proof-unavailable", "manifest_io_error")

    proof_key = _proof_key(
        provider=provider,
        pin_identity=pin_identity,
        manifest_hash=manifest_hash,
        files=file_fingerprints,
    )
    if proof_key in cache.get("affirmative", {}):
        return ProofResult("ready", "ready", cache_hit=True)

    for file_info in file_fingerprints:
        expected = file_info["expected_sha256"]
        try:
            actual = _sha256_file(Path(file_info["path"]))
        except OSError as exc:
            _log_unavailable("inventory member hash failed", exc)
            return ProofResult("proof-unavailable", "inventory_member_io_error")
        if actual != expected:
            return ProofResult("missing-or-mismatched", "sha256_mismatch")

    _write_affirmative_cache(
        cache_path,
        proof_key=proof_key,
        manifest_identity_key=manifest_identity_key,
        manifest_hash=manifest_hash,
    )
    return ProofResult("ready", "ready", cache_hit=False)


def prove_cuda_sidecar(
    *,
    provider: str,
    image_ref: str,
    arch: str,
    wanted_files: list[str] | tuple[str, ...],
    target_dir: Path,
    pin_identity: dict[str, Any],
    verifier: Callable[[str, str, list[str] | tuple[str, ...], Path], bool],
    journal_path: str | Path | None = None,
) -> ProofResult:
    """Cache successful CUDA OCI sidecar verification."""
    _validate_provider(provider)
    target_dir = Path(target_dir)
    sidecar = target_dir / ".oci-install.json"
    try:
        sidecar_stat = _required_file_stat(
            sidecar,
            io_reason_code="cuda_sidecar_io_error",
        )
    except FileNotFoundError:
        return ProofResult("missing-or-mismatched", "cuda_sidecar_missing")
    except ValueError:
        return ProofResult("missing-or-mismatched", "cuda_sidecar_malformed")
    except ProofUnavailableError as exc:
        return ProofResult("proof-unavailable", exc.reason_code)
    try:
        record = json.loads(sidecar.read_text(encoding="utf-8"))
    except OSError as exc:
        _log_unavailable("CUDA sidecar read failed", exc)
        return ProofResult("proof-unavailable", "cuda_sidecar_io_error")
    except (json.JSONDecodeError, ValueError):
        return ProofResult("missing-or-mismatched", "cuda_sidecar_malformed")
    files = record.get("files")
    if record.get("image_ref") != image_ref or record.get("arch") != arch:
        return ProofResult("missing-or-mismatched", "cuda_sidecar_pin_mismatch")
    if not isinstance(files, dict):
        return ProofResult("missing-or-mismatched", "cuda_sidecar_malformed")

    file_fingerprints: list[dict[str, Any]] = []
    for name in wanted_files:
        expected = files.get(name)
        if not isinstance(expected, str):
            return ProofResult("missing-or-mismatched", "expected_hash_unavailable")
        path = target_dir / name
        try:
            stat_result = _required_file_stat(path)
        except FileNotFoundError:
            return ProofResult("missing-or-mismatched", "inventory_member_missing")
        except ProofUnavailableError as exc:
            return ProofResult("proof-unavailable", exc.reason_code)
        stat_info = _stat_identity_from_result(path, stat_result)
        stat_info.update(
            {
                "path": str(path),
                "relative_path": name,
                "expected_sha256": expected,
                "executable": os.access(path, os.X_OK),
            }
        )
        file_fingerprints.append(stat_info)

    cache_path = proof_cache_path(provider, journal_path=journal_path)
    cache = _read_proof_cache(cache_path)
    sidecar_identity = canonical_fingerprint(
        _stat_identity_from_result(sidecar, sidecar_stat)
    )
    proof_key = _proof_key(
        provider=provider,
        pin_identity={
            **pin_identity,
            "image_ref": image_ref,
            "arch": arch,
            "sidecar_identity": sidecar_identity,
        },
        manifest_hash="cuda-oci-sidecar",
        files=file_fingerprints,
    )
    if proof_key in cache.get("affirmative", {}):
        return ProofResult("ready", "ready", cache_hit=True)

    try:
        verified = verifier(image_ref, arch, wanted_files, target_dir)
    except OSError as exc:
        _log_unavailable("CUDA sidecar verifier could not run", exc)
        return ProofResult("proof-unavailable", "cuda_sidecar_verify_unavailable")
    if not verified:
        return ProofResult("missing-or-mismatched", "cuda_sidecar_verify_failed")
    _write_affirmative_cache(cache_path, proof_key=proof_key)
    return ProofResult("ready", "ready", cache_hit=False)


def prove_launch_probe(
    command: Sequence[str], *, timeout_s: float = 10.0
) -> ProofResult:
    """Classify a probe command: rejected is repair-needed; no verdict is unavailable."""
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log_unavailable("probe could not produce a verdict", exc)
        return ProofResult("proof-unavailable", "probe_unavailable")
    if completed.returncode == 0:
        return ProofResult("ready", "ready")
    return ProofResult("missing-or-mismatched", "probe_rejected")


def _inventory_file_fingerprints(
    root: Path,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for entry in manifest["inventory"]:
        rel_path = Path(entry["relative_path"])
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise ValueError("unsafe_relative_path")
        path = root / rel_path
        entry_type = entry.get("type", "file")
        if entry_type == "symlink":
            try:
                stat_result = path.lstat()
            except FileNotFoundError:
                raise FileNotFoundError(path)
            except OSError as exc:
                raise ProofUnavailableError("inventory_member_io_error") from exc
            if not stat.S_ISLNK(stat_result.st_mode):
                raise ValueError("inventory_member_type_mismatch")
            files.append(
                {
                    "path": str(path),
                    "relative_path": entry["relative_path"],
                    "type": "symlink",
                    "st_dev": stat_result.st_dev,
                    "st_ino": stat_result.st_ino,
                    "size": stat_result.st_size,
                    "st_mtime_ns": stat_result.st_mtime_ns,
                    "expected_sha256": entry.get("sha256") or "symlink-no-content",
                }
            )
            continue
        stat_result = _required_file_stat(path)
        expected = entry.get("sha256")
        if not isinstance(expected, str) or not expected:
            raise ValueError(EXPECTED_HASH_UNAVAILABLE)
        stat_info = _stat_identity_from_result(path, stat_result)
        stat_info.update(
            {
                "path": str(path),
                "relative_path": entry["relative_path"],
                "expected_sha256": expected,
                "executable": bool(stat_result.st_mode & 0o111),
            }
        )
        files.append(stat_info)
    return files


def _manifest_root(manifest_path: Path, manifest: dict[str, Any]) -> Path:
    external_root = manifest.get("external_root")
    if isinstance(external_root, str) and external_root:
        return Path(external_root)
    return manifest_path.parent


def _read_proof_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": PROOF_CACHE_SCHEMA_VERSION,
            "affirmative": {},
            "manifest_hashes": {},
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "schema_version": PROOF_CACHE_SCHEMA_VERSION,
            "affirmative": {},
            "manifest_hashes": {},
        }
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != PROOF_CACHE_SCHEMA_VERSION
    ):
        return {
            "schema_version": PROOF_CACHE_SCHEMA_VERSION,
            "affirmative": {},
            "manifest_hashes": {},
        }
    data.setdefault("affirmative", {})
    data.setdefault("manifest_hashes", {})
    return data


def _write_affirmative_cache(
    path: Path,
    *,
    proof_key: str,
    manifest_identity_key: str | None = None,
    manifest_hash: str | None = None,
) -> None:
    with hold_lock(path, mode=PRIVATE_MODE):
        cache = _read_proof_cache(path)
        cache.setdefault("affirmative", {})[proof_key] = {"ok": True}
        if manifest_identity_key is not None and manifest_hash is not None:
            cache.setdefault("manifest_hashes", {})[manifest_identity_key] = (
                manifest_hash
            )
        atomic_replace(
            path,
            json.dumps(cache, indent=2, sort_keys=True) + "\n",
            mode=PRIVATE_MODE,
        )


def _proof_key(
    *,
    provider: str,
    pin_identity: dict[str, Any],
    manifest_hash: str,
    files: list[dict[str, Any]],
) -> str:
    key_json = canonical_fingerprint(
        {
            "provider": provider,
            "pin_identity": pin_identity,
            "manifest_hash": manifest_hash,
            "files": [
                {
                    key: value
                    for key, value in file_info.items()
                    if key
                    in {
                        "relative_path",
                        "st_dev",
                        "st_ino",
                        "size",
                        "st_mtime_ns",
                        "executable",
                        "expected_sha256",
                    }
                }
                for file_info in files
            ],
        }
    )
    return fingerprint_sha256(key_json)


def _stat_identity(path: Path) -> dict[str, Any]:
    stat_result = Path(path).stat()
    return _stat_identity_from_result(path, stat_result)


def _required_file_stat(
    path: Path,
    *,
    io_reason_code: str = "inventory_member_io_error",
) -> os.stat_result:
    try:
        stat_result = Path(path).stat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        _log_unavailable("required artifact stat failed", exc)
        raise ProofUnavailableError(io_reason_code) from exc
    if not stat.S_ISREG(stat_result.st_mode):
        raise ValueError("required artifact is not a regular file")
    return stat_result


def _stat_identity_from_result(
    path: Path, stat_result: os.stat_result
) -> dict[str, Any]:
    return {
        "path": str(path),
        "st_dev": stat_result.st_dev,
        "st_ino": stat_result.st_ino,
        "size": stat_result.st_size,
        "st_mtime_ns": stat_result.st_mtime_ns,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_source(source: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(canonical_fingerprint(source))
    if "pin_identity" not in normalized:
        normalized["pin_identity"] = dict(normalized)
    return normalized


def _normalize_pin_identity(pin_identity: dict[str, Any]) -> dict[str, Any]:
    return json.loads(canonical_fingerprint(pin_identity))


def _normalize_inventory_entry(entry: dict[str, Any]) -> dict[str, Any]:
    relative_path = entry.get("relative_path")
    role = entry.get("role")
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("inventory entry requires relative_path")
    if not isinstance(role, str) or not role:
        raise ValueError("inventory entry requires role")
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("inventory relative_path must stay inside root")
    normalized = dict(entry)
    normalized["relative_path"] = path.as_posix()
    normalized.setdefault("type", "file")
    size = normalized.get("size")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int)):
        raise ValueError("inventory size must be an integer or null")
    sha = normalized.get("sha256")
    if sha is not None and not isinstance(sha, str):
        raise ValueError("inventory sha256 must be a string or null")
    return normalized


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported provider artifact manifest schema")
    _validate_provider(str(manifest.get("provider")))
    if not isinstance(manifest.get("unit"), str) or not manifest["unit"]:
        raise ValueError("manifest unit is required")
    if not isinstance(manifest.get("target_fingerprint_sha256"), str):
        raise ValueError("manifest target_fingerprint_sha256 is required")
    if not isinstance(manifest.get("source"), dict):
        raise ValueError("manifest source is required")
    inventory = manifest.get("inventory")
    if not isinstance(inventory, list):
        raise ValueError("manifest inventory must be a list")
    for entry in inventory:
        if not isinstance(entry, dict):
            raise ValueError("manifest inventory entries must be objects")
        _normalize_inventory_entry(entry)


def _restore_aside(aside: Path | None, target_dir: Path) -> None:
    if aside is None or not aside.exists() or target_dir.exists():
        return
    aside.replace(target_dir)


def _safe_repo(repo: str) -> str:
    return repo.replace("/", "__")


def _validate_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of: {sorted(PROVIDERS)}")


def _log_unavailable(message: str, exc: BaseException) -> None:
    LOG.debug("%s: %r", message, exc, exc_info=True)


__all__ = [
    "MANIFEST_NAME",
    "PRIVATE_MODE",
    "ProofResult",
    "ReadinessOutcome",
    "ReadinessStatus",
    "artifact_manifest_path",
    "build_manifest",
    "mlx_manifest_dir",
    "mlx_snapshot_manifest_path",
    "mlx_variant_manifest_path",
    "proof_cache_path",
    "prove_cuda_sidecar",
    "prove_launch_probe",
    "prove_manifest",
    "publish_staged_tree",
    "read_manifest",
    "write_manifest",
]
