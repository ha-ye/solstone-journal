# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only installation invariant for the native speakers-analyze helper."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Literal

from packaging import tags

from solstone.apps.speakers.encoder_config import (
    OVERLAP_DETECTOR_SHA256,
    WESPEAKER_MODEL_SHA256,
)
from solstone.think import probe
from solstone.think.journal_io import MalformedPolicy, atomic_replace, read_json
from solstone.think.journal_io.lease import (
    FileLease,
    acquire_file_lease,
    probe_file_lease_held,
)
from solstone.think.model_assets import (
    ModelsDistributionUnavailable,
    resolve_pyannote_segmentation_model,
    resolve_wespeaker_model,
)
from solstone.think.utils import get_journal

HELPER_DIST_NAME = "solstone-core-speakers-analyze"
MODELS_DIST_NAME = "solstone-journal-models"
HELPER_BINARY_NAME = "solstone-core-speakers-analyze"
ROOT_DIST_NAME = "solstone"
INSTALL_GENERATION_SCHEMA = "solstone.speakers_analyze.install_generation.v1"
PROOF_KEY_SCHEMA = "solstone.speakers_analyze.install_proof_key.v1"
GENERATION_ENV_KEY = "SOL_SPEAKERS_ANALYZE_INSTALL_GENERATION_ID"
GENERATION_MODE = 0o600

SPEAKERS_ANALYZE_REPAIR_TEXT = (
    "Repair: reinstall the journal host stack with solstone-journal, or "
    "solstone-journal-cuda on NVIDIA hosts, and restart the journal."
)

SpeakersAnalyzeInstallationStatus = Literal[
    "ok",
    "metadata-missing",
    "metadata-version-mismatch",
    "platform-unsupported",
    "helper-missing",
    "helper-not-executable",
    "asset-missing",
    "asset-digest-mismatch",
]


@dataclass(frozen=True)
class SpeakersAnalyzeInstallationResult:
    status: SpeakersAnalyzeInstallationStatus
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def message(self) -> str:
        if self.ok:
            return "speakers-analyze installation ready"
        detail = f": {self.detail}" if self.detail else ""
        return (
            f"Speakers-analyze installation is incomplete "
            f"({self.status}{detail}). {SPEAKERS_ANALYZE_REPAIR_TEXT}"
        )


@dataclass(frozen=True)
class SpeakersAnalyzeGeneration:
    generation_id: str
    lease: FileLease

    def release(self) -> None:
        self.lease.release()

    def __enter__(self) -> SpeakersAnalyzeGeneration:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def speakers_analyze_path_for_executable(executable: str | Path | None = None) -> Path:
    return Path(executable or sys.executable).with_name(HELPER_BINARY_NAME)


def _packaging_platform_tags() -> set[str]:
    return {tag.platform for tag in tags.sys_tags()}


def runtime_has_speakers_analyze_wheel_coverage(
    *,
    platform_reader: Callable[
        [], probe.CorePlatform
    ] = probe.current_solstone_core_platform,
    platform_tag_reader: Callable[[], set[str]] = _packaging_platform_tags,
) -> bool:
    platform_tuple = platform_reader()
    if platform_tuple not in probe.SOLSTONE_CORE_SPEAKERS_ANALYZE_COVERED_PLATFORMS:
        return False
    expected_platforms = probe.SOLSTONE_CORE_SPEAKERS_ANALYZE_PLATFORM_TAGS.get(
        platform_tuple
    )
    if expected_platforms is None:
        return False
    return not set(expected_platforms.split(".")).isdisjoint(platform_tag_reader())


def check_speakers_analyze_installation(
    *,
    journal_path: str | Path | None = None,
    executable: str | Path | None = None,
    version_reader: Callable[[str], str] = distribution_version,
    platform_reader: Callable[
        [], probe.CorePlatform
    ] = probe.current_solstone_core_platform,
    platform_tag_reader: Callable[[], set[str]] = _packaging_platform_tags,
    executable_predicate: Callable[[Path], bool] = lambda path: os.access(
        path, os.X_OK
    ),
    digest: bool = True,
    generation_id: str | None = None,
) -> SpeakersAnalyzeInstallationResult:
    cheap = _cheap_installation_result(
        executable=executable,
        version_reader=version_reader,
        platform_reader=platform_reader,
        platform_tag_reader=platform_tag_reader,
        executable_predicate=executable_predicate,
    )
    if not cheap.ok or not digest:
        return cheap

    proof_key = _installation_proof_key(
        executable=executable,
        version_reader=version_reader,
        platform_reader=platform_reader,
    )
    if _generation_proves_digest(
        journal_path=journal_path,
        generation_id=generation_id or os.getenv(GENERATION_ENV_KEY),
        proof_key=proof_key,
    ):
        return SpeakersAnalyzeInstallationResult("ok")
    return _digest_assets(proof_key)


