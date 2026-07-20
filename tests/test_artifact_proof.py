# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from solstone.think.providers import artifact_proof
from solstone.think.providers.artifact_proof import (
    MANIFEST_NAME,
    artifact_manifest_path,
    build_manifest,
    mlx_snapshot_manifest_path,
    mlx_variant_manifest_path,
    proof_cache_path,
    prove_manifest,
    publish_staged_tree,
    write_manifest,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _skip_if_root_chmod_is_ignored() -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses chmod permission checks")


def _manifest(root: Path, *, pin: str = "pin") -> dict:
    payload = b"artifact"
    artifact = root / "bin" / "thing"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(payload)
    artifact.chmod(0o755)
    return build_manifest(
        provider="local",
        unit="runtime-vulkan",
        target_fingerprint_sha256="target",
        source={"pin_identity": {"pin": pin}},
        inventory=[
            {
                "relative_path": "bin/thing",
                "role": "runtime_binary",
                "size": len(payload),
                "sha256": _sha(payload),
            }
        ],
    )


def test_manifest_write_mode_and_proof_cache_zero_rehash(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    root = tmp_path / "artifact"
    manifest = _manifest(root)
    manifest_path = artifact_manifest_path(root)
    write_manifest(manifest_path, manifest)

    first = prove_manifest(
        manifest_path,
        provider="local",
        pin_identity={"pin": "pin"},
    )
    calls: list[Path] = []
    real_hash = artifact_proof._sha256_file

    def spy(path: Path) -> str:
        calls.append(path)
        return real_hash(path)

    monkeypatch.setattr(artifact_proof, "_sha256_file", spy)
    second = prove_manifest(
        manifest_path,
        provider="local",
        pin_identity={"pin": "pin"},
    )

    assert first.ready
    assert second.ready
    assert second.cache_hit is True
    assert calls == []
    assert (manifest_path.stat().st_mode & 0o777) == 0o600
    assert (proof_cache_path("local").stat().st_mode & 0o777) == 0o600


def test_manifest_proof_cache_write_failure_is_nonfatal(
    tmp_path, monkeypatch, caplog
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    root = tmp_path / "artifact"
    manifest = _manifest(root)
    manifest_path = artifact_manifest_path(root)
    write_manifest(manifest_path, manifest)

    def fail_cache_replace(path: Path, *_args, **_kwargs) -> None:
        if path == proof_cache_path("local"):
            raise PermissionError("cache read-only")
        raise AssertionError(f"unexpected replace path: {path}")

    monkeypatch.setattr(artifact_proof, "atomic_replace", fail_cache_replace)

    result = prove_manifest(
        manifest_path,
        provider="local",
        pin_identity={"pin": "pin"},
    )

    assert result.ready
    assert result.cache_hit is False
    assert "could not update provider artifact proof cache" in caplog.text


def test_manifest_cache_invalidates_on_chmod_after_cached_success(
    tmp_path, monkeypatch
) -> None:
    _skip_if_root_chmod_is_ignored()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    root = tmp_path / "artifact"
    manifest = _manifest(root)
    manifest_path = artifact_manifest_path(root)
    write_manifest(manifest_path, manifest)
    assert prove_manifest(
        manifest_path,
        provider="local",
        pin_identity={"pin": "pin"},
    ).ready
    artifact = root / "bin" / "thing"
    artifact.chmod(0o000)

    try:
        result = prove_manifest(
            manifest_path,
            provider="local",
            pin_identity={"pin": "pin"},
        )
    finally:
        artifact.chmod(0o755)

    assert result.status == "proof-unavailable"
    assert result.reason_code == "inventory_member_io_error"
    assert result.cache_hit is False


def test_manifest_cache_invalidates_on_touch_after_cached_success(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    root = tmp_path / "artifact"
    manifest = _manifest(root)
    manifest_path = artifact_manifest_path(root)
    write_manifest(manifest_path, manifest)
    first = prove_manifest(
        manifest_path,
        provider="local",
        pin_identity={"pin": "pin"},
    )
    assert first.ready
    calls: list[Path] = []
    real_hash = artifact_proof._sha256_file

    def spy(path: Path) -> str:
        calls.append(path)
        return real_hash(path)

    artifact = root / "bin" / "thing"
    stat_result = artifact.stat()
    os.utime(
        artifact,
        ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000_000),
    )
    monkeypatch.setattr(artifact_proof, "_sha256_file", spy)

    second = prove_manifest(
        manifest_path,
        provider="local",
        pin_identity={"pin": "pin"},
    )

    assert second.ready
    assert second.cache_hit is False
    assert artifact in calls


def test_manifest_cache_invalidates_on_pin_change_after_cached_success(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    root = tmp_path / "artifact"
    manifest = _manifest(root)
    manifest_path = artifact_manifest_path(root)
    write_manifest(manifest_path, manifest)
    assert prove_manifest(
        manifest_path,
        provider="local",
        pin_identity={"pin": "pin"},
    ).ready
    calls: list[Path] = []

    def spy(path: Path) -> str:
        calls.append(path)
        return "unused"

    monkeypatch.setattr(artifact_proof, "_sha256_file", spy)

    result = prove_manifest(
        manifest_path,
        provider="local",
        pin_identity={"pin": "new"},
    )

    assert result.status == "missing-or-mismatched"
    assert result.reason_code == "manifest_pin_mismatch"
    assert calls == []


def test_tree_without_manifest_is_repair_needed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    root = tmp_path / "artifact"
    (root / "thing").parent.mkdir(parents=True)
    (root / "thing").write_text("x", encoding="utf-8")

    result = prove_manifest(
        artifact_manifest_path(root),
        provider="local",
        pin_identity={"pin": "pin"},
    )

    assert result.status == "missing-or-mismatched"
    assert result.reason_code == "manifest_missing"
    repair_attempt_permitted = result.status == "missing-or-mismatched"
    assert repair_attempt_permitted is True


def test_corrupt_manifest_json_is_repair_needed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    root = tmp_path / "artifact"
    root.mkdir()
    manifest_path = artifact_manifest_path(root)
    manifest_path.write_text("{not-json", encoding="utf-8")
    manifest_path.chmod(0o600)

    result = prove_manifest(
        manifest_path,
        provider="local",
        pin_identity={"pin": "pin"},
    )

    assert result.status == "missing-or-mismatched"
    assert result.reason_code == "manifest_malformed"


def test_manifest_missing_expected_hash_is_repair_needed(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    root = tmp_path / "artifact"
    manifest = _manifest(root)
    manifest["inventory"][0].pop("sha256")
    manifest_path = artifact_manifest_path(root)
    write_manifest(manifest_path, manifest)

    result = prove_manifest(
        manifest_path,
        provider="local",
        pin_identity={"pin": "pin"},
    )

    assert result.status == "missing-or-mismatched"
    assert result.reason_code == "expected_hash_unavailable"


def test_required_file_unreadable_is_proof_unavailable_and_tree_untouched(
    tmp_path,
    monkeypatch,
) -> None:
    _skip_if_root_chmod_is_ignored()
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    root = tmp_path / "artifact"
    manifest = _manifest(root)
    manifest_path = artifact_manifest_path(root)
    write_manifest(manifest_path, manifest)
    artifact = root / "bin" / "thing"
    artifact.chmod(0o000)

    try:
        result = prove_manifest(
            manifest_path,
            provider="local",
            pin_identity={"pin": "pin"},
        )
        assert result.status == "proof-unavailable"
        assert result.reason_code == "inventory_member_io_error"
        assert artifact.exists()
        assert artifact.stat().st_size == len(b"artifact")
    finally:
        artifact.chmod(0o755)

    assert artifact.read_bytes() == b"artifact"


def test_inventory_deletion_and_corruption_are_not_ready(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    root = tmp_path / "artifact"
    manifest = _manifest(root)
    manifest_path = artifact_manifest_path(root)
    write_manifest(manifest_path, manifest)
    (root / "bin" / "thing").unlink()

    missing = prove_manifest(
        manifest_path,
        provider="local",
        pin_identity={"pin": "pin"},
    )
    assert missing.status == "missing-or-mismatched"

    (root / "bin" / "thing").write_bytes(b"changed")
    corrupt = prove_manifest(
        manifest_path,
        provider="local",
        pin_identity={"pin": "pin"},
    )
    assert corrupt.status == "missing-or-mismatched"
    assert corrupt.reason_code == "sha256_mismatch"


def test_pin_mismatch_is_not_ready(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    root = tmp_path / "artifact"
    manifest = _manifest(root, pin="old")
    manifest_path = artifact_manifest_path(root)
    write_manifest(manifest_path, manifest)

    result = prove_manifest(
        manifest_path,
        provider="local",
        pin_identity={"pin": "new"},
    )

    assert result.status == "missing-or-mismatched"
    assert result.reason_code == "manifest_pin_mismatch"


def test_mlx_manifests_are_solstone_side(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path / "journal"))
    hf_root = tmp_path / "hf-cache"

    snapshot = mlx_snapshot_manifest_path(
        "mlx-community/Qwen",
        "rev",
    )
    variant = mlx_variant_manifest_path(
        "mlx-community/Qwen",
        "rev",
    )

    assert str(snapshot).startswith(str(tmp_path / "journal" / "cache"))
    assert str(variant).startswith(str(tmp_path / "journal" / "cache"))
    assert not str(snapshot).startswith(str(hf_root))
    assert not str(variant).startswith(str(hf_root))


def test_publish_staged_tree_restores_prior_tree_on_replace_failure(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "old").write_text("old", encoding="utf-8")
    (target / MANIFEST_NAME).write_text("old", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "new").write_text("new", encoding="utf-8")
    (staging / MANIFEST_NAME).write_text("new", encoding="utf-8")
    real_replace = Path.replace

    def fail_staging_replace(self: Path, target_path: Path):
        if self == staging:
            raise OSError("promote failed")
        return real_replace(self, target_path)

    monkeypatch.setattr(Path, "replace", fail_staging_replace)

    with pytest.raises(OSError):
        publish_staged_tree(staging, target)

    assert (target / "old").read_text(encoding="utf-8") == "old"
    assert not (target / "new").exists()
