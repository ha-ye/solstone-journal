# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared release-candidate fixture builders."""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import tarfile
import zipfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

import scripts.check_rust_release_manifest as checker
import scripts.record_macos_native_wheel as native
import scripts.release_candidate_driver as driver
import scripts.release_tool_pins as pins
from scripts.check_wheel_contents import (
    CORE_REQUIRED_SDIST_MEMBERS,
    CORE_SCRIPT_NAMES,
    CPU_TYPE_ARM64,
    ELF_MACHINE,
    EXPECTED_MODEL_SHA256,
)
from scripts.release_advisory_policy import PolicyRun
from scripts.release_build_host import BuildHostResult, SourceBundle
from scripts.release_install_smoke import (
    CORE_SMOKE_STDOUT,
    SCRUBBED_COMMAND_ENV,
    CommandResult,
    InstallObservation,
    build_install_proof,
    expected_distribution_entries,
    target_install_paths_from_ledger,
    write_install_proof,
)
from tests.helpers.release_wheel_fixtures import (
    minimal_elf,
    minimal_macho,
    write_core_wheel,
    write_platform_base_wheel,
)

SOURCE_COMMIT = "a" * 40
_CORE_LOCK_CONTENT = "fixture lock\n"
LOCK_SHA = hashlib.sha256(_CORE_LOCK_CONTENT.encode("utf-8")).hexdigest()
_LINUX_X86_CORE = minimal_elf(ELF_MACHINE["x86_64"])
_LINUX_AARCH64_CORE = minimal_elf(ELF_MACHINE["aarch64"])
MACOS_CORE = minimal_macho(CPU_TYPE_ARM64)
MACOS_HELPER = minimal_macho(CPU_TYPE_ARM64)
_ZIP_DATE_TIME = (2026, 7, 20, 12, 0, 0)

TombstoneMutation = Literal[
    "extra-key",
    "malformed-json",
    "missing-key",
    "non-mapping",
    "wrong-status",
    "wrong-version",
]