def begin_speakers_analyze_generation(
    *,
    journal_path: str | Path | None = None,
    executable: str | Path | None = None,
    version_reader: Callable[[str], str] = distribution_version,
    platform_reader: Callable[
        [], probe.CorePlatform
    ] = probe.current_solstone_core_platform,
) -> SpeakersAnalyzeGeneration:
    root = _journal_root(journal_path)
    lease = acquire_file_lease(
        _generation_lease_path(root), attempts=1, retry_max_seconds=0
    )
    if lease is None:
        raise RuntimeError("speakers-analyze generation lease is already held")
    cheap = _cheap_installation_result(
        executable=executable,
        version_reader=version_reader,
        platform_reader=platform_reader,
        platform_tag_reader=_packaging_platform_tags,
        executable_predicate=lambda path: os.access(path, os.X_OK),
    )
    if not cheap.ok:
        lease.release()
        raise RuntimeError(cheap.message)
    proof_key = _installation_proof_key(
        executable=executable,
        version_reader=version_reader,
        platform_reader=platform_reader,
    )
    result, observed_assets = _validated_asset_digests(proof_key)
    if not result.ok:
        lease.release()
        raise RuntimeError(result.message)
    generation_id = uuid.uuid4().hex
    record = {
        "schema": INSTALL_GENERATION_SCHEMA,
        "generation_id": generation_id,
        "created_at": _now_iso(),
        "verified_at": _now_iso(),
        "proof_key": proof_key,
        "assets": observed_assets,
        "helper": proof_key["helper"],
        "packages": proof_key["packages"],
        "platform": proof_key["platform"],
    }
    _write_generation_record(root, record)
    os.environ[GENERATION_ENV_KEY] = generation_id
    return SpeakersAnalyzeGeneration(generation_id=generation_id, lease=lease)


def _cheap_installation_result(
    *,
    executable: str | Path | None,
    version_reader: Callable[[str], str],
    platform_reader: Callable[[], probe.CorePlatform],
    platform_tag_reader: Callable[[], set[str]],
    executable_predicate: Callable[[Path], bool],
) -> SpeakersAnalyzeInstallationResult:
    if not runtime_has_speakers_analyze_wheel_coverage(
        platform_reader=platform_reader,
        platform_tag_reader=platform_tag_reader,
    ):
        system, machine = platform_reader()
        return SpeakersAnalyzeInstallationResult(
            "platform-unsupported", f"{system}/{machine} is not covered"
        )

    versions = _read_versions(version_reader)
    if isinstance(versions, SpeakersAnalyzeInstallationResult):
        return versions
    root_version, helper_version, _models_version = versions
    if helper_version != root_version:
        return SpeakersAnalyzeInstallationResult(
            "metadata-version-mismatch",
            f"{HELPER_DIST_NAME} is {helper_version} but {ROOT_DIST_NAME} is {root_version}",
        )

    helper_path = speakers_analyze_path_for_executable(executable)
    if not helper_path.exists():
        return SpeakersAnalyzeInstallationResult("helper-missing", str(helper_path))
    if not executable_predicate(helper_path):
        return SpeakersAnalyzeInstallationResult(
            "helper-not-executable", str(helper_path)
        )

    try:
        required_assets = _required_assets()
    except ModelsDistributionUnavailable as exc:
        return SpeakersAnalyzeInstallationResult("asset-missing", str(exc))

    for role, path, _expected in required_assets:
        if not path.exists():
            return SpeakersAnalyzeInstallationResult("asset-missing", f"{role}: {path}")
        try:
            path.stat()
        except OSError as exc:
            return SpeakersAnalyzeInstallationResult(
                "asset-missing", f"{role}: {path} ({exc})"
            )
    return SpeakersAnalyzeInstallationResult("ok")


def _read_versions(
    version_reader: Callable[[str], str],
) -> tuple[str, str, str] | SpeakersAnalyzeInstallationResult:
    try:
        root_version = version_reader(ROOT_DIST_NAME)
    except PackageNotFoundError:
        return SpeakersAnalyzeInstallationResult("metadata-missing", ROOT_DIST_NAME)
    try:
        helper_version = version_reader(HELPER_DIST_NAME)
    except PackageNotFoundError:
        return SpeakersAnalyzeInstallationResult("metadata-missing", HELPER_DIST_NAME)
    try:
        models_version = version_reader(MODELS_DIST_NAME)
    except PackageNotFoundError:
        return SpeakersAnalyzeInstallationResult("metadata-missing", MODELS_DIST_NAME)
    return root_version, helper_version, models_version


