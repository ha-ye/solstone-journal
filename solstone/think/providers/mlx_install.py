# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""MLX local backend install helpers."""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import platform
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solstone.think.models import GEMMA4_26B_A4B_4BIT, QWEN_35_9B
from solstone.think.providers.artifact_proof import (
    ReadinessOutcome,
    build_manifest,
    mlx_snapshot_manifest_path,
    mlx_variant_manifest_path,
    prove_manifest,
    write_manifest,
)
from solstone.think.providers.install_lease import (
    InstallLease,
    acquire_install_lease,
    assert_install_lease_owned,
)
from solstone.think.providers.install_state import (
    InstallStatus,
    assert_install_attempt_current,
    begin_or_replace_install_attempt,
    canonical_fingerprint,
    fingerprint_sha256,
    read_install_status,
    transition_state,
    write_install_status,
)
from solstone.think.providers.memory import (
    MLX_AVAILABLE_FLOOR_BYTES,
    assess_memory,
)

LOG = logging.getLogger(__name__)
MLX_SOFT_TOKEN_BUDGET = 1120
_GEMMA4_MIN_POSITION_EMBEDDING_SIZE = 10240
_LOCAL_NAME = "local"
_HASH_CHUNK_SIZE = 1024 * 1024
_REWRITTEN_VARIANT_FILES = frozenset({"config.json", "processor_config.json"})


@dataclass(frozen=True)
class MLXModelSpec:
    name: str
    repo: str
    revision: str
    size_bytes: int


_MLX_MODEL_REGISTRY: dict[str, MLXModelSpec] = {
    QWEN_35_9B: MLXModelSpec(
        name=QWEN_35_9B,
        repo="mlx-community/Qwen3.5-9B-MLX-8bit",
        revision="84f7c2deea248d8df56240f88102def51c7ed5d6",
        size_bytes=10453446077,
    ),
    GEMMA4_26B_A4B_4BIT: MLXModelSpec(
        name=GEMMA4_26B_A4B_4BIT,
        repo="mlx-community/gemma-4-26b-a4b-it-4bit",
        revision="efbeee6e582ebfd06abc9d65e90839c4b5d2116b",
        # Per-model RAM floors are future work tied to hardware-keyed model
        # selection; today's active VLM floor is shared.
        size_bytes=15641241224,
    ),
}


class MLXInstallUnavailableError(RuntimeError):
    """Raised when the host cannot install or run the requested MLX model."""


class MLXVerificationError(RuntimeError):
    """Raised when a downloaded MLX snapshot fails sha256 verification."""


def _read_status() -> InstallStatus:
    return read_install_status(name=_LOCAL_NAME)


def _write_status(status: InstallStatus) -> InstallStatus:
    write_install_status(status)
    return status


def _platform_unsupported_reason() -> str | None:
    if platform.system() != "Darwin":
        return "not running on macOS"
    if platform.machine() != "arm64":
        return "not running on Apple Silicon"
    return None


def is_mlx_platform_supported() -> bool:
    """True when the host is Apple Silicon macOS. Does not import mlx_vlm."""
    return _platform_unsupported_reason() is None


def _check_platform_and_package() -> tuple[bool, str]:
    platform_reason = _platform_unsupported_reason()
    if platform_reason is not None:
        return False, platform_reason

    try:
        importlib.import_module("mlx_vlm")
    except ImportError:
        return False, "mlx-vlm package not installed"

    return True, ""


def resolve_model_spec(model_id: str | None = None) -> MLXModelSpec:
    selected = model_id or QWEN_35_9B
    spec = _MLX_MODEL_REGISTRY.get(selected)
    if spec is None:
        raise ValueError(
            f"unknown MLX model: {selected!r}; known: {sorted(_MLX_MODEL_REGISTRY)}"
        )
    return spec


def _pin_identity(spec: MLXModelSpec) -> dict[str, Any]:
    return {
        "unit": "mlx-snapshot",
        "model_id": spec.name,
        "repo": spec.repo,
        "revision": spec.revision,
        "soft_token_budget": (
            MLX_SOFT_TOKEN_BUDGET if spec.name == GEMMA4_26B_A4B_4BIT else None
        ),
    }