class _GuardedEnv(dict):
    def get(self, key: str, default: Any = None) -> Any:
        if key == "SOURCE_COMMIT":
            raise AssertionError("driver must not read SOURCE_COMMIT")
        return super().get(key, default)

    def __getitem__(self, key: str) -> Any:
        if key == "SOURCE_COMMIT":
            raise AssertionError("driver must not read SOURCE_COMMIT")
        return super().__getitem__(key)


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "core").mkdir(parents=True)
    (root / "packages" / "solstone-journal-models").mkdir(parents=True)
    (root / "core" / "Cargo.lock").write_text(_CORE_LOCK_CONTENT, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        f'[project]\nversion = "{checker._current_version()}"\n',
        encoding="utf-8",
    )
    (root / "packages" / "solstone-journal-models" / "pyproject.toml").write_text(
        '[project]\nname = "solstone-journal-models"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    return root


def env() -> _GuardedEnv:
    return _GuardedEnv(
        {
            "EXPECTED_RELEASE_COMMIT": SOURCE_COMMIT,
            "SOURCE_COMMIT": "b" * 40,
            "RELEASE_MODEL_PACKAGES": "exclude",
            "RELEASE_ADVISORY_SOURCE_NAME": "fixture",
            "RELEASE_ADVISORY_DB_URL": "ssh://example.test/db.git",
            "RELEASE_ADVISORY_DB_ROOT": "/advisory-db",
        }
    )


def _policy() -> PolicyRun:
    return PolicyRun(
        advisory_source_id="fixture",
        db_snapshot_basename="advisory-db-fixture00000000",
        db_commit="b" * 40,
        db_archive_sha256="c" * 64,
        advisory_count=1,
        advisory_acquired_at="2026-07-20T11:00:00Z",
        db_commit_timestamp="2026-07-19T12:00:00Z",
        policy_checked_at="2026-07-20T12:00:00Z",
        result="pass",
    )


def _wheel_metadata(name: str) -> tuple[str, str]:
    parts = name.removesuffix(".whl").split("-")
    distribution = parts[0]
    version = parts[1]
    return (
        f"{distribution}-{version}.dist-info/METADATA",
        f"Name: {distribution.replace('_', '-')}\nVersion: {version}\n",
    )


def _write_metadata_wheel(path: Path) -> None:
    metadata_name, metadata = _wheel_metadata(path.name)
    with zipfile.ZipFile(path, "w") as wheel:
        info = zipfile.ZipInfo(metadata_name, _ZIP_DATE_TIME)
        info.create_system = 3
        info.external_attr = 0o644 << 16
        wheel.writestr(info, metadata)


def _write_linux_core_wheels(dist_dir: Path) -> None:
    content_by_lane = {
        "linux-x86_64-musl": _LINUX_X86_CORE,
        "linux-aarch64-musl": _LINUX_AARCH64_CORE,
    }
    for artifact, (lane, _target) in checker.rust_artifact_targets().items():
        if lane not in content_by_lane:
            continue
        tag = artifact.split("-py3-none-", 1)[1].removesuffix(".whl")
        write_core_wheel(
            dist_dir,
            tag=tag,
            binary=content_by_lane[lane],
            version=checker._current_version(),
        )


def _write_core_sdist(path: Path) -> None:
    version = checker._current_version()
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gzipped:
            with tarfile.open(fileobj=gzipped, mode="w") as archive:
                for member in sorted(CORE_REQUIRED_SDIST_MEMBERS):
                    content = b"x"
                    info = tarfile.TarInfo(f"solstone_core-{version}/{member}")
                    info.size = len(content)
                    info.mtime = 0
                    info.mode = 0o644
                    archive.addfile(info, BytesIO(content))


def _write_models_wheel(path: Path) -> None:
    assets_dir = (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "solstone-journal-models"
        / "solstone_journal_models"
        / "assets"
    )
    with zipfile.ZipFile(path, "w") as wheel:
        metadata_name, metadata = _wheel_metadata(path.name)
        metadata_info = zipfile.ZipInfo(metadata_name, _ZIP_DATE_TIME)
        metadata_info.create_system = 3
        metadata_info.external_attr = 0o644 << 16
        wheel.writestr(metadata_info, metadata)
        for basename in sorted(EXPECTED_MODEL_SHA256):
            asset_info = zipfile.ZipInfo(
                f"solstone_journal_models/assets/{basename}", _ZIP_DATE_TIME
            )
            asset_info.create_system = 3
            asset_info.external_attr = 0o644 << 16
            wheel.writestr(asset_info, (assets_dir / basename).read_bytes())


def macos_wheel_names() -> tuple[str, str]:
    names = checker.expected_package_names(include_models=False)
    root = next(
        name
        for name in names
        if name.startswith("solstone-") and "macosx_14_0_arm64" in name
    )
    core = next(
        name
        for name in names
        if name.startswith("solstone_core-") and "macosx_14_0_arm64" in name
    )
    return root, core


def _facts(content: bytes) -> dict[str, Any]:
    return {
        "signed_binary_sha256": hashlib.sha256(content).hexdigest(),
        "signer_pinned": True,
        "team_pinned": True,
        "hardened_runtime": True,
        "trusted_timestamp": True,
        "notarization_status": "accepted",
        "tools": {
            "xcode": pins.MACOS_XCODE_PIN,
            "swift": pins.MACOS_SWIFT_FIXTURE_BANNER,
            "codesign": pins.MACOS_CODESIGN_PUBLIC_PIN,
            "notarytool": pins.MACOS_NOTARYTOOL_PIN,
        },
    }


def _core_facts(content: bytes) -> dict[str, Any]:
    return {"members": {name: _facts(content) for name in CORE_SCRIPT_NAMES}}


def write_macos_host_outputs(
    output_dir: Path,
    *,
    mutate: str | None = None,
) -> BuildHostResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    root_name, core_name = macos_wheel_names()
    root_wheel = output_dir / root_name
    core_wheel = output_dir / core_name
    if mutate == "wrong_tag":
        root_wheel = output_dir / root_name.replace(
            "macosx_14_0_arm64", "manylinux2014_x86_64"
        )
    root_bytes = MACOS_HELPER
    core_bytes = MACOS_CORE
    write_platform_base_wheel(
        root_wheel.parent,
        helper_binary=root_bytes,
        version=checker._current_version(),
    )
    if root_wheel.name != root_name:
        (output_dir / root_name).rename(root_wheel)
    write_core_wheel(
        core_wheel.parent,
        tag="macosx_14_0_arm64",
        binary=core_bytes,
        version=checker._current_version(),
    )
    root_record = native.build_macos_native_record(
        role="root",
        wheel_path=root_wheel,
        signing_facts=_facts(root_bytes),
        source_commit=SOURCE_COMMIT,
        core_lock_sha256=LOCK_SHA,
    )
    core_record = native.build_macos_native_record(
        role="core",
        wheel_path=core_wheel,
        signing_facts=_core_facts(core_bytes),
        source_commit=SOURCE_COMMIT,
        core_lock_sha256=LOCK_SHA,
    )
    if mutate == "record_role":
        root_record["role"] = "core"
    if mutate == "member":
        core_record["member"]["sha256"] = "0" * 64
    if mutate == "tool":
        core_record["tools"]["swift"] = "Apple Swift 6.3.3"
    if mutate == "signing":
        root_record["signing"]["team_pinned"] = False
    if mutate == "notary":
        root_record["notarization_status"] = "rejected"
    if mutate == "wheel_hash":
        root_record["wheel"]["sha256"] = "0" * 64
    root_record_path = output_dir / "macos-native-root.json"
    core_record_path = output_dir / "macos-native-core.json"
    if mutate == "record_paths_swapped":
        root_record_path, core_record_path = core_record_path, root_record_path
    root_record_path.write_text(
        json.dumps(root_record, sort_keys=True), encoding="utf-8"
    )
    core_record_path.write_text(
        json.dumps(core_record, sort_keys=True), encoding="utf-8"
    )
    return BuildHostResult(
        macos_wheels=(root_wheel, core_wheel),
        native_records=(root_record_path, core_record_path),
        tool_evidence=pins.fixture_presign_lane_tool_evidence("macos-arm64"),
    )


def _proof_observation(
    target: str,
    *,
    env_root: Path,
    candidate_dir: Path,
    install_paths: tuple[Path, ...],
    version: str,
) -> InstallObservation:
    (env_root / "bin").mkdir(parents=True, exist_ok=True)
    (env_root / "bin" / "python").write_bytes(b"python")
    for name in CORE_SCRIPT_NAMES:
        (env_root / "bin" / name).write_bytes(b"core")
    core_sha = {
        "linux-x86_64-musl": hashlib.sha256(_LINUX_X86_CORE).hexdigest(),
        "linux-aarch64-musl": hashlib.sha256(_LINUX_AARCH64_CORE).hexdigest(),
        "macos-arm64": hashlib.sha256(MACOS_CORE).hexdigest(),
    }[target]
    members = [
        {
            "name": name,
            "path": env_root / "bin" / name,
            "sha256": core_sha,
            "symlink": False,
        }
        for name in CORE_SCRIPT_NAMES
    ]
    if target == "macos-arm64":
        (env_root / "bin" / "parakeet-helper").write_bytes(b"helper")
        members.append(
            {
                "name": "parakeet-helper",
                "path": env_root / "bin" / "parakeet-helper",
                "sha256": hashlib.sha256(MACOS_HELPER).hexdigest(),
                "symlink": False,
            }
        )
    return InstallObservation(
        env_root=env_root,
        preexisting_distributions=(),
        install=CommandResult(
            argv=(
                str(env_root / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                *(str(path) for path in install_paths),
            ),
            exit_code=0,
            stdout="installed",
            env=SCRUBBED_COMMAND_ENV,
        ),
        installed_distributions=expected_distribution_entries(install_paths),
        installed_members=tuple(members),
        smoke={
            name: CommandResult(
                argv=(str(env_root / "bin" / name), "--version"),
                exit_code=0,
                stdout=f"{CORE_SMOKE_STDOUT[name]} {version}",
                env=SCRUBBED_COMMAND_ENV,
            )
            for name in CORE_SCRIPT_NAMES
        },
    )


def services(
    root: Path, *, native_mutation: str | None = None
) -> driver.CandidateServices:
    def clean_outputs(repo_root: Path, version: str) -> None:
        for relative in (
            "build",
            "dist",
            f"target/release-evidence/{version}",
            f"target/release-transfer/{version}",
            f"target/release-transfer/.{version}.source.bundle",
        ):
            path = repo_root / relative
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()

    def build_local_dist(repo_root: Path, include_models: bool) -> None:
        dist = repo_root / "dist"
        dist.mkdir(parents=True, exist_ok=True)
        for name in driver._expected_local_dist_names(include_models=include_models):
            path = dist / name
            if name.startswith("solstone_journal_models-") and name.endswith(".whl"):
                _write_models_wheel(path)
            elif name.endswith(".whl"):
                _write_metadata_wheel(path)
            elif name.startswith("solstone_core-") and name.endswith(".tar.gz"):
                _write_core_sdist(path)
            else:
                path.write_bytes(b"fixture package")
        _write_linux_core_wheels(repo_root / "dist")

    def create_source_bundle(
        _repo: Path, commit: str, output_path: Path
    ) -> SourceBundle:
        assert commit == SOURCE_COMMIT
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"bundle")
        return SourceBundle(
            path=output_path,
            source_commit=SOURCE_COMMIT,
            sha256=hashlib.sha256(b"bundle").hexdigest(),
            bytes=len(b"bundle"),
        )

    def build_host(
        source_bundle: SourceBundle, commit: str, output_dir: Path
    ) -> BuildHostResult:
        assert source_bundle.path.read_bytes() == b"bundle"
        assert source_bundle.source_commit == SOURCE_COMMIT
        assert source_bundle.sha256 == hashlib.sha256(b"bundle").hexdigest()
        assert source_bundle.bytes == len(b"bundle")
        assert commit == SOURCE_COMMIT
        return write_macos_host_outputs(output_dir, mutate=native_mutation)

    def run_proof(**kwargs: Any) -> Path:
        output_path = Path(kwargs["output_path"])
        target = str(kwargs["target"])
        install_paths = target_install_paths_from_ledger(
            kwargs["ledger_payload"],
            target=target,
            candidate_dir=Path(kwargs["candidate_dir"]),
        )
        proof = build_install_proof(
            **{key: value for key, value in kwargs.items() if key != "output_path"},
            observation=_proof_observation(
                target,
                env_root=root / "env" / target,
                candidate_dir=Path(kwargs["candidate_dir"]),
                install_paths=install_paths,
                version=str(kwargs["version"]),
            ),
            recorded_at=datetime(2026, 7, 20, 12, 30, tzinfo=UTC),
        )
        return write_install_proof(
            output_path,
            proof,
            target=target,
            version=str(kwargs["version"]),
            source_commit=str(kwargs["source_commit"]),
            core_lock_sha256=str(kwargs["core_lock_sha256"]),
            candidate_digest=str(kwargs["candidate_digest"]),
            ledger_sha256=str(kwargs["ledger_sha256"]),
            candidate_dir=Path(kwargs["candidate_dir"]),
            ledger_payload=kwargs["ledger_payload"],
        )

    def cleanup(paths: Sequence[Path]) -> None:
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()

    return driver.CandidateServices(
        git_head=lambda _repo: SOURCE_COMMIT,
        git_status=lambda _repo: "",
        core_lock_sha256=lambda _repo: LOCK_SHA,
        clean_outputs=clean_outputs,
        build_local_dist=build_local_dist,
        prepare_policy=lambda _repo, _env: _policy(),
        coordinator_tool_evidence=lambda: {
            lane: pins.fixture_lane_tool_evidence(lane)
            for lane in ("source", "linux-x86_64-musl", "linux-aarch64-musl")
        },
        create_source_bundle=create_source_bundle,
        build_host=build_host,
        cleanup_transients=cleanup,
        run_install_proof=run_proof,
        transaction_hook=lambda _point: None,
    )


def recover(root: Path) -> driver.CandidateReport:
    return driver.run_recover(
        root,
        version=checker._current_version(),
        source_commit=SOURCE_COMMIT,
    )


def _tombstone_payload(version: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": checker.CORE_UNSUPPORTED_TOMBSTONE_KIND,
        "project": checker.CORE_UNSUPPORTED_TOMBSTONE_PROJECT,
        "version": version,
        "status": checker.CORE_UNSUPPORTED_TOMBSTONE_STATUS,
        "supported_platform_triples": list(
            checker.CORE_UNSUPPORTED_TOMBSTONE_SUPPORTED_TRIPLES
        ),
        "resolver_checks": dict(checker.CORE_UNSUPPORTED_TOMBSTONE_RESOLVER_CHECKS),
    }


def write_core_unsupported_tombstone_record(
    evidence_dir: Path,
    version: str,
    *,
    mutation: TombstoneMutation | None = None,
) -> Path:
    path = evidence_dir / checker.CORE_UNSUPPORTED_TOMBSTONE_RECORD
    if mutation == "malformed-json":
        path.write_text("{not-json", encoding="utf-8")
        return path
    if mutation == "non-mapping":
        path.write_text("[]", encoding="utf-8")
        return path
    payload: Mapping[str, Any] | dict[str, Any] = _tombstone_payload(version)
    if mutation == "extra-key":
        payload = {**payload, "extra": "invalid"}
    elif mutation == "missing-key":
        payload = dict(payload)
        payload.pop("status")
    elif mutation == "wrong-status":
        payload = {**payload, "status": "not-verified"}
    elif mutation == "wrong-version":
        payload = {**payload, "version": "0.0.0"}
    path.write_bytes(checker.canonical_json_bytes(payload))
    return path