def _installation_proof_key(
    *,
    executable: str | Path | None,
    version_reader: Callable[[str], str],
    platform_reader: Callable[[], probe.CorePlatform],
) -> dict[str, object]:
    versions = _read_versions(version_reader)
    if isinstance(versions, SpeakersAnalyzeInstallationResult):
        raise RuntimeError(versions.message)
    root_version, helper_version, models_version = versions
    helper_path = speakers_analyze_path_for_executable(executable)
    helper_stat = helper_path.stat()
    try:
        required_assets = _required_assets()
    except ModelsDistributionUnavailable as exc:
        raise RuntimeError(str(exc)) from exc

    assets = []
    for role, path, expected_sha256 in required_assets:
        stat = path.stat()
        assets.append(
            {
                "role": role,
                "path": str(path),
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
                "expected_sha256": expected_sha256,
            }
        )
    system, machine = platform_reader()
    return {
        "schema": PROOF_KEY_SCHEMA,
        "platform": {"system": system, "machine": machine},
        "packages": {
            ROOT_DIST_NAME: root_version,
            HELPER_DIST_NAME: helper_version,
            MODELS_DIST_NAME: models_version,
        },
        "helper": {
            "path": str(helper_path),
            "size": int(helper_stat.st_size),
            "mtime_ns": int(helper_stat.st_mtime_ns),
            "mode": int(helper_stat.st_mode & 0o777),
            "executable": bool(os.access(helper_path, os.X_OK)),
        },
        "assets": assets,
    }


def _digest_assets(proof_key: dict[str, object]) -> SpeakersAnalyzeInstallationResult:
    result, _observed_assets = _validated_asset_digests(proof_key)
    return result


def _validated_asset_digests(
    proof_key: dict[str, object],
) -> tuple[SpeakersAnalyzeInstallationResult, list[dict[str, object]]]:
    observed_assets: list[dict[str, object]] = []
    for asset in proof_key["assets"]:  # type: ignore[index]
        assert isinstance(asset, dict)
        path = Path(str(asset["path"]))
        expected_sha256 = str(asset["expected_sha256"])
        try:
            observed_sha256 = _sha256_file(path)
        except OSError as exc:
            return (
                SpeakersAnalyzeInstallationResult(
                    "asset-missing", f"{asset['role']}: {path} ({exc})"
                ),
                observed_assets,
            )
        observed = dict(asset)
        observed["observed_sha256"] = observed_sha256
        observed["observed_bytes"] = int(path.stat().st_size)
        observed_assets.append(observed)
        if observed_sha256 != expected_sha256:
            return (
                SpeakersAnalyzeInstallationResult(
                    "asset-digest-mismatch", f"{asset['role']}: {path}"
                ),
                observed_assets,
            )
    return SpeakersAnalyzeInstallationResult("ok"), observed_assets


def _generation_proves_digest(
    *,
    journal_path: str | Path | None,
    generation_id: str | None,
    proof_key: dict[str, object],
) -> bool:
    if not generation_id:
        return False
    root = _journal_root(journal_path)
    if not probe_file_lease_held(_generation_lease_path(root)):
        return False
    raw = read_json(
        _generation_record_path(root),
        on_error=MalformedPolicy.WARN_AND_SKIP,
        default={},
    )
    if not isinstance(raw, dict):
        return False
    if raw.get("schema") != INSTALL_GENERATION_SCHEMA:
        return False
    if raw.get("generation_id") != generation_id:
        return False
    if raw.get("proof_key") != proof_key:
        return False
    assets = raw.get("assets")
    if not isinstance(assets, list):
        return False
    for asset in assets:
        if not isinstance(asset, dict):
            return False
        if asset.get("observed_sha256") != asset.get("expected_sha256"):
            return False
    return True


def _required_assets() -> tuple[tuple[str, Path, str], ...]:
    return (
        ("wespeaker", resolve_wespeaker_model(), WESPEAKER_MODEL_SHA256),
        ("pyannote", resolve_pyannote_segmentation_model(), OVERLAP_DETECTOR_SHA256),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _journal_root(journal_path: str | Path | None) -> Path:
    return Path(journal_path) if journal_path is not None else Path(get_journal())


def _generation_record_path(root: Path) -> Path:
    return root / "health" / "speakers-analyze" / "install-generation.json"


def _generation_lease_path(root: Path) -> Path:
    return root / "health" / "speakers-analyze" / "install-generation.lock"


def _write_generation_record(root: Path, record: dict[str, object]) -> None:
    atomic_replace(
        _generation_record_path(root),
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        mode=GENERATION_MODE,
    )


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "GENERATION_ENV_KEY",
    "HELPER_BINARY_NAME",
    "HELPER_DIST_NAME",
    "MODELS_DIST_NAME",
    "ROOT_DIST_NAME",
    "SPEAKERS_ANALYZE_REPAIR_TEXT",
    "SpeakersAnalyzeGeneration",
    "SpeakersAnalyzeInstallationResult",
    "begin_speakers_analyze_generation",
    "check_speakers_analyze_installation",
    "runtime_has_speakers_analyze_wheel_coverage",
    "speakers_analyze_path_for_executable",
]
