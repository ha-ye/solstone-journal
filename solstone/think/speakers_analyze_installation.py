# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only installation invariant for the native speakers-analyze helper."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
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
    BorrowedFileLease,
    FileLease,
    acquire_file_lease,
    adopt_inherited_file_lease_fd,
    read_file_lease_fd,
    set_file_lease_offset_token,
)
from solstone.think.model_assets import (
    ModelsDistributionUnavailable,
    resolve_pyannote_segmentation_model,
    resolve_wespeaker_model,
)
from solstone.think.utils import get_journal, is_source_checkout

HELPER_DIST_NAME = "solstone-core-speakers-analyze"
MODELS_DIST_NAME = "solstone-journal-models"
HELPER_BINARY_NAME = "solstone-core-speakers-analyze"
ROOT_DIST_NAME = "solstone"
INSTALL_GENERATION_SCHEMA = "solstone.speakers_analyze.install_generation.v1"
PROOF_KEY_SCHEMA = "solstone.speakers_analyze.install_proof_key.v1"
GENERATION_ENV_KEY = "SOL_SPEAKERS_ANALYZE_INSTALL_GENERATION_ID"
GENERATION_FD_ENV_KEY = "SOL_SPEAKERS_ANALYZE_INSTALL_GENERATION_FD"
GENERATION_TOKEN_ENV_KEY = "SOL_SPEAKERS_ANALYZE_INSTALL_GENERATION_TOKEN"
GENERATION_MODE = 0o600
GENERATION_FD_MIN = 3
GENERATION_FD_MAX = 1_048_576
GENERATION_TOKEN_MAX = (1 << 31) - 1

PACKAGED_SPEAKERS_ANALYZE_REPAIR_TEXT = (
    "Repair: reinstall the journal host stack with solstone-journal, or "
    "solstone-journal-cuda on NVIDIA hosts, and restart the journal."
)
SOURCE_CHECKOUT_SPEAKERS_ANALYZE_REPAIR_TEXT = (
    "Repair: run make speakers-analyze-helper to install the published "
    "speakers-analyze helper, then run make install to finish source-checkout "
    "setup."
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
            f"({self.status}{detail}). {speakers_analyze_repair_text()}"
        )


@dataclass(frozen=True)
class SpeakersAnalyzeGeneration:
    generation_id: str
    lease: FileLease | BorrowedFileLease

    def release(self) -> None:
        if isinstance(self.lease, FileLease) and (
            os.environ.get(GENERATION_ENV_KEY) == self.generation_id
        ):
            os.environ.pop(GENERATION_ENV_KEY, None)
            os.environ.pop(GENERATION_FD_ENV_KEY, None)
            os.environ.pop(GENERATION_TOKEN_ENV_KEY, None)
        self.lease.release()

    def __enter__(self) -> SpeakersAnalyzeGeneration:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def speakers_analyze_path_for_executable(executable: str | Path | None = None) -> Path:
    return Path(executable or sys.executable).with_name(HELPER_BINARY_NAME)


def speakers_analyze_repair_text() -> str:
    if is_source_checkout():
        return SOURCE_CHECKOUT_SPEAKERS_ANALYZE_REPAIR_TEXT
    return PACKAGED_SPEAKERS_ANALYZE_REPAIR_TEXT


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


def enter_speakers_analyze_generation(
    *,
    journal_path: str | Path | None = None,
    executable: str | Path | None = None,
    version_reader: Callable[[str], str] = distribution_version,
    platform_reader: Callable[
        [], probe.CorePlatform
    ] = probe.current_solstone_core_platform,
) -> SpeakersAnalyzeGeneration:
    root = _journal_root(journal_path)
    borrowed = _borrow_speakers_analyze_generation(
        root=root,
        executable=executable,
        version_reader=version_reader,
        platform_reader=platform_reader,
    )
    if borrowed is not None:
        return borrowed

    lease = acquire_file_lease(
        _generation_lease_path(root), attempts=1, retry_max_seconds=0
    )
    if lease is None:
        raise RuntimeError("speakers-analyze generation lease is already held")
    try:
        cheap = _cheap_installation_result(
            executable=executable,
            version_reader=version_reader,
            platform_reader=platform_reader,
            platform_tag_reader=_packaging_platform_tags,
            executable_predicate=lambda path: os.access(path, os.X_OK),
        )
        if not cheap.ok:
            raise RuntimeError(cheap.message)
        proof_key = _installation_proof_key(
            executable=executable,
            version_reader=version_reader,
            platform_reader=platform_reader,
        )
        result, observed_assets = _validated_asset_digests(proof_key)
        if not result.ok:
            raise RuntimeError(result.message)
        generation_id = uuid.uuid4().hex
        token = secrets.randbelow(GENERATION_TOKEN_MAX) + 1
        set_file_lease_offset_token(lease, token, _generation_lease_path(root))
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
        os.environ[GENERATION_FD_ENV_KEY] = str(
            read_file_lease_fd(lease, _generation_lease_path(root))
        )
        os.environ[GENERATION_TOKEN_ENV_KEY] = str(token)
        return SpeakersAnalyzeGeneration(generation_id=generation_id, lease=lease)
    except BaseException:
        _clear_generation_env()
        lease.release()
        raise


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
    candidate = _inherited_generation_candidate()
    if candidate is None:
        return False
    fd, token = candidate
    borrowed = adopt_inherited_file_lease_fd(
        _generation_lease_path(root), fd=fd, token=token
    )
    if borrowed is None:
        return False
    try:
        return _generation_record_proves_digest(
            root=root,
            generation_id=generation_id,
            proof_key=proof_key,
        )
    finally:
        borrowed.release()