def target_fingerprint(model_id: str | None = None) -> dict[str, Any]:
    spec = resolve_model_spec(model_id)
    return {
        "provider": _LOCAL_NAME,
        "runtime": "mlx",
        "model_pin": _pin_identity(spec),
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


def snapshot_dir_for_spec(spec: MLXModelSpec) -> Path:
    from huggingface_hub import constants
    from huggingface_hub.file_download import repo_folder_name

    repo_folder = repo_folder_name(repo_id=spec.repo, repo_type="model")
    return Path(constants.HF_HUB_CACHE) / repo_folder / "snapshots" / spec.revision


def variant_dir_for_snapshot(snapshot_dir: Path) -> Path:
    return (
        snapshot_dir.parent
        / f"{snapshot_dir.name}-solstone-budget{MLX_SOFT_TOKEN_BUDGET}"
    )


def _safetensors_paths(snapshot_dir: Path) -> list[str]:
    index_path = snapshot_dir / "model.safetensors.index.json"
    data = json.loads(index_path.read_text(encoding="utf-8"))
    weight_map = data.get("weight_map")
    if not isinstance(weight_map, dict):
        raise ValueError("model.safetensors.index.json missing weight_map")
    paths = sorted({str(path) for path in weight_map.values() if str(path)})
    if not paths:
        raise ValueError("model.safetensors.index.json has no safetensors paths")
    return paths


def _snapshot_present(snapshot_dir: Path) -> bool:
    index_path = snapshot_dir / "model.safetensors.index.json"
    if not snapshot_dir.is_dir() or not index_path.is_file():
        return False
    try:
        for rel_path in _safetensors_paths(snapshot_dir):
            file_path = snapshot_dir / rel_path
            if not file_path.is_file() or file_path.stat().st_size <= 0:
                return False
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return True


def _remote_safetensors_metadata(
    spec: MLXModelSpec, paths: list[str]
) -> dict[str, tuple[str, int]]:
    import huggingface_hub

    wanted = set(paths)
    found: dict[str, tuple[str, int]] = {}
    api = huggingface_hub.HfApi()
    for entry in api.list_repo_tree(
        repo_id=spec.repo,
        revision=spec.revision,
        repo_type="model",
        recursive=True,
    ):
        if not isinstance(entry, huggingface_hub.RepoFile) or entry.path not in wanted:
            continue
        if entry.lfs is None:
            raise MLXVerificationError(f"missing LFS sha256 for {entry.path}")
        found[entry.path] = (entry.lfs.sha256, int(entry.lfs.size))
    missing = sorted(wanted - set(found))
    if missing:
        raise MLXVerificationError(f"missing published sha256 for {missing[0]}")
    return found


def validate_snapshot_sha256(
    spec: MLXModelSpec, snapshot_dir: Path
) -> dict[str, tuple[str, int]]:
    safetensors_paths = _safetensors_paths(snapshot_dir)
    metadata = _remote_safetensors_metadata(spec, safetensors_paths)

    for rel_path in safetensors_paths:
        expected_sha, _expected_size = metadata[rel_path]
        file_path = snapshot_dir / rel_path
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
        actual_sha = digest.hexdigest()
        if actual_sha != expected_sha:
            raise MLXVerificationError(f"sha256 mismatch for {rel_path}")
    return metadata


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _variant_pin_identity(spec: MLXModelSpec) -> dict[str, Any]:
    identity = dict(_pin_identity(spec))
    identity["unit"] = "mlx-variant"
    identity["variant"] = f"solstone-budget{MLX_SOFT_TOKEN_BUDGET}"
    return identity


def _manifest_inventory_for_tree(
    root: Path,
    *,
    role_prefix: str,
    known_hashes: dict[str, tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(root).as_posix()
        known = known_hashes.get(rel_path) if known_hashes is not None else None
        if known is None:
            digest = _sha256_file(path)
            size = path.stat().st_size
        else:
            digest, size = known
        inventory.append(
            {
                "relative_path": rel_path,
                "role": role_prefix,
                "size": size,
                "sha256": digest,
            }
        )
    return inventory


def _write_snapshot_manifest(
    spec: MLXModelSpec,
    snapshot_dir: Path,
    *,
    metadata: dict[str, tuple[str, int]],
    attempt_status: InstallStatus | None,
    fingerprint: dict[str, Any],
) -> None:
    manifest = build_manifest(
        provider=_LOCAL_NAME,
        unit="mlx-snapshot",
        target_fingerprint_sha256=_manifest_target_sha(attempt_status, fingerprint),
        source={"pin_identity": _pin_identity(spec)},
        inventory=_manifest_inventory_for_tree(
            snapshot_dir, role_prefix="snapshot_file", known_hashes=metadata
        ),
        external_root=snapshot_dir,
        attempt_id=attempt_status["attempt_id"] if attempt_status else None,
    )
    write_manifest(mlx_snapshot_manifest_path(spec.repo, spec.revision), manifest)


def _write_variant_manifest(
    spec: MLXModelSpec,
    variant_dir: Path,
    *,
    attempt_status: InstallStatus | None,
    fingerprint: dict[str, Any],
) -> None:
    manifest = build_manifest(
        provider=_LOCAL_NAME,
        unit="mlx-variant",
        target_fingerprint_sha256=_manifest_target_sha(attempt_status, fingerprint),
        source={"pin_identity": _variant_pin_identity(spec)},
        inventory=_manifest_inventory_for_tree(variant_dir, role_prefix="variant_file"),
        external_root=variant_dir,
        attempt_id=attempt_status["attempt_id"] if attempt_status else None,
    )
    write_manifest(mlx_variant_manifest_path(spec.repo, spec.revision), manifest)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_gemma4_position_embedding(config: dict[str, Any]) -> None:
    vision_config = config.get("vision_config")
    if not isinstance(vision_config, dict):
        raise ValueError("config.json missing vision_config")
    position_embedding_size = vision_config.get("position_embedding_size")
    if position_embedding_size is None:
        raise ValueError("config.json missing vision_config.position_embedding_size")
    if position_embedding_size < _GEMMA4_MIN_POSITION_EMBEDDING_SIZE:
        raise ValueError(
            "config.json vision_config.position_embedding_size must be >= "
            f"{_GEMMA4_MIN_POSITION_EMBEDDING_SIZE}; actual {position_embedding_size}"
        )


def _rewrite_config(source: Path, target: Path) -> None:
    data = _read_json(source)
    _validate_gemma4_position_embedding(data)
    vision_config = data["vision_config"]
    vision_config["default_output_length"] = MLX_SOFT_TOKEN_BUDGET
    _write_json(target, data)


def _rewrite_processor_config(source: Path, target: Path) -> None:
    data = _read_json(source)
    image_processor = data.get("image_processor")
    if not isinstance(image_processor, dict):
        raise ValueError("processor_config.json missing image_processor")
    image_processor["max_soft_tokens"] = MLX_SOFT_TOKEN_BUDGET
    image_processor["image_seq_length"] = MLX_SOFT_TOKEN_BUDGET
    if "image_seq_length" in data:
        data["image_seq_length"] = MLX_SOFT_TOKEN_BUDGET
    _write_json(target, data)


def _gemma4_variant_valid(variant_dir: Path) -> bool:
    try:
        config = _read_json(variant_dir / "config.json")
        processor_config = _read_json(variant_dir / "processor_config.json")
        _validate_gemma4_position_embedding(config)
        image_processor = processor_config["image_processor"]
        return (
            config["vision_config"]["default_output_length"] == MLX_SOFT_TOKEN_BUDGET
            and image_processor["max_soft_tokens"] == MLX_SOFT_TOKEN_BUDGET
            and image_processor["image_seq_length"] == MLX_SOFT_TOKEN_BUDGET
            and processor_config["image_seq_length"] == MLX_SOFT_TOKEN_BUDGET
        )
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return False


def _symlink_snapshot_entry(source: Path, target: Path) -> None:
    import os

    relative_source = os.path.relpath(source, target.parent)
    target.symlink_to(relative_source)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def create_gemma4_variant(snapshot_dir: Path) -> Path:
    variant_dir = variant_dir_for_snapshot(snapshot_dir)
    if variant_dir.exists() and _gemma4_variant_valid(variant_dir):
        return variant_dir

    config_source = snapshot_dir / "config.json"
    processor_source = snapshot_dir / "processor_config.json"
    if not config_source.is_file():
        raise FileNotFoundError(config_source)
    if not processor_source.is_file():
        raise FileNotFoundError(processor_source)

    tmp_dir = variant_dir.parent / f".{variant_dir.name}.{uuid.uuid4().hex}.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    try:
        for source in snapshot_dir.rglob("*"):
            rel_path = source.relative_to(snapshot_dir)
            target = tmp_dir / rel_path
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            rel_name = rel_path.as_posix()
            if rel_name in _REWRITTEN_VARIANT_FILES:
                if rel_name == "config.json":
                    _rewrite_config(source, target)
                else:
                    _rewrite_processor_config(source, target)
            else:
                _symlink_snapshot_entry(source, target)

        if not _gemma4_variant_valid(tmp_dir):
            raise ValueError("generated Gemma4 variant failed validation")
        if variant_dir.exists():
            _remove_path(variant_dir)
        tmp_dir.replace(variant_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return variant_dir


def _artifact_presence(spec: MLXModelSpec) -> dict[str, Any]:
    snapshot_dir = snapshot_dir_for_spec(spec)
    snapshot_installed = _snapshot_present(snapshot_dir)
    variant_dir = variant_dir_for_snapshot(snapshot_dir)
    variant_installed = True
    runtime_dir = snapshot_dir
    if spec.name == GEMMA4_26B_A4B_4BIT:
        variant_installed = _gemma4_variant_valid(variant_dir)
        runtime_dir = variant_dir
    model_installed = snapshot_installed and variant_installed
    return {
        "model_installed": model_installed,
        "snapshot_installed": snapshot_installed,
        "variant_installed": variant_installed,
        "snapshot_dir": snapshot_dir,
        "variant_dir": variant_dir if spec.name == GEMMA4_26B_A4B_4BIT else None,
        "runtime_dir": runtime_dir,
    }


def inspect_artifacts(model_id: str | None = None) -> dict[str, Any]:
    spec = resolve_model_spec(model_id)
    presence = _artifact_presence(spec)
    return {
        "model_installed": presence["model_installed"],
        "snapshot_installed": presence["snapshot_installed"],
        "variant_installed": presence["variant_installed"],
        "model_id": spec.name,
        "snapshot_dir": str(presence["snapshot_dir"]),
        "variant_dir": (
            str(presence["variant_dir"])
            if presence["variant_dir"] is not None
            else None
        ),
        "runtime_dir": str(presence["runtime_dir"]),
    }


def inspect_readiness(model_id: str | None = None) -> ReadinessOutcome:
    status = _read_status()
    artifacts = inspect_artifacts(model_id)
    spec = resolve_model_spec(str(artifacts["model_id"]))
    verdict = assess_memory(MLX_AVAILABLE_FLOOR_BYTES, block_below_floor=True)
    snapshot_proof = prove_manifest(
        mlx_snapshot_manifest_path(spec.repo, spec.revision),
        provider=_LOCAL_NAME,
        pin_identity=_pin_identity(spec),
    )
    if spec.name == GEMMA4_26B_A4B_4BIT:
        variant_proof = prove_manifest(
            mlx_variant_manifest_path(spec.repo, spec.revision),
            provider=_LOCAL_NAME,
            pin_identity=_variant_pin_identity(spec),
        )
    else:
        variant_proof = None
    platform_supported = is_mlx_platform_supported()
    package_available = _check_platform_and_package()[0]
    ram_sufficient = verdict.severity != "blocked"
    proof_payloads = [
        {
            "status": snapshot_proof.status,
            "reason_code": snapshot_proof.reason_code,
            "cache_hit": snapshot_proof.cache_hit,
        }
    ]
    if variant_proof is not None:
        proof_payloads.append(
            {
                "status": variant_proof.status,
                "reason_code": variant_proof.reason_code,
                "cache_hit": variant_proof.cache_hit,
            }
        )
    if any(proof["status"] == "proof-unavailable" for proof in proof_payloads):
        readiness_status = "proof-unavailable"
        reason_code = next(
            str(proof["reason_code"])
            for proof in proof_payloads
            if proof["status"] == "proof-unavailable"
        )
    elif any(proof["status"] == "missing-or-mismatched" for proof in proof_payloads):
        readiness_status = "missing-or-mismatched"
        reason_code = next(
            str(proof["reason_code"])
            for proof in proof_payloads
            if proof["status"] == "missing-or-mismatched"
        )
    elif not platform_supported:
        readiness_status = "host-ineligible"
        reason_code = "platform_unsupported"
    elif not package_available:
        readiness_status = "host-ineligible"
        reason_code = "package_unavailable"
    elif not ram_sufficient:
        readiness_status = "host-ineligible"
        reason_code = "ram_insufficient"
    else:
        readiness_status = "ready"
        reason_code = "ready"

    model_installed = snapshot_proof.ready and (
        variant_proof is None or variant_proof.ready
    )
    return ReadinessOutcome(
        provider=_LOCAL_NAME,
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
            "platform_supported": platform_supported,
            "package_available": package_available,
        },
        artifacts={
            **artifacts,
            "model_installed": model_installed,
            "snapshot_installed": snapshot_proof.ready,
            "variant_installed": variant_proof is None or variant_proof.ready,
        },
        proof={
            "snapshot": proof_payloads[0],
            "variant": proof_payloads[1] if len(proof_payloads) > 1 else None,
        },
    )


def install_local_mlx(
    model_id: str = QWEN_35_9B,
    *,
    lease: InstallLease | None = None,
    attempt_status: InstallStatus | None = None,
) -> InstallStatus:
    import huggingface_hub

    fingerprint = target_fingerprint(model_id)
    owned_lease = lease is None
    if lease is None:
        lease = acquire_install_lease(_LOCAL_NAME)
        if lease is None:
            raise MLXInstallUnavailableError(
                "Local provider install is already running."
            )
    lease = assert_install_lease_owned(lease, _LOCAL_NAME)
    try:
        if attempt_status is None:
            attempt_status = begin_or_replace_install_attempt(
                _LOCAL_NAME,
                fingerprint,
                initial_state="resolving",
                owner={"entry": "install_local_mlx"},
            )
        spec = resolve_model_spec(model_id)
        readiness = inspect_readiness(model_id)
        if readiness.status in {"proof-unavailable", "host-ineligible"}:
            current = assert_install_attempt_current(attempt_status)
            return _write_status(
                transition_state(
                    current,
                    new_state="failed",
                    error=readiness.reason_code,
                    error_code=readiness.reason_code,
                )
            )

        from solstone.think.providers import fit_report

        if not readiness.ready:
            report = fit_report.build_mlx_fit_report(spec.name)
            rendered = fit_report.render_fit_report(report)
            if report.overall == "blocked":
                raise MLXInstallUnavailableError(rendered)
            if report.overall == "warning":
                LOG.warning("MLX provider host fit warning:\n%s", rendered)

            _write_status(transition_state(_read_status(), new_state="downloading"))
            assert_install_lease_owned(lease, _LOCAL_NAME)
            snapshot_dir = Path(
                huggingface_hub.snapshot_download(
                    repo_id=spec.repo,
                    revision=spec.revision,
                )
            )

            _write_status(transition_state(_read_status(), new_state="verifying"))
            metadata = validate_snapshot_sha256(spec, snapshot_dir)
            assert_install_lease_owned(lease, _LOCAL_NAME)
            assert_install_attempt_current(attempt_status)
            _write_snapshot_manifest(
                spec,
                snapshot_dir,
                metadata=metadata,
                attempt_status=attempt_status,
                fingerprint=fingerprint,
            )

            _write_status(transition_state(_read_status(), new_state="installing"))
            if spec.name == GEMMA4_26B_A4B_4BIT:
                assert_install_lease_owned(lease, _LOCAL_NAME)
                variant_dir = create_gemma4_variant(snapshot_dir)
                assert_install_lease_owned(lease, _LOCAL_NAME)
                assert_install_attempt_current(attempt_status)
                _write_variant_manifest(
                    spec,
                    variant_dir,
                    attempt_status=attempt_status,
                    fingerprint=fingerprint,
                )

        final_readiness = inspect_readiness(model_id)
        assert_install_lease_owned(lease, _LOCAL_NAME)
        current = assert_install_attempt_current(attempt_status)
        if final_readiness.ready:
            return _write_status(transition_state(current, new_state="installed"))
        return _write_status(
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
            _write_status(
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


__all__ = [
    "GEMMA4_26B_A4B_4BIT",
    "MLXInstallUnavailableError",
    "MLXModelSpec",
    "MLXVerificationError",
    "MLX_SOFT_TOKEN_BUDGET",
    "QWEN_35_9B",
    "_GEMMA4_MIN_POSITION_EMBEDDING_SIZE",
    "_MLX_MODEL_REGISTRY",
    "create_gemma4_variant",
    "inspect_readiness",
    "install_local_mlx",
    "is_mlx_platform_supported",
    "resolve_model_spec",
    "snapshot_dir_for_spec",
    "target_fingerprint",
    "validate_snapshot_sha256",
    "variant_dir_for_snapshot",
]