def _borrow_speakers_analyze_generation(
    *,
    root: Path,
    executable: str | Path | None,
    version_reader: Callable[[str], str],
    platform_reader: Callable[[], probe.CorePlatform],
) -> SpeakersAnalyzeGeneration | None:
    generation_id = os.environ.get(GENERATION_ENV_KEY)
    if not any(
        os.environ.get(key)
        for key in (
            GENERATION_ENV_KEY,
            GENERATION_FD_ENV_KEY,
            GENERATION_TOKEN_ENV_KEY,
        )
    ):
        return None
    candidate = _inherited_generation_candidate()
    if generation_id is None or candidate is None:
        _reject_generation_borrow(
            candidate_fd=_parse_generation_fd(os.environ.get(GENERATION_FD_ENV_KEY))
        )
        return None
    fd, token = candidate
    borrowed = adopt_inherited_file_lease_fd(
        _generation_lease_path(root), fd=fd, token=token
    )
    if borrowed is None:
        _reject_generation_borrow(candidate_fd=fd)
        return None
    try:
        proof_key = _installation_proof_key(
            executable=executable,
            version_reader=version_reader,
            platform_reader=platform_reader,
        )
        if _generation_record_proves_digest(
            root=root,
            generation_id=generation_id,
            proof_key=proof_key,
        ):
            return SpeakersAnalyzeGeneration(
                generation_id=generation_id, lease=borrowed
            )
    except BaseException:
        borrowed.release()
        raise
    borrowed.release()
    _reject_generation_borrow(candidate_fd=fd)
    return None


def _generation_record_proves_digest(
    *,
    root: Path,
    generation_id: str,
    proof_key: dict[str, object],
) -> bool:
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


def _inherited_generation_candidate() -> tuple[int, int] | None:
    fd = _parse_generation_fd(os.environ.get(GENERATION_FD_ENV_KEY))
    token = _parse_generation_token(os.environ.get(GENERATION_TOKEN_ENV_KEY))
    if fd is None or token is None:
        return None
    try:
        os.fstat(fd)
    except OSError:
        return None
    return fd, token


def _parse_generation_fd(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        fd = int(value)
    except (TypeError, ValueError):
        return None
    if fd < GENERATION_FD_MIN or fd > GENERATION_FD_MAX:
        return None
    return fd


def _parse_generation_token(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        token = int(value)
    except (TypeError, ValueError):
        return None
    if token <= 0 or token > GENERATION_TOKEN_MAX:
        return None
    return token


def _reject_generation_borrow(candidate_fd: int | None = None) -> None:
    if candidate_fd is not None:
        try:
            os.close(candidate_fd)
        except OSError:
            pass
    _clear_generation_env()


def _clear_generation_env() -> None:
    os.environ.pop(GENERATION_ENV_KEY, None)
    os.environ.pop(GENERATION_FD_ENV_KEY, None)
    os.environ.pop(GENERATION_TOKEN_ENV_KEY, None)


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
    "GENERATION_FD_ENV_KEY",
    "GENERATION_TOKEN_ENV_KEY",
    "HELPER_BINARY_NAME",
    "HELPER_DIST_NAME",
    "MODELS_DIST_NAME",
    "PACKAGED_SPEAKERS_ANALYZE_REPAIR_TEXT",
    "ROOT_DIST_NAME",
    "SOURCE_CHECKOUT_SPEAKERS_ANALYZE_REPAIR_TEXT",
    "SpeakersAnalyzeGeneration",
    "SpeakersAnalyzeInstallationResult",
    "check_speakers_analyze_installation",
    "enter_speakers_analyze_generation",
    "runtime_has_speakers_analyze_wheel_coverage",
    "speakers_analyze_path_for_executable",
    "speakers_analyze_repair_text",
]
