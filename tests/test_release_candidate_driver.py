# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import gzip
import hashlib
import inspect
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tomllib
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

import scripts.check_rust_release_manifest as checker
import scripts.record_macos_native_wheel as native
import scripts.release_build_host as release_build_host
import scripts.release_candidate_driver as driver
import scripts.release_ledger as ledger
import scripts.release_tool_pins as pins
from scripts.check_wheel_contents import (
    CORE_REQUIRED_SDIST_MEMBERS,
    CORE_SCRIPT_NAMES,
    CPU_TYPE_ARM64,
    ELF_MACHINE,
    EXPECTED_MODEL_SHA256,
    PARAKEET_HELPER_MEMBER,
    core_wheel_script_members,
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
CORE_LOCK_CONTENT = "fixture lock\n"
LOCK_SHA = hashlib.sha256(CORE_LOCK_CONTENT.encode("utf-8")).hexdigest()
LINUX_X86_CORE = minimal_elf(ELF_MACHINE["x86_64"])
LINUX_AARCH64_CORE = minimal_elf(ELF_MACHINE["aarch64"])
MACOS_CORE = minimal_macho(CPU_TYPE_ARM64)
MACOS_HELPER = minimal_macho(CPU_TYPE_ARM64)
ZIP_DATE_TIME = (2026, 7, 20, 12, 0, 0)
PRIOR_RETAINED_VERSION = "1.0.13"
assert PRIOR_RETAINED_VERSION != checker._current_version()
assert not PRIOR_RETAINED_VERSION.startswith(checker._current_version())
assert not checker._current_version().startswith(PRIOR_RETAINED_VERSION)


class GuardedEnv(dict):
    def get(self, key: str, default: Any = None) -> Any:
        if key == "SOURCE_COMMIT":
            raise AssertionError("driver must not read SOURCE_COMMIT")
        return super().get(key, default)

    def __getitem__(self, key: str) -> Any:
        if key == "SOURCE_COMMIT":
            raise AssertionError("driver must not read SOURCE_COMMIT")
        return super().__getitem__(key)


def _local_dist_names_for_build_argv(
    argv: Sequence[str], *, include_models: bool
) -> set[str]:
    args = tuple(argv)
    expected = driver._expected_local_dist_names(include_models=include_models)
    if args == ("uv", "build", "--package", "solstone-core", "--sdist"):
        return {
            name
            for name in expected
            if name.startswith("solstone_core-") and name.endswith(".tar.gz")
        }
    if len(args) == 4 and args[:3] == ("uv", "build", "--package"):
        package = args[3]
        prefix = f"{package.replace('-', '_')}-"
        return {name for name in expected if name.startswith(prefix)}
    return set()


def _write_fake_core_sdist(root: Path, archive: Path) -> None:
    version = archive.name.removeprefix("solstone_core-").removesuffix(".tar.gz")
    source_members = ["crates/solstone-core", "crates/solstone-core-sol"]
    source_manifest = (
        f'[workspace]\nmembers = {json.dumps(source_members)}\nresolver = "3"\n'
    )
    (root / "core" / "crates" / "solstone-core").mkdir(parents=True, exist_ok=True)
    (root / "core" / "crates" / "solstone-core-sol").mkdir(parents=True, exist_ok=True)
    (root / "core" / "crates" / "solstone-core" / "src").mkdir(
        parents=True,
        exist_ok=True,
    )
    (root / "core" / "crates" / "solstone-core-sol" / "src").mkdir(
        parents=True,
        exist_ok=True,
    )
    (root / "core" / "Cargo.toml").write_text(source_manifest, encoding="utf-8")
    (root / "core" / "crates" / "solstone-core" / "Cargo.toml").write_text(
        f'[package]\nname = "solstone-core"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (root / "core" / "crates" / "solstone-core-sol" / "Cargo.toml").write_text(
        f'[package]\nname = "solstone-core-sol"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    (root / "core" / "crates" / "solstone-core" / "src" / "main.rs").write_text(
        "fn main() {}\n",
        encoding="utf-8",
    )
    (root / "core" / "crates" / "solstone-core-sol" / "src" / "lib.rs").write_text(
        "",
        encoding="utf-8",
    )
    sdist_manifest = (
        '[workspace]\nmembers = ["crates/solstone-core", "crates/solstone-core-sol"]\n'
        'resolver = "3"\n'
    ).encode()
    sdist_lock = (
        "version = 4\n\n"
        f'[[package]]\nname = "solstone-core"\nversion = "{version}"\n\n'
        f'[[package]]\nname = "solstone-core-sol"\nversion = "{version}"\n'
    ).encode()
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, mode="w:gz") as target:
        for name, data in (
            (f"solstone_core-{version}/core/Cargo.toml", sdist_manifest),
            (f"solstone_core-{version}/core/Cargo.lock", sdist_lock),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            target.addfile(member, BytesIO(data))


def _fabricate_local_dist_for_build_argv(
    root: Path, argv: Sequence[str], *, include_models: bool
) -> None:
    args = tuple(argv)
    names = _local_dist_names_for_build_argv(argv, include_models=include_models)
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    if args == ("uv", "build", "--package", "solstone-core", "--sdist"):
        archive = dist / next(iter(names))
        _write_fake_core_sdist(root, archive)
        return
    if (
        len(args) == 6
        and args[:2] == ("uv", "build")
        and args[2].startswith("dist/solstone_core-")
        and args[3:] == ("--wheel", "--out-dir", "dist")
    ):
        core_wheels = {
            name
            for name in driver._expected_local_dist_names(include_models=include_models)
            if name.startswith("solstone_core-") and name.endswith(".whl")
        }
        remaining = sorted(name for name in core_wheels if not (dist / name).exists())
        if remaining:
            (dist / remaining[0]).write_bytes(b"package")
        return
    for name in names:
        (dist / name).write_bytes(b"package")


def _prepare_fake_build_root(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nversion = "{checker._current_version()}"\n',
        encoding="utf-8",
    )


def _is_final_core_wheel_build(
    root: Path, argv: Sequence[str], *, include_models: bool
) -> bool:
    args = tuple(argv)
    if not (
        len(args) == 6
        and args[:2] == ("uv", "build")
        and args[2].startswith("dist/solstone_core-")
        and args[3:] == ("--wheel", "--out-dir", "dist")
    ):
        return False
    expected = {
        name
        for name in driver._expected_local_dist_names(include_models=include_models)
        if name.startswith("solstone_core-") and name.endswith(".whl")
    }
    return all((root / "dist" / name).is_file() for name in expected)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "core").mkdir(parents=True)
    (root / "packages" / "solstone-journal-models").mkdir(parents=True)
    (root / "core" / "Cargo.lock").write_text(CORE_LOCK_CONTENT, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        f'[project]\nversion = "{checker._current_version()}"\n',
        encoding="utf-8",
    )
    (root / "packages" / "solstone-journal-models" / "pyproject.toml").write_text(
        '[project]\nname = "solstone-journal-models"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    return root


def _env() -> GuardedEnv:
    return GuardedEnv(
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
        info = zipfile.ZipInfo(metadata_name, ZIP_DATE_TIME)
        info.create_system = 3
        info.external_attr = 0o644 << 16
        wheel.writestr(info, metadata)


def _write_linux_core_wheels(dist_dir: Path) -> None:
    content_by_lane = {
        "linux-x86_64-musl": LINUX_X86_CORE,
        "linux-aarch64-musl": LINUX_AARCH64_CORE,
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
        Path(__file__).resolve().parents[1]
        / "packages"
        / "solstone-journal-models"
        / "solstone_journal_models"
        / "assets"
    )
    with zipfile.ZipFile(path, "w") as wheel:
        metadata_name, metadata = _wheel_metadata(path.name)
        metadata_info = zipfile.ZipInfo(metadata_name, ZIP_DATE_TIME)
        metadata_info.create_system = 3
        metadata_info.external_attr = 0o644 << 16
        wheel.writestr(metadata_info, metadata)
        for basename in sorted(EXPECTED_MODEL_SHA256):
            asset_info = zipfile.ZipInfo(
                f"solstone_journal_models/assets/{basename}", ZIP_DATE_TIME
            )
            asset_info.create_system = 3
            asset_info.external_attr = 0o644 << 16
            wheel.writestr(asset_info, (assets_dir / basename).read_bytes())


def _macos_wheel_names() -> tuple[str, str]:
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


def _write_macos_host_outputs(
    output_dir: Path,
    *,
    mutate: str | None = None,
) -> BuildHostResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    root_name, core_name = _macos_wheel_names()
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
        "linux-x86_64-musl": hashlib.sha256(LINUX_X86_CORE).hexdigest(),
        "linux-aarch64-musl": hashlib.sha256(LINUX_AARCH64_CORE).hexdigest(),
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


def _services(
    root: Path, *, native_mutation: str | None = None
) -> driver.CandidateServices:
    def clean_outputs(repo: Path, version: str) -> None:
        for relative in (
            "build",
            "dist",
            f"target/release-evidence/{version}",
            f"target/release-transfer/{version}",
            f"target/release-transfer/.{version}.source.bundle",
        ):
            path = repo / relative
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()

    def build_local_dist(repo: Path, include_models: bool) -> None:
        dist = repo / "dist"
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
        _write_linux_core_wheels(repo / "dist")

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
        return _write_macos_host_outputs(output_dir, mutate=native_mutation)

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


def _ready_paths(root: Path) -> tuple[Path, Path, Path, Path]:
    version = checker._current_version()
    ready_path = root / "dist" / "release-candidate" / version
    payload_staging = ready_path.parent / f"{version}.payload-staging"
    evidence_dir = root / "target" / "release-evidence" / version
    evidence_staging = root / "target" / "release-evidence" / f"{version}.staging"
    return ready_path, payload_staging, evidence_dir, evidence_staging


def _assert_no_ready_cohort(root: Path) -> None:
    ready_path, payload_staging, evidence_dir, evidence_staging = _ready_paths(root)
    assert not ready_path.exists()
    assert not payload_staging.exists()
    assert not evidence_dir.exists()
    assert not evidence_staging.exists()


def _expected_scrubbed_env(root: Path, maturin_args: str) -> dict[str, str]:
    cache_root = root / "target" / "release-zig-cache"
    return {
        "MATURIN_PEP517_ARGS": maturin_args,
        "PATH": os.environ["PATH"],
        "PYTHONNOUSERSITE": "1",
        "ZIG_GLOBAL_CACHE_DIR": str((cache_root / "zig-global").resolve()),
        "ZIG_LOCAL_CACHE_DIR": str((cache_root / "zig-local").resolve()),
    }


def _recover(root: Path) -> driver.CandidateReport:
    return driver.run_recover(
        root,
        version=checker._current_version(),
        source_commit=SOURCE_COMMIT,
    )


@dataclass(frozen=True)
class TreeSnapshotEntry:
    relative: str
    kind: str
    mode: int
    symlink_target: str | None
    empty_dir: bool
    size: int | None
    sha256: str | None


def _snapshot_kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "regular"
    return "special"


def _structural_snapshot(path: Path) -> tuple[TreeSnapshotEntry, ...]:
    if not path.exists() and not path.is_symlink():
        return ()
    entries: list[TreeSnapshotEntry] = []

    def visit(current: Path, relative: Path) -> None:
        entry = current.lstat()
        kind = _snapshot_kind(entry.st_mode)
        children: list[Path] = []
        symlink_target: str | None = None
        size: int | None = None
        digest: str | None = None
        if kind == "symlink":
            symlink_target = os.readlink(current)
        elif kind == "regular":
            data = current.read_bytes()
            size = len(data)
            digest = hashlib.sha256(data).hexdigest()
        elif kind == "directory":
            children = sorted(current.iterdir(), key=lambda child: child.name)
        entries.append(
            TreeSnapshotEntry(
                relative=relative.as_posix() if relative.parts else ".",
                kind=kind,
                mode=stat.S_IMODE(entry.st_mode),
                symlink_target=symlink_target,
                empty_dir=kind == "directory" and not children,
                size=size,
                sha256=digest,
            )
        )
        if kind == "directory":
            for child in children:
                visit(child, relative / child.name)

    visit(path, Path())
    return tuple(entries)


def _access_spy(
    monkeypatch: pytest.MonkeyPatch, *, denied_path: Path | None = None
) -> list[tuple[Path, int]]:
    original_access = os.access
    recorded: list[tuple[Path, int]] = []

    def access(path: object, mask: int, *args: object, **kwargs: object) -> bool:
        recorded_path = Path(path)
        recorded.append((recorded_path, mask))
        if denied_path is not None and recorded_path == denied_path:
            return False
        return original_access(path, mask, *args, **kwargs)

    monkeypatch.setattr(os, "access", access)
    return recorded


def _enumeration_spy(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    original_listdir = os.listdir
    original_scandir = os.scandir
    recorded: list[Path] = []

    def record(path: object) -> None:
        if isinstance(path, int):
            return
        try:
            recorded.append(Path(path))
        except TypeError:
            return

    def listdir(path: object = ".") -> list[str]:
        record(path)
        return original_listdir(path)

    def scandir(path: object = ".") -> object:
        record(path)
        return original_scandir(path)

    monkeypatch.setattr(os, "listdir", listdir)
    monkeypatch.setattr(os, "scandir", scandir)
    return recorded


def _same_or_descendant(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _write_directory_sentinel(path: Path) -> None:
    marker = path / "inside" / "marker.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"inside {path.name}", encoding="utf-8")
    outside = path.parent / f"outside-{path.name}.txt"
    outside.write_text(f"outside {path.name}", encoding="utf-8")


def _write_cleanup_preflight_sentinels(
    root: Path,
    dist_payload_root: Path | None,
    *,
    version: str,
) -> None:
    for path in (
        root / "build",
        root / "root-stale.egg-info",
        root / "packages" / "solstone-journal" / "solstone_journal.egg-info",
        root / "packages" / "solstone-journal-cuda" / "solstone_journal_cuda.egg-info",
        root
        / "packages"
        / "solstone-journal-models"
        / "solstone_journal_models.egg-info",
        root / "target" / "release-evidence" / version,
        root / "target" / "release-evidence" / f"{version}.staging",
        root / "target" / "release-transfer" / version,
        root / "target" / "release-transfer" / f".{version}.source.bundle",
        root / "target" / "release-transfer" / f".{version}.request-abc123",
        root / "target" / "release-zig-cache",
    ):
        _write_directory_sentinel(path)
    if dist_payload_root is None:
        return
    dist_payload_root.mkdir(parents=True, exist_ok=True)
    (dist_payload_root / "raw-build-output.whl").write_bytes(b"raw")
    _write_directory_sentinel(dist_payload_root / "raw-dir")
    reserved = dist_payload_root / driver.RESERVED_CANDIDATE_DIRNAME
    for name in (
        version,
        f"{version}.payload-staging",
        f"{version}.payload-staging.staging",
        f"{version}.payload-staging.quarantine",
    ):
        _write_directory_sentinel(reserved / name)


def _reserved_candidate_path(root: Path) -> Path:
    return root / "dist" / driver.RESERVED_CANDIDATE_DIRNAME


def _write_expected_local_dist(root: Path, *, include_models: bool) -> None:
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    for name in driver._expected_local_dist_names(include_models=include_models):
        (dist / name).write_bytes(f"fixture package {name}\n".encode("utf-8"))


def _existing_expected_artifact_facts(
    root: Path, *, include_models: bool
) -> dict[str, tuple[str, int]]:
    dist = root / "dist"
    facts: dict[str, tuple[str, int]] = {}
    for name in driver._expected_local_dist_names(include_models=include_models):
        path = dist / name
        if not path.exists() or path.is_symlink():
            continue
        data = path.read_bytes()
        facts[name] = (hashlib.sha256(data).hexdigest(), path.stat().st_size)
    return facts


def _assert_reserved_parent_failure(
    exc: pytest.ExceptionInfo[driver.DriverError],
    *,
    operation: driver.DistPreflightOperation,
    actual: str,
    denied_access: bool = False,
) -> None:
    policy = driver.DIST_PREFLIGHT_POLICIES[operation]
    if denied_access:
        reserved_access = policy.reserved_access
        assert reserved_access is not None
        expected_error = reserved_access.access_error
    else:
        expected_error = policy.reserved_unsafe_error
    assert exc.value.failures[0].error == expected_error
    assert exc.value.failures[0].expected == driver._reserved_expected(policy)
    assert exc.value.failures[0].actual == actual
    assert exc.value.failures[0].repair == "bash scripts/release.sh --candidate"


def _assert_dist_preflight_failure(
    exc: pytest.ExceptionInfo[driver.DriverError],
    *,
    operation: driver.DistPreflightOperation,
    actual: str,
    denied_access: bool = False,
) -> None:
    policy = driver.DIST_PREFLIGHT_POLICIES[operation]
    assert exc.value.failures[0].error == (
        policy.dist_access_error if denied_access else policy.dist_unsafe_error
    )
    assert exc.value.failures[0].expected == driver._dist_expected(policy)
    assert exc.value.failures[0].actual == actual
    assert exc.value.failures[0].repair == "bash scripts/release.sh --candidate"


def test_fake_all_host_candidate_and_recovery_are_deterministic(
    tmp_path: Path,
) -> None:
    first_root = _repo(tmp_path / "one")
    second_root = _repo(tmp_path / "two")

    first = driver.run_candidate(first_root, _env(), _services(first_root))
    second = driver.run_candidate(second_root, _env(), _services(second_root))

    assert first.heading == "candidate-proven"
    assert second.heading == "candidate-proven"
    assert not (
        first_root / "target" / "release-transfer" / checker._current_version()
    ).exists()
    assert not (
        first_root
        / "target"
        / "release-transfer"
        / f".{checker._current_version()}.source.bundle"
    ).exists()
    assert first.candidate_digest == second.candidate_digest
    assert first.bundle_digest == second.bundle_digest
    assert (
        first.evidence_dir.joinpath("ledger.json").read_bytes()
        == second.evidence_dir.joinpath("ledger.json").read_bytes()
    )
    assert sorted(path.name for path in first.release_dir.iterdir()) == sorted(
        path.name for path in second.release_dir.iterdir()
    )
    release_names = {path.name for path in first.release_dir.iterdir()}
    assert any(
        name.startswith("solstone_core-") and "manylinux2014_x86_64" in name
        for name in release_names
    )
    assert any(
        name.startswith("solstone_core-") and "manylinux2014_aarch64" in name
        for name in release_names
    )
    root_name, core_name = _macos_wheel_names()
    with zipfile.ZipFile(first.release_dir / root_name) as wheel:
        assert wheel.read(PARAKEET_HELPER_MEMBER) == MACOS_HELPER
    with zipfile.ZipFile(first.release_dir / core_name) as wheel:
        member = next(
            member
            for member in core_wheel_script_members(wheel)
            if Path(member.filename).name == "solstone-core"
        )
        assert wheel.read(member) == MACOS_CORE

    recovered = _recover(first_root)
    assert recovered.heading == "retained-candidate-valid"
    assert recovered.bundle_digest == first.bundle_digest


def test_recovery_uses_explicit_selector_and_preserves_retained_bytes(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    report = driver.run_candidate(root, _env(), _services(root))
    before_payload = _structural_snapshot(report.release_dir)
    before_evidence = _structural_snapshot(report.evidence_dir)
    (root / "pyproject.toml").unlink()
    shutil.rmtree(root / "packages")

    recovered = driver.run_recover(
        root,
        version=report.version,
        source_commit=SOURCE_COMMIT,
    )

    assert recovered.heading == "retained-candidate-valid"
    assert _structural_snapshot(report.release_dir) == before_payload
    assert _structural_snapshot(report.evidence_dir) == before_evidence


def test_recovery_ignores_current_release_metadata_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    report = driver.run_candidate(root, _env(), _services(root))

    def fail_if_used(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("recovery read current release metadata")

    monkeypatch.setattr(driver, "expected_package_names", fail_if_used)
    monkeypatch.setattr(driver, "rust_artifact_targets", fail_if_used)
    monkeypatch.setattr(driver, "validate_release_dir", fail_if_used)
    monkeypatch.setattr(ledger, "rust_artifact_targets", fail_if_used)

    recovered = driver.run_recover(
        root,
        version=report.version,
        source_commit=SOURCE_COMMIT,
    )

    assert recovered.heading == "retained-candidate-valid"


def test_fresh_cleanup_preserves_other_retained_versions_and_recovery(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    report = driver.run_candidate(root, _env(), _services(root))
    before_payload = _structural_snapshot(report.release_dir)
    before_evidence = _structural_snapshot(report.evidence_dir)
    raw_dist_file = root / "dist" / "raw-build-output.whl"
    raw_dist_file.write_bytes(b"raw")

    driver._default_clean_outputs(root, "9.9.9")

    assert not raw_dist_file.exists()
    assert _structural_snapshot(report.release_dir) == before_payload
    assert _structural_snapshot(report.evidence_dir) == before_evidence
    recovered = driver.run_recover(
        root,
        version=report.version,
        source_commit=SOURCE_COMMIT,
    )
    assert recovered.heading == "retained-candidate-valid"


def test_recovery_rejects_absent_or_mutated_selector(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    driver.run_candidate(root, _env(), _services(root))

    with pytest.raises(driver.DriverError) as exc:
        driver.run_recover(root, version="", source_commit=SOURCE_COMMIT)
    assert exc.value.failures[0].error == "retained release version selector is missing"

    with pytest.raises(driver.DriverError) as exc:
        driver.run_recover(root, version="../0.9.0", source_commit=SOURCE_COMMIT)
    assert exc.value.failures[0].error == "retained release version selector is unsafe"

    with pytest.raises(driver.DriverError) as exc:
        driver.run_recover(
            root,
            version=checker._current_version(),
            source_commit="b" * 40,
        )
    assert (
        exc.value.failures[0].error
        == "retained ledger source commit does not match selector"
    )

    with pytest.raises(driver.DriverError) as exc:
        driver.run_recover(root, version="0.0.0", source_commit=SOURCE_COMMIT)
    assert (
        exc.value.failures[0].error == "retained ledger could not be read for selector"
    )


def test_recovery_rejects_garbage_retained_advisory_identity(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    report = driver.run_candidate(root, _env(), _services(root))
    ledger_path = report.evidence_dir / "ledger.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    payload["policy_run"]["db_commit"] = "not-hex"
    ledger_path.write_bytes(checker.canonical_json_bytes(payload))

    with pytest.raises(driver.DriverError) as exc:
        _recover(root)

    assert any(
        failure.error.endswith(".db_commit is invalid")
        for failure in exc.value.failures
    )


def test_recovery_rejects_impossible_retained_policy_timestamp(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    report = driver.run_candidate(root, _env(), _services(root))
    ledger_path = report.evidence_dir / "ledger.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    payload["policy_run"]["db_commit_timestamp"] = "2026-99-19T12:00:00Z"
    ledger_path.write_bytes(checker.canonical_json_bytes(payload))

    with pytest.raises(driver.DriverError) as exc:
        _recover(root)

    assert any(
        failure.error == "retained ledger db_commit_timestamp is invalid"
        for failure in exc.value.failures
    )


def test_recovery_has_no_service_surface() -> None:
    parameters = set(inspect.signature(driver.run_recover).parameters)

    assert parameters == {"root", "version", "source_commit"}


def test_machine_report_is_canonical_sorted_and_not_publication_authorization(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    candidate = driver.run_candidate(root, _env(), _services(root))
    retained = _recover(root)

    for report in (candidate, retained):
        text = driver.format_report(report)
        payload = json.loads(text)
        assert text.encode("utf-8") == checker.canonical_json_bytes(payload)
        assert payload["verdict"] == report.heading
        assert (
            payload["publication_authorization"]
            == "local candidate evidence only; not publication authorization"
        )
        payload_names = [item["name"] for item in payload["payload_inventory"]]
        evidence_names = [item["name"] for item in payload["evidence_inventory"]]
        assert payload_names == sorted(payload_names)
        assert evidence_names == sorted(evidence_names)
        assert payload["candidate_digest"] == driver.candidate_digest(
            report.release_dir
        )
        assert (
            payload["ledger_sha256"]
            == driver.file_sha256_size(report.evidence_dir / "ledger.json")[0]
        )
        for target, entry in payload["proof_inventory"].items():
            assert entry["sha256"] == payload["proof_sha256"][target]


def test_candidate_cleanup_receives_release_zig_cache_root(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    services = _services(root)
    version = checker._current_version()
    cache_root = root / "target" / "release-zig-cache"
    (cache_root / "zig-global").mkdir(parents=True)
    (cache_root / "zig-global" / "marker").write_text("stale", encoding="utf-8")
    cleanup_calls: list[tuple[Path, ...]] = []

    def cleanup(paths: Sequence[Path]) -> None:
        cleanup_calls.append(tuple(paths))
        services.cleanup_transients(paths)

    driver.run_candidate(
        root,
        _env(),
        replace(services, cleanup_transients=cleanup),
    )

    assert cleanup_calls == [
        (
            root / "target" / "release-transfer" / version,
            root / "target" / "release-transfer" / f".{version}.source.bundle",
            cache_root,
        )
    ]
    assert not cache_root.exists()


def test_candidate_source_bundle_does_not_preexist_build_host_output(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    services = _services(root)
    original = services.build_host
    checked_output_dirs: list[Path] = []

    def build_host(
        source_bundle: SourceBundle, commit: str, output_dir: Path
    ) -> BuildHostResult:
        release_build_host._validate_fresh_directory_path(output_dir, label="output")
        checked_output_dirs.append(output_dir)
        return original(source_bundle, commit, output_dir)

    driver.run_candidate(root, _env(), replace(services, build_host=build_host))

    assert checked_output_dirs == [
        root / "target" / "release-transfer" / checker._current_version()
    ]


def test_dry_run_linux_validates_static_plan_without_files_or_services(
    tmp_path: Path,
) -> None:
    before = sorted(tmp_path.rglob("*"))
    output = driver.run_dry_run_linux(tmp_path, _env())

    assert sorted(tmp_path.rglob("*")) == before
    assert "validated" in output
    assert "candidate-proven" not in output
    assert "clean-source claim" in output


@pytest.mark.parametrize(
    "mutation",
    ["artifact", "model", "tool", "build-arg", "lockout"],
)
def test_dry_run_linux_rejects_bad_plan_cases(
    tmp_path: Path,
    mutation: str,
) -> None:
    plan = driver.default_dry_run_plan(_env())
    if mutation == "artifact":
        plan = replace(plan, artifacts=plan.artifacts[:-1])
    elif mutation == "model":
        plan = replace(plan, models_decision="publish")
    elif mutation == "tool":
        tools = {lane: dict(values) for lane, values in plan.tool_evidence.items()}
        tools["source"]["rustc"] = "rustc 0.0.0"
        plan = replace(plan, tool_evidence=tools)
    elif mutation == "build-arg":
        args = dict(plan.linux_maturin_args)
        args["x86_64-unknown-linux-musl"] = args["x86_64-unknown-linux-musl"].replace(
            "--locked ", ""
        )
        plan = replace(plan, linux_maturin_args=args)
    elif mutation == "lockout":
        lockout = dict(plan.publication_lockout)
        lockout["make release"] = False
        plan = replace(plan, publication_lockout=lockout)

    with pytest.raises(driver.DriverError):
        driver.run_dry_run_linux(tmp_path, _env(), plan=plan)


def test_main_prints_failure_records_from_build_host_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    failure = checker.Failure(
        error="distinct build-host failure",
        expected="distinct expected value",
        actual="distinct actual value",
        repair="distinct repair command",
    )

    def run_candidate(*_args: object, **_kwargs: object) -> driver.CandidateReport:
        raise release_build_host.BuildHostError([failure])

    monkeypatch.setattr(driver, "run_candidate", run_candidate)

    assert driver.main(["candidate"], _env()) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "ERROR: distinct build-host failure" in captured.err
    assert "expected: distinct expected value" in captured.err
    assert "actual: distinct actual value" in captured.err
    assert "repair command: distinct repair command" in captured.err
    assert "actual: BuildHostError" not in captured.err


def test_main_preserves_generic_fallback_for_plain_exceptions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def run_candidate(*_args: object, **_kwargs: object) -> driver.CandidateReport:
        raise RuntimeError("boom")

    monkeypatch.setattr(driver, "run_candidate", run_candidate)

    assert driver.main(["candidate"], _env()) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "ERROR: release candidate driver failed" in captured.err
    assert "actual: RuntimeError" in captured.err


def test_main_uses_generic_fallback_for_invalid_failure_records(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class EmptyFailuresError(RuntimeError):
        failures: tuple[object, ...] = ()

    class FailureShapeError(RuntimeError):
        def __init__(self, failures: object) -> None:
            self.failures = failures
            super().__init__("boom")

    cases = (
        RuntimeError("boom"),
        EmptyFailuresError("boom"),
        FailureShapeError("boom"),
        FailureShapeError(b"boom"),
        FailureShapeError(("not a failure",)),
    )
    for exc in cases:

        def run_candidate(
            *_args: object,
            _exc: BaseException = exc,
            **_kwargs: object,
        ) -> driver.CandidateReport:
            raise _exc

        monkeypatch.setattr(driver, "run_candidate", run_candidate)

        assert driver.main(["candidate"], _env()) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "ERROR: release candidate driver failed" in captured.err
        assert f"actual: {type(exc).__name__}" in captured.err


@pytest.mark.parametrize(
    ("point", "exc_factory"),
    [
        ("after-payload-rename", RuntimeError),
        ("between-renames", RuntimeError),
        ("after-evidence-rename", RuntimeError),
        ("after-payload-rename", KeyboardInterrupt),
        ("between-renames", SystemExit),
    ],
)
def test_candidate_transaction_rolls_back_payload_and_evidence_at_each_rename_point(
    tmp_path: Path,
    point: str,
    exc_factory: type[BaseException],
) -> None:
    root = _repo(tmp_path)
    foreign_payload = root / "dist" / "release-candidate" / "foreign"
    foreign_evidence = root / "target" / "release-evidence" / "foreign"

    def hook(actual_point: str) -> None:
        if actual_point == point:
            foreign_payload.mkdir(parents=True)
            foreign_evidence.mkdir(parents=True)
            (foreign_payload / "keep").write_text("payload", encoding="utf-8")
            (foreign_evidence / "keep").write_text("evidence", encoding="utf-8")
            raise exc_factory()

    services = replace(_services(root), transaction_hook=hook)

    with pytest.raises(driver.DriverError) as exc:
        driver.run_candidate(root, _env(), services)

    _assert_no_ready_cohort(root)
    assert (foreign_payload / "keep").read_text(encoding="utf-8") == "payload"
    assert (foreign_evidence / "keep").read_text(encoding="utf-8") == "evidence"
    assert any(
        failure.error == "release candidate finalization transaction failed"
        for failure in exc.value.failures
    )


def test_candidate_transaction_aggregates_cleanup_errors(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "foreign"
    outside.mkdir()
    (outside / "keep").write_text("keep", encoding="utf-8")

    def hook(point: str) -> None:
        if point == "after-payload-rename":
            ready_path, _payload_staging, _evidence_dir, _evidence_staging = (
                _ready_paths(root)
            )
            shutil.rmtree(ready_path)
            ready_path.symlink_to(outside, target_is_directory=True)
            raise RuntimeError()

    services = replace(_services(root), transaction_hook=hook)

    with pytest.raises(driver.DriverError) as exc:
        driver.run_candidate(root, _env(), services)

    assert (outside / "keep").read_text(encoding="utf-8") == "keep"
    assert any(
        failure.error == "release candidate finalization transaction failed"
        for failure in exc.value.failures
    )
    assert any("symlink residue" in failure.error for failure in exc.value.failures)


@pytest.mark.parametrize("mutation", ["nested", "extra", "missing", "symlink"])
def test_candidate_final_recheck_rejects_payload_inventory_mutations(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.whl").write_text("outside", encoding="utf-8")

    def hook(point: str) -> None:
        if point != "after-evidence-rename":
            return
        ready_path, _payload_staging, _evidence_dir, _evidence_staging = _ready_paths(
            root
        )
        first_file = next(path for path in ready_path.iterdir() if path.is_file())
        if mutation == "nested":
            nested = ready_path / "nested"
            nested.mkdir()
            (nested / "extra.txt").write_text("extra", encoding="utf-8")
        elif mutation == "extra":
            (ready_path / "extra.whl").write_text("extra", encoding="utf-8")
        elif mutation == "missing":
            first_file.unlink()
        elif mutation == "symlink":
            first_file.unlink()
            first_file.symlink_to(outside / "payload.whl")

    services = replace(_services(root), transaction_hook=hook)

    with pytest.raises(driver.DriverError) as exc:
        driver.run_candidate(root, _env(), services)

    _assert_no_ready_cohort(root)
    assert any("payload" in failure.error for failure in exc.value.failures)


@pytest.mark.parametrize("mutation", ["extra", "temp", "directory", "proof-symlink"])
def test_candidate_final_recheck_rejects_evidence_inventory_mutations(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "outside-proof.json"
    outside.write_text("{}", encoding="utf-8")

    def hook(point: str) -> None:
        if point != "after-evidence-rename":
            return
        _ready_path, _payload_staging, evidence_dir, _evidence_staging = _ready_paths(
            root
        )
        if mutation == "extra":
            (evidence_dir / "extra.json").write_text("extra", encoding="utf-8")
        elif mutation == "temp":
            (evidence_dir / ".ledger.json.tmp").write_text("temp", encoding="utf-8")
        elif mutation == "directory":
            (evidence_dir / "extra-dir").mkdir()
        elif mutation == "proof-symlink":
            proof = evidence_dir / "proofs" / "macos-arm64.json"
            proof.unlink()
            proof.symlink_to(outside)

    services = replace(_services(root), transaction_hook=hook)

    with pytest.raises(driver.DriverError) as exc:
        driver.run_candidate(root, _env(), services)

    _assert_no_ready_cohort(root)
    assert any(
        "evidence" in failure.error or "proof" in failure.error
        for failure in exc.value.failures
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "kind",
        "product",
        "version",
        "source_commit",
        "core_lock_sha256",
        "rust_targets",
        "proofs",
        "redaction",
        "policy_result",
        "advisory_source_id",
        "native_summary",
    ],
)
def test_candidate_final_recheck_rejects_deep_ledger_binding_mutations(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = _repo(tmp_path)

    def hook(point: str) -> None:
        if point != "after-evidence-rename":
            return
        _ready_path, _payload_staging, evidence_dir, _evidence_staging = _ready_paths(
            root
        )
        ledger_path = evidence_dir / "ledger.json"
        payload = json.loads(ledger_path.read_text(encoding="utf-8"))
        if mutation == "kind":
            payload["kind"] = "forged-ledger"
        elif mutation == "product":
            payload["product"] = "other"
        elif mutation == "version":
            payload["version"] = "0.0.0"
        elif mutation == "source_commit":
            payload["source_commit"] = "b" * 40
        elif mutation == "core_lock_sha256":
            payload["core_lock_sha256"] = "0" * 64
        elif mutation == "rust_targets":
            payload["rust_targets"] = []
        elif mutation == "proofs":
            payload["proofs"]["expected_targets"] = ["macos-arm64"]
        elif mutation == "redaction":
            payload["redaction"]["validator"] = "none"
        elif mutation == "policy_result":
            payload["policy_run"]["result"] = "fail"
        elif mutation == "advisory_source_id":
            payload["policy_run"]["advisory_source_id"] = ""
        elif mutation == "native_summary":
            payload["native_summary"]["macos_root_helper"]["wheel"]["sha256"] = "0" * 64
        ledger_path.write_bytes(checker.canonical_json_bytes(payload))

    services = replace(_services(root), transaction_hook=hook)

    with pytest.raises(driver.DriverError):
        driver.run_candidate(root, _env(), services)

    _assert_no_ready_cohort(root)


def test_candidate_final_recheck_rejects_clean_status_drift_and_rolls_back(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    calls = 0

    def git_status(_repo: Path) -> str:
        nonlocal calls
        calls += 1
        return " M late-change" if calls == 3 else ""

    services = replace(_services(root), git_status=git_status)

    with pytest.raises(driver.DriverError) as exc:
        driver.run_candidate(root, _env(), services)

    _assert_no_ready_cohort(root)
    assert exc.value.failures[0].error == "release source tree is not clean"


def test_candidate_final_recheck_rejects_core_lock_drift_and_rolls_back(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    calls = 0

    def core_lock_sha256(_repo: Path) -> str:
        nonlocal calls
        calls += 1
        return "0" * 64 if calls == 4 else LOCK_SHA

    services = replace(_services(root), core_lock_sha256=core_lock_sha256)

    with pytest.raises(driver.DriverError) as exc:
        driver.run_candidate(root, _env(), services)

    _assert_no_ready_cohort(root)
    assert exc.value.failures[0].error == "core lock hash changed before finalization"


def test_recovery_rejects_swapped_replayed_or_mutated_proofs(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    report = driver.run_candidate(root, _env(), _services(root))
    proof = report.evidence_dir / "proofs" / "macos-arm64.json"
    payload = json.loads(proof.read_text(encoding="utf-8"))
    payload["candidate_digest"] = "0" * 64
    proof.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(driver.DriverError) as exc:
        _recover(root)

    assert any(
        failure.error
        == "install proof candidate_digest is not bound to retained candidate"
        for failure in exc.value.failures
    )


@pytest.mark.parametrize(
    "argv",
    [
        ("bash", "scripts/release.sh"),
        ("bash", "scripts/release.sh", "--test"),
        ("make", "release"),
        ("make", "release-test"),
    ],
)
def test_publication_entrypoints_fail_closed_before_external_seams(
    tmp_path: Path, argv: Sequence[str]
) -> None:
    sentinel_dir = tmp_path / "sentinels"
    sentinel_dir.mkdir()
    log = tmp_path / "sentinel.log"
    for name in (
        "ssh",
        "rsync",
        "twine",
        "uvx",
        "gh",
        "git",
        "curl",
        "uv",
        "cargo",
        "codesign",
        "xcrun",
    ):
        path = sentinel_dir / name
        path.write_text(
            f'#!/bin/sh\necho {name} "$@" >> {log}\nexit 99\n',
            encoding="utf-8",
        )
        path.chmod(0o755)
    env = {
        "PATH": f"{sentinel_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
    }

    result = subprocess.run(
        list(argv),
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "make publish-release" in result.stderr
    assert "scripts/release_publish.py" in result.stderr
    assert "release-publisher" not in result.stderr
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


def test_deleted_all_hosts_mode_is_unknown_without_external_seams(
    tmp_path: Path,
) -> None:
    sentinel_dir = tmp_path / "sentinels"
    sentinel_dir.mkdir()
    log = tmp_path / "sentinel.log"
    for name in ("git", "ssh", "uv", "uvx", "cargo"):
        path = sentinel_dir / name
        path.write_text(
            f'#!/bin/sh\necho {name} "$@" >> {log}\nexit 99\n',
            encoding="utf-8",
        )
        path.chmod(0o755)
    result = subprocess.run(
        ["bash", "scripts/release.sh", "--dry-run-all-hosts"],
        cwd=Path(__file__).resolve().parent.parent,
        env={
            "PATH": f"{sentinel_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path / "home"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "unknown argument: --dry-run-all-hosts" in result.stderr
    assert not log.exists()


def test_make_release_targets_have_no_prerequisites() -> None:
    makefile = (Path(__file__).resolve().parent.parent / "Makefile").read_text(
        encoding="utf-8"
    )
    for target in ("release", "release-test"):
        line = next(
            line for line in makefile.splitlines() if line.startswith(f"{target}:")
        )
        before_comment = line.split("##", 1)[0]
        assert before_comment == f"{target}: "


def test_candidate_rejects_models_and_identity_drift(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    env = _env()
    env["RELEASE_MODEL_PACKAGES"] = "publish"
    with pytest.raises(driver.DriverError) as exc:
        driver.run_candidate(root, env, _services(root))
    assert exc.value.failures[0].error == "release model package decision is invalid"

    services = replace(_services(root), git_head=lambda _repo: "b" * 40)
    with pytest.raises(driver.DriverError) as exc:
        driver.run_candidate(root, _env(), services)
    assert (
        exc.value.failures[0].error
        == "release source commit does not match EXPECTED_RELEASE_COMMIT"
    )


def test_default_services_have_no_fixture_lane_evidence() -> None:
    services = driver.default_services()

    assert not hasattr(services, "lane_evidence")
    assert (
        services.coordinator_tool_evidence is driver._default_coordinator_tool_evidence
    )


def test_tool_skew_is_rejected_before_any_build(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    build_called = False

    def build_local_dist(_repo: Path, _include_models: bool) -> None:
        nonlocal build_called
        build_called = True

    tools = {
        lane: pins.fixture_lane_tool_evidence(lane)
        for lane in ("source", "linux-x86_64-musl", "linux-aarch64-musl")
    }
    tools["source"] = {**tools["source"], "rustc": "rustc 0.0.0"}
    services = replace(
        _services(root),
        coordinator_tool_evidence=lambda: tools,
        build_local_dist=build_local_dist,
    )

    with pytest.raises(driver.DriverError) as exc:
        driver.run_candidate(root, _env(), services)

    assert not build_called
    assert any(
        failure.error == "release lane tool rustc is not pinned"
        for failure in exc.value.failures
    )


def test_models_decision_is_bound_in_ledger_and_recovery(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    env = _env()
    env["RELEASE_MODEL_PACKAGES"] = "include"
    report = driver.run_candidate(root, env, _services(root))
    payload = json.loads((report.evidence_dir / "ledger.json").read_text())

    assert payload["models"] == {"decision": "include", "package_version": "1.0.0"}
    assert any(
        item["name"].startswith("solstone_journal_models-")
        for item in payload["candidate"]["files"]
    )

    (root / "packages" / "solstone-journal-models" / "pyproject.toml").write_text(
        '[project]\nname = "solstone-journal-models"\nversion = "1.0.1"\n',
        encoding="utf-8",
    )
    recovered = _recover(root)
    assert recovered.heading == "retained-candidate-valid"


def test_default_build_local_dist_package_selection_tracks_workspace_sources() -> None:
    root_data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    expected_include = tuple(
        sorted({driver.ROOT_WORKSPACE_PACKAGE, *driver.WORKSPACE_SOURCES})
    )

    assert driver.ROOT_WORKSPACE_PACKAGE == root_data["project"]["name"]
    assert driver.MODELS_WORKSPACE_PACKAGE in driver.WORKSPACE_SOURCES
    assert (
        driver._expected_local_build_packages(include_models=True) == expected_include
    )
    assert driver._expected_local_build_packages(include_models=False) == tuple(
        name for name in expected_include if name != driver.MODELS_WORKSPACE_PACKAGE
    )


@pytest.mark.parametrize(
    ("include_models", "mutation"),
    [
        (False, "unselected"),
        (True, "partial"),
        (True, "changed"),
    ],
)
def test_default_build_local_dist_rejects_models_inventory_drift(
    tmp_path: Path,
    include_models: bool,
    mutation: str,
) -> None:
    _prepare_fake_build_root(tmp_path)

    def runner(
        argv: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        _fabricate_local_dist_for_build_argv(
            tmp_path,
            argv,
            include_models=include_models,
        )
        if _is_final_core_wheel_build(tmp_path, argv, include_models=include_models):
            dist = tmp_path / "dist"
            if mutation == "unselected":
                for name in (
                    name
                    for name in checker.expected_package_names(include_models=True)
                    if name.startswith("solstone_journal_models-")
                ):
                    (dist / name).write_bytes(b"package")
            elif mutation == "partial":
                models = sorted(
                    path
                    for path in dist.iterdir()
                    if path.name.startswith("solstone_journal_models-")
                )
                models[0].unlink()
            elif mutation == "changed":
                models = sorted(
                    path
                    for path in dist.iterdir()
                    if path.name.startswith("solstone_journal_models-")
                )
                changed = models[0].name.replace("1.0.0", "1.0.1")
                models[0].unlink()
                (dist / changed).write_bytes(b"package")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(driver.DriverError) as exc:
        driver._default_build_local_dist(
            tmp_path,
            include_models=include_models,
            runner=runner,
        )

    assert (
        exc.value.failures[0].error
        == "local release build artifact inventory does not match models decision"
    )


@pytest.mark.parametrize("marker", [b"*", b"*\n"])
def test_default_build_local_dist_strips_uv_dist_gitignore_marker(
    tmp_path: Path,
    marker: bytes,
) -> None:
    _prepare_fake_build_root(tmp_path)

    def runner(
        argv: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        _fabricate_local_dist_for_build_argv(
            tmp_path,
            argv,
            include_models=False,
        )
        if tuple(argv[:2]) == ("uv", "build"):
            (tmp_path / "dist" / ".gitignore").write_bytes(marker)
        return subprocess.CompletedProcess(argv, 0, "", "")

    try:
        driver._default_build_local_dist(tmp_path, include_models=False, runner=runner)
    except driver.DriverError as exc:
        pytest.fail(
            "; ".join(
                f"{failure.error}: actual={failure.actual}" for failure in exc.failures
            )
        )

    assert not (tmp_path / "dist" / ".gitignore").exists()
    assert {p.name for p in (tmp_path / "dist").iterdir()} == set(
        driver._expected_local_dist_names(include_models=False)
    )


def test_default_build_local_dist_rejects_foreign_dist_gitignore_content(
    tmp_path: Path,
) -> None:
    _prepare_fake_build_root(tmp_path)

    def runner(
        argv: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        _fabricate_local_dist_for_build_argv(
            tmp_path,
            argv,
            include_models=False,
        )
        if _is_final_core_wheel_build(tmp_path, argv, include_models=False):
            (tmp_path / "dist" / ".gitignore").write_bytes(b"build/")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(driver.DriverError) as exc:
        driver._default_build_local_dist(tmp_path, include_models=False, runner=runner)

    assert (
        exc.value.failures[0].error
        == "local release build artifact inventory does not match models decision"
    )
    assert ".gitignore" in exc.value.failures[0].actual


def test_default_build_local_dist_rejects_foreign_dist_dotfile(
    tmp_path: Path,
) -> None:
    _prepare_fake_build_root(tmp_path)

    def runner(
        argv: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        _fabricate_local_dist_for_build_argv(
            tmp_path,
            argv,
            include_models=False,
        )
        if _is_final_core_wheel_build(tmp_path, argv, include_models=False):
            (tmp_path / "dist" / ".hidden").write_bytes(b"")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(driver.DriverError) as exc:
        driver._default_build_local_dist(tmp_path, include_models=False, runner=runner)

    assert (
        exc.value.failures[0].error
        == "local release build artifact inventory does not match models decision"
    )
    assert ".hidden" in exc.value.failures[0].actual


def test_default_build_local_dist_rejects_symlink_dist_gitignore(
    tmp_path: Path,
) -> None:
    _prepare_fake_build_root(tmp_path)

    def runner(
        argv: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        _fabricate_local_dist_for_build_argv(
            tmp_path,
            argv,
            include_models=False,
        )
        if _is_final_core_wheel_build(tmp_path, argv, include_models=False):
            (tmp_path / "dist" / ".gitignore").symlink_to("uv-generated-marker")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(driver.DriverError) as exc:
        driver._default_build_local_dist(tmp_path, include_models=False, runner=runner)

    assert {failure.error for failure in exc.value.failures} >= {
        "local release build produced unsafe dist entry"
    }


def test_default_build_local_dist_uses_exact_linux_contract_and_scrubbed_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_fake_build_root(tmp_path)
    monkeypatch.setenv("AMBIENT_RELEASE_TOKEN", "do-not-copy")
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def runner(
        argv: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        calls.append((tuple(argv), dict(env)))
        _fabricate_local_dist_for_build_argv(
            tmp_path,
            argv,
            include_models=False,
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    driver._default_build_local_dist(tmp_path, include_models=False, runner=runner)

    expected_x86_env = _expected_scrubbed_env(tmp_path, driver.CORE_X86_64_MATURIN_ARGS)
    expected_aarch64_env = _expected_scrubbed_env(
        tmp_path, driver.CORE_AARCH64_MATURIN_ARGS
    )
    core_sdist_path = f"dist/solstone_core-{checker._current_version()}.tar.gz"
    assert calls == [
        (
            ("python3", "scripts/render_packaging.py", "--check"),
            _expected_scrubbed_env(tmp_path, ""),
        ),
        (
            ("uv", "build", "--package", "solstone"),
            expected_x86_env,
        ),
        (
            ("uv", "build", "--package", "solstone-journal"),
            expected_x86_env,
        ),
        (
            ("uv", "build", "--package", "solstone-journal-cuda"),
            expected_x86_env,
        ),
        (
            ("uv", "build", "--package", "solstone-core", "--sdist"),
            _expected_scrubbed_env(tmp_path, ""),
        ),
        (
            ("uv", "build", core_sdist_path, "--wheel", "--out-dir", "dist"),
            expected_x86_env,
        ),
        (
            ("uv", "build", core_sdist_path, "--wheel", "--out-dir", "dist"),
            expected_aarch64_env,
        ),
    ]
    assert all("--exclude" not in argv for argv, _env in calls)
    assert [
        env["MATURIN_PEP517_ARGS"]
        for argv, env in calls
        if argv[:2] == ("uv", "build") and "--wheel" not in argv
    ] == [driver.CORE_X86_64_MATURIN_ARGS] * 3 + [""]
    assert [env["MATURIN_PEP517_ARGS"] for argv, env in calls if "--wheel" in argv] == [
        driver.CORE_X86_64_MATURIN_ARGS,
        driver.CORE_AARCH64_MATURIN_ARGS,
    ]
    assert all("AMBIENT_RELEASE_TOKEN" not in env for _argv, env in calls)
    for _argv, env in calls:
        assert Path(env["ZIG_GLOBAL_CACHE_DIR"]).is_relative_to(tmp_path)
        assert Path(env["ZIG_LOCAL_CACHE_DIR"]).is_relative_to(tmp_path)
    assert {path.name for path in (tmp_path / "dist").iterdir()} == set(
        driver._expected_local_dist_names(include_models=False)
    )


def test_scrubbed_build_env_reports_uncreatable_zig_cache_root(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "target" / "release-zig-cache"
    cache_root.parent.mkdir(parents=True)
    cache_root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(driver.DriverError) as exc:
        driver._scrubbed_build_env(tmp_path, driver.CORE_X86_64_MATURIN_ARGS)

    assert exc.value.failures[0].error == (
        "release Zig cache directory could not be created"
    )
    assert exc.value.failures[0].expected == (
        "writable Zig cache directories under target/release-zig-cache"
    )
    assert "NotADirectoryError" in exc.value.failures[0].actual


def test_default_build_local_dist_honors_include_models_build_selection(
    tmp_path: Path,
) -> None:
    _prepare_fake_build_root(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(
        argv: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(argv))
        _fabricate_local_dist_for_build_argv(
            tmp_path,
            argv,
            include_models=True,
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    driver._default_build_local_dist(tmp_path, include_models=True, runner=runner)

    core_sdist_path = f"dist/solstone_core-{checker._current_version()}.tar.gz"
    assert calls == [
        ("python3", "scripts/render_packaging.py", "--check"),
        ("uv", "build", "--package", "solstone"),
        ("uv", "build", "--package", "solstone-journal"),
        ("uv", "build", "--package", "solstone-journal-cuda"),
        ("uv", "build", "--package", "solstone-journal-models"),
        ("uv", "build", "--package", "solstone-core", "--sdist"),
        ("uv", "build", core_sdist_path, "--wheel", "--out-dir", "dist"),
        ("uv", "build", core_sdist_path, "--wheel", "--out-dir", "dist"),
    ]
    assert all("--exclude" not in call for call in calls)
    assert {path.name for path in (tmp_path / "dist").iterdir()} == set(
        driver._expected_local_dist_names(include_models=True)
    )


@pytest.mark.parametrize("include_models", [False, True])
def test_local_dist_inventory_accepts_owned_reserved_candidate_parent_for_both_model_decisions(
    tmp_path: Path,
    include_models: bool,
) -> None:
    root = _repo(tmp_path)
    _write_expected_local_dist(root, include_models=include_models)
    reserved = _reserved_candidate_path(root)
    (reserved / "retained" / "marker.txt").parent.mkdir(parents=True)
    (reserved / "retained" / "marker.txt").write_text("keep", encoding="utf-8")
    before = _structural_snapshot(reserved)

    driver._validate_local_dist_inventory(root / "dist", include_models=include_models)

    assert _structural_snapshot(reserved) == before


def test_local_dist_inventory_does_not_enumerate_reserved_candidate_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    _write_expected_local_dist(root, include_models=False)
    dist = root / "dist"
    reserved = _reserved_candidate_path(root)
    (reserved / "retained" / "marker.txt").parent.mkdir(parents=True)
    (reserved / "retained" / "marker.txt").write_text("keep", encoding="utf-8")
    original_listdir = os.listdir
    original_scandir = os.scandir
    recorded: list[Path] = []

    def record(path: object) -> None:
        if isinstance(path, int):
            return
        try:
            recorded.append(Path(path))
        except TypeError:
            return

    def listdir(path: object = ".") -> list[str]:
        record(path)
        return original_listdir(path)

    def scandir(path: object = ".") -> object:
        record(path)
        return original_scandir(path)

    monkeypatch.setattr(os, "listdir", listdir)
    monkeypatch.setattr(os, "scandir", scandir)

    driver._validate_local_dist_inventory(dist, include_models=False)

    assert dist in recorded
    assert all(
        path != reserved and not path.is_relative_to(reserved) for path in recorded
    )


def test_local_dist_inventory_accepts_first_release_without_reserved_candidate_parent(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _write_expected_local_dist(root, include_models=False)

    driver._validate_local_dist_inventory(root / "dist", include_models=False)


def test_local_dist_inventory_rejects_denied_dist_read_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _write_expected_local_dist(root, include_models=False)
    dist = root / "dist"
    facts_before = _existing_expected_artifact_facts(root, include_models=False)
    access_calls = _access_spy(monkeypatch, denied_path=dist)

    with pytest.raises(driver.DriverError) as exc:
        driver._validate_local_dist_inventory(dist, include_models=False)

    _assert_dist_preflight_failure(
        exc,
        operation="inventory",
        actual="dist/ lacks read/search access",
        denied_access=True,
    )
    assert access_calls == [(dist, os.R_OK | os.X_OK)]
    assert _existing_expected_artifact_facts(root, include_models=False) == facts_before


def test_fresh_cleanup_preserves_retained_candidates_and_removes_only_current_candidate_transients(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    version = checker._current_version()
    reserved = _reserved_candidate_path(root)
    prior_names = (
        PRIOR_RETAINED_VERSION,
        f"{version}0",
        f"{version}0.payload-staging",
    )
    for name in prior_names:
        marker = reserved / name / "nested" / "marker.txt"
        marker.parent.mkdir(parents=True)
        marker.write_text(f"keep {name}", encoding="utf-8")
    retained_before = {
        name: _structural_snapshot(reserved / name) for name in prior_names
    }
    lock = reserved / ".rust-release-candidate.lock"
    lock.write_text("lock", encoding="utf-8")
    lock_before = lock.read_bytes()
    current_paths = (
        reserved / version,
        reserved / f"{version}.payload-staging",
        reserved / f"{version}.payload-staging.staging",
        reserved / f"{version}.payload-staging.quarantine",
    )
    for path in current_paths:
        (path / "marker.txt").parent.mkdir(parents=True)
        (path / "marker.txt").write_text("stale", encoding="utf-8")
    _write_expected_local_dist(root, include_models=False)
    raw_artifacts = tuple(
        root / "dist" / name
        for name in driver._expected_local_dist_names(include_models=False)
    )

    driver._default_clean_outputs(root, version)

    retained_after_cleanup = {
        name: _structural_snapshot(reserved / name) for name in prior_names
    }
    assert retained_after_cleanup == retained_before
    assert lock.read_bytes() == lock_before
    assert all(not path.exists() for path in current_paths)
    assert all(not path.exists() for path in raw_artifacts)

    _write_expected_local_dist(root, include_models=False)
    driver._validate_local_dist_inventory(root / "dist", include_models=False)
    assert {name: _structural_snapshot(reserved / name) for name in prior_names} == (
        retained_before
    )


@pytest.mark.parametrize(
    ("reserved_kind", "actual"),
    [
        ("symlink", "dist/release-candidate is symlink"),
        ("regular", "dist/release-candidate is regular file"),
        ("fifo", "dist/release-candidate is special file"),
        (
            "denied-directory",
            "dist/release-candidate lacks write/search access",
        ),
    ],
)
def test_fresh_cleanup_rejects_unsafe_reserved_candidate_parent_before_any_mutation(
    tmp_path: Path,
    reserved_kind: str,
    actual: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    version = checker._current_version()
    build_keep = root / "build" / "keep"
    build_keep.parent.mkdir()
    build_keep.write_text("keep", encoding="utf-8")
    reserved = _reserved_candidate_path(root)
    reserved.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside_before: tuple[TreeSnapshotEntry, ...] | None = None
    if reserved_kind == "symlink":
        marker = outside / version / "nested" / "keep.txt"
        marker.parent.mkdir(parents=True)
        marker.write_text("keep", encoding="utf-8")
        outside_before = _structural_snapshot(outside)
        reserved.symlink_to(outside, target_is_directory=True)
    elif reserved_kind == "regular":
        reserved.write_text("unsafe", encoding="utf-8")
    elif reserved_kind == "fifo":
        os.mkfifo(reserved)
    elif reserved_kind == "denied-directory":
        reserved.mkdir()

    access_calls = _access_spy(
        monkeypatch,
        denied_path=reserved if reserved_kind == "denied-directory" else None,
    )

    with pytest.raises(driver.DriverError) as exc:
        driver._default_clean_outputs(root, version)

    denied_access = reserved_kind == "denied-directory"
    _assert_reserved_parent_failure(
        exc,
        operation="cleanup",
        actual=actual,
        denied_access=denied_access,
    )
    expected_access_calls = [(root / "dist", os.R_OK | os.W_OK | os.X_OK)]
    if denied_access:
        expected_access_calls.append((reserved, os.W_OK | os.X_OK))
    assert access_calls == expected_access_calls
    assert build_keep.read_text(encoding="utf-8") == "keep"
    if reserved_kind == "symlink":
        assert reserved.is_symlink()
        assert outside_before is not None
        assert _structural_snapshot(outside) == outside_before


@pytest.mark.parametrize(
    ("dist_kind", "actual"),
    [
        ("symlink", "dist/ is symlink"),
        ("regular", "dist/ is regular file"),
        ("fifo", "dist/ is special file"),
        ("denied-directory", "dist/ lacks read/write/search access"),
    ],
)
def test_fresh_cleanup_rejects_unsafe_dist_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dist_kind: str,
    actual: str,
) -> None:
    root = _repo(tmp_path)
    version = checker._current_version()
    dist = root / "dist"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external-target" / "marker.txt").parent.mkdir()
    (outside / "external-target" / "marker.txt").write_text(
        "external", encoding="utf-8"
    )
    if dist_kind == "symlink":
        _write_cleanup_preflight_sentinels(root, outside, version=version)
        dist.symlink_to(outside, target_is_directory=True)
    elif dist_kind == "regular":
        _write_cleanup_preflight_sentinels(root, None, version=version)
        dist.write_text("unsafe", encoding="utf-8")
    elif dist_kind == "fifo":
        _write_cleanup_preflight_sentinels(root, None, version=version)
        os.mkfifo(dist)
    elif dist_kind == "denied-directory":
        _write_cleanup_preflight_sentinels(root, dist, version=version)

    root_before = _structural_snapshot(root)
    outside_before = _structural_snapshot(outside)
    blocked_calls: list[str] = []

    def fail_if_called(name: str) -> object:
        def wrapper(*_args: object, **_kwargs: object) -> object:
            blocked_calls.append(name)
            raise AssertionError(f"{name} must not run after failed dist preflight")

        return wrapper

    for name in (
        "_remove_owned_path",
        "_remove_owned_relative",
        "_owned_glob",
        "_clean_raw_dist_outputs",
        "_payload_transient_paths",
    ):
        monkeypatch.setattr(driver, name, fail_if_called(name))
    access_calls = _access_spy(
        monkeypatch,
        denied_path=dist if dist_kind == "denied-directory" else None,
    )
    with monkeypatch.context() as enumeration_patch:
        enumerated = _enumeration_spy(enumeration_patch)

        with pytest.raises(driver.DriverError) as exc:
            driver._default_clean_outputs(root, version)

        assert blocked_calls == []
        assert all(not _same_or_descendant(path, dist) for path in enumerated)

    _assert_dist_preflight_failure(
        exc,
        operation="cleanup",
        actual=actual,
        denied_access=dist_kind == "denied-directory",
    )
    expected_access_calls: list[tuple[Path, int]] = []
    if dist_kind == "denied-directory":
        expected_access_calls.append((dist, os.R_OK | os.W_OK | os.X_OK))
    assert access_calls == expected_access_calls
    assert _structural_snapshot(root) == root_before
    assert _structural_snapshot(outside) == outside_before


@pytest.mark.parametrize(
    ("reserved_kind", "actual"),
    [
        ("symlink", "dist/release-candidate is symlink"),
        ("regular", "dist/release-candidate is regular file"),
        ("fifo", "dist/release-candidate is special file"),
    ],
)
def test_local_dist_inventory_rejects_unsafe_reserved_candidate_parent_without_mutating_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reserved_kind: str,
    actual: str,
) -> None:
    root = _repo(tmp_path)
    _write_expected_local_dist(root, include_models=False)
    facts_before = _existing_expected_artifact_facts(root, include_models=False)
    reserved = _reserved_candidate_path(root)
    outside = tmp_path / "outside"
    outside_before: tuple[TreeSnapshotEntry, ...] | None = None
    if reserved_kind == "symlink":
        marker = outside / "nested" / "keep.txt"
        marker.parent.mkdir(parents=True)
        marker.write_text("keep", encoding="utf-8")
        outside_before = _structural_snapshot(outside)
        reserved.symlink_to(outside, target_is_directory=True)
    elif reserved_kind == "regular":
        reserved.write_text("unsafe", encoding="utf-8")
    elif reserved_kind == "fifo":
        os.mkfifo(reserved)
    access_calls = _access_spy(monkeypatch)

    with pytest.raises(driver.DriverError) as exc:
        driver._validate_local_dist_inventory(root / "dist", include_models=False)

    _assert_reserved_parent_failure(exc, operation="inventory", actual=actual)
    assert access_calls == [(root / "dist", os.R_OK | os.X_OK)]
    assert _existing_expected_artifact_facts(root, include_models=False) == facts_before
    if reserved_kind == "symlink":
        assert reserved.is_symlink()
        assert outside_before is not None
        assert _structural_snapshot(outside) == outside_before


def test_local_dist_inventory_accepts_reserved_directory_without_reserved_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    _write_expected_local_dist(root, include_models=False)
    reserved = _reserved_candidate_path(root)
    marker = reserved / "retained" / "marker.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("keep", encoding="utf-8")
    reserved.chmod(0o700)
    before = _structural_snapshot(reserved)
    access_calls = _access_spy(monkeypatch, denied_path=reserved)

    try:
        reserved.chmod(0)
        driver._validate_local_dist_inventory(root / "dist", include_models=False)
    finally:
        reserved.chmod(0o700)

    assert access_calls == [(root / "dist", os.R_OK | os.X_OK)]
    assert _structural_snapshot(reserved) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign-directory",
        "foreign-symlink",
        "foreign-special",
        "extra-regular-file",
        "missing-expected-file",
        "wrong-model-package-set",
    ],
)
def test_local_dist_inventory_rejects_foreign_entries_with_reserved_candidate_parent_present(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = _repo(tmp_path)
    _write_expected_local_dist(root, include_models=False)
    reserved = _reserved_candidate_path(root)
    reserved.mkdir(parents=True)
    expected = driver._expected_local_dist_names(include_models=False)
    if mutation == "foreign-directory":
        (root / "dist" / "foreign-dir").mkdir()
    elif mutation == "foreign-symlink":
        (root / "dist" / "foreign-link").symlink_to(next(iter(sorted(expected))))
    elif mutation == "foreign-special":
        os.mkfifo(root / "dist" / "foreign-pipe")
    elif mutation == "extra-regular-file":
        (root / "dist" / "extra.whl").write_bytes(b"extra")
    elif mutation == "missing-expected-file":
        missing = next(
            name
            for name in sorted(expected)
            if name.startswith("solstone-") and name.endswith(".tar.gz")
        )
        (root / "dist" / missing).unlink()
    elif mutation == "wrong-model-package-set":
        extra_models = driver._expected_local_dist_names(include_models=True) - expected
        for name in extra_models:
            (root / "dist" / name).write_bytes(b"model")
    facts_before = _existing_expected_artifact_facts(root, include_models=False)

    with pytest.raises(driver.DriverError):
        driver._validate_local_dist_inventory(root / "dist", include_models=False)

    assert _existing_expected_artifact_facts(root, include_models=False) == facts_before


def test_fresh_cleanup_removes_nested_egg_infos_request_siblings_and_staging(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    version = checker._current_version()
    paths = [
        root / "packages" / "solstone-journal" / "solstone_journal.egg-info",
        root / "packages" / "solstone-journal-cuda" / "solstone_journal_cuda.egg-info",
        root
        / "packages"
        / "solstone-journal-models"
        / "solstone_journal_models.egg-info",
        root / "target" / "release-transfer" / f".{version}.request-abc123",
        root / "target" / "release-transfer" / f".{version}.source.bundle",
        root / "target" / "release-evidence" / f"{version}.staging",
        root / "target" / "release-zig-cache",
        root / "dist" / "release-candidate" / f"{version}.payload-staging.staging",
        root / "dist" / "release-candidate" / f"{version}.payload-staging.quarantine",
    ]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        (path / "marker").write_text("stale", encoding="utf-8")

    driver._default_clean_outputs(root, version)

    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("relative", ["packages/solstone-journal/bad.egg-info", "dist"])
def test_fresh_cleanup_preserves_symlink_targets_and_surfaces_residue(
    tmp_path: Path, relative: str
) -> None:
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    link = root / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(driver.DriverError) as exc:
        driver._default_clean_outputs(root, checker._current_version())

    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert link.is_symlink()
    if relative == "dist":
        _assert_dist_preflight_failure(
            exc,
            operation="cleanup",
            actual="dist/ is symlink",
        )
    else:
        assert any("symlink residue" in failure.error for failure in exc.value.failures)


@pytest.mark.parametrize(
    "args",
    [
        driver.CORE_X86_64_MATURIN_ARGS.replace("--locked ", ""),
        driver.CORE_X86_64_MATURIN_ARGS.replace("--zig ", ""),
        driver.CORE_X86_64_MATURIN_ARGS.replace("--compatibility manylinux2014 ", ""),
        driver.CORE_X86_64_MATURIN_ARGS.replace(
            "--compatibility manylinux2014", "--compatibility manylinux_2_28"
        ),
        driver.CORE_X86_64_MATURIN_ARGS.replace(
            "--target x86_64-unknown-linux-musl", ""
        ),
        driver.CORE_X86_64_MATURIN_ARGS.replace(
            "x86_64-unknown-linux-musl", "x86_64-unknown-linux-gnu"
        ),
    ],
)
def test_linux_maturin_contract_rejects_missing_or_wrong_tokens(args: str) -> None:
    failures = driver.validate_linux_maturin_args(
        args,
        target="x86_64-unknown-linux-musl",
    )
    assert failures


@pytest.mark.parametrize(
    "mutation",
    [
        "record_role",
        "record_paths_swapped",
        "wrong_tag",
        "member",
        "tool",
        "signing",
        "notary",
        "wheel_hash",
    ],
)
def test_candidate_rejects_native_record_mismatches(
    tmp_path: Path, mutation: str
) -> None:
    root = _repo(tmp_path)

    with pytest.raises(driver.DriverError):
        driver.run_candidate(root, _env(), _services(root, native_mutation=mutation))


def test_candidate_revalidates_macos_wheel_bytes_after_copy_before_ledger(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    services = _services(root)

    def cleanup(paths: Sequence[Path]) -> None:
        root_name, _core_name = _macos_wheel_names()
        wheel_path = root / "dist" / root_name
        if wheel_path.exists():
            write_platform_base_wheel(
                wheel_path.parent,
                helper_binary=b"mutated-helper",
                version=checker._current_version(),
            )
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()

    services = replace(services, cleanup_transients=cleanup)

    with pytest.raises(driver.DriverError) as exc:
        driver.run_candidate(root, _env(), services)

    assert any(
        failure.error == "release candidate wheel content check failed"
        and PARAKEET_HELPER_MEMBER in failure.actual
        for failure in exc.value.failures
    )


def test_candidate_rejects_poisoned_core_wheel_content(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    base_services = _services(root)

    def build_local_dist(repo: Path, include_models: bool) -> None:
        base_services.build_local_dist(repo, include_models)
        write_core_wheel(
            repo / "dist",
            tag="manylinux_2_17_x86_64.manylinux2014_x86_64",
            binary=b"not an elf",
            version=checker._current_version(),
        )

    services = replace(base_services, build_local_dist=build_local_dist)

    with pytest.raises(driver.DriverError) as exc:
        driver.run_candidate(root, _env(), services)

    _assert_no_ready_cohort(root)
    assert any(
        failure.error == "release candidate wheel content check failed"
        and ".data/scripts/solstone-core" in failure.actual
        and "ELF binary is too short" in failure.actual
        for failure in exc.value.failures
    )


def test_candidate_rejects_coordinator_sourced_macos_tool_evidence(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    services = _services(root)
    base = services.coordinator_tool_evidence()
    services = replace(
        services,
        coordinator_tool_evidence=lambda: {
            **base,
            "macos-arm64": pins.fixture_lane_tool_evidence("macos-arm64"),
        },
    )

    with pytest.raises(driver.DriverError) as exc:
        driver.run_candidate(root, _env(), services)

    assert (
        exc.value.failures[0].error
        == "macOS release tool evidence must be attested by the build host"
    )


def test_candidate_rejects_forged_host_macos_tool_evidence(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    services = _services(root)

    def build_host(
        source_bundle: SourceBundle, commit: str, output_dir: Path
    ) -> BuildHostResult:
        result = _write_macos_host_outputs(output_dir)
        tools = dict(result.tool_evidence)
        tools["swift"] = "Apple Swift 6.3.3"
        return BuildHostResult(
            macos_wheels=result.macos_wheels,
            native_records=result.native_records,
            tool_evidence=tools,
        )

    services = replace(services, build_host=build_host)

    with pytest.raises(driver.DriverError) as exc:
        driver.run_candidate(root, _env(), services)

    assert any(
        failure.error == "pre-sign lane tool swift is not pinned"
        for failure in exc.value.failures
    )


def test_candidate_derives_manifest_evidence_from_single_frozen_tool_observation(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    calls = 0

    def coordinator_tool_evidence() -> dict[str, dict[str, str]]:
        nonlocal calls
        calls += 1
        return {
            lane: pins.fixture_lane_tool_evidence(lane)
            for lane in ("source", "linux-x86_64-musl", "linux-aarch64-musl")
        }

    services = replace(
        _services(root), coordinator_tool_evidence=coordinator_tool_evidence
    )
    report = driver.run_candidate(root, _env(), services)
    payload = json.loads((report.evidence_dir / "ledger.json").read_text())

    assert calls == 1
    assert payload["tool_evidence"]["source"]["uv"] == pins.UV_LINUX_FIXTURE_BANNER
    assert payload["tool_evidence"]["source"]["maturin"] == pins.MATURIN_PIN
    source_artifact = next(
        name
        for name, (lane, _target) in checker.rust_artifact_targets().items()
        if lane == "source"
    )
    manifest = json.loads(
        (
            report.release_dir / f"{source_artifact}.rust-release-manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["native_tools"]["uv"] == pins.UV_LINUX_FIXTURE_BANNER


def test_recovery_rejects_native_member_path_mutation_with_matching_hash(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    report = driver.run_candidate(root, _env(), _services(root))
    ledger_path = report.evidence_dir / "ledger.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    payload["native_members"]["linux-x86_64-musl"]["solstone-core"]["path"] = (
        "forged/path/solstone-core"
    )
    ledger_path.write_bytes(checker.canonical_json_bytes(payload))

    with pytest.raises(driver.DriverError) as exc:
        _recover(root)

    assert (
        exc.value.failures[0].error
        == "retained ledger native_members do not match finalized wheels"
    )


def test_proof_binding_surfaces_target_install_parse_failure(tmp_path: Path) -> None:
    target = "linux-x86_64-musl"
    digest = "a" * 64
    ledger_sha256 = "b" * 64
    ledger_payload = {
        "source_commit": SOURCE_COMMIT,
        "core_lock_sha256": LOCK_SHA,
        "candidate": {"files": []},
        "native_members": {
            target: {
                "solstone-core": {
                    "path": "solstone_core-0.9.0.data/scripts/solstone-core",
                    "sha256": "d" * 64,
                    "bytes": 5,
                }
            }
        },
    }
    proof = {
        "target": target,
        "source_commit": SOURCE_COMMIT,
        "candidate_digest": digest,
        "ledger_sha256": ledger_sha256,
        "core_lock_sha256": LOCK_SHA,
        "candidate_files": [],
        "installed_members": [
            {
                "name": "solstone-core",
                "wheel_member_path": ("solstone_core-0.9.0.data/scripts/solstone-core"),
                "installed_path": "ENVROOT/bin/solstone-core",
                "sha256": "d" * 64,
            }
        ],
    }

    failures = driver._validate_proof_binding(
        proof,
        target=target,
        ledger=ledger_payload,
        digest=digest,
        ledger_sha256=ledger_sha256,
        release_dir=tmp_path,
    )

    assert any(
        failure.error == "install proof target install set is empty"
        for failure in failures
    )


def test_recovery_rejects_empty_linux_native_member_set(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    report = driver.run_candidate(root, _env(), _services(root))
    ledger_path = report.evidence_dir / "ledger.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    payload["native_members"]["linux-x86_64-musl"] = {}
    ledger_path.write_bytes(checker.canonical_json_bytes(payload))

    with pytest.raises(driver.DriverError) as exc:
        _recover(root)

    assert any(
        "native member set is invalid" in failure.error
        for failure in exc.value.failures
    )


def test_recovery_rejects_self_consistent_native_member_forgery(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    report = driver.run_candidate(root, _env(), _services(root))
    ledger_path = report.evidence_dir / "ledger.json"
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    forged = payload["native_members"]["linux-x86_64-musl"]["solstone-core"]
    forged["path"] = "forged/path/solstone-core"
    forged["sha256"] = "0" * 64
    ledger_path.write_bytes(checker.canonical_json_bytes(payload))
    forged_ledger_sha = hashlib.sha256(ledger_path.read_bytes()).hexdigest()

    for proof_path in sorted((report.evidence_dir / "proofs").glob("*.json")):
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        proof["ledger_sha256"] = forged_ledger_sha
        if proof["target"] == "linux-x86_64-musl":
            proof["installed_members"] = [
                {
                    "name": name,
                    "wheel_member_path": member["path"],
                    "installed_path": f"ENVROOT/bin/{name}",
                    "sha256": member["sha256"],
                }
                for name, member in sorted(
                    payload["native_members"]["linux-x86_64-musl"].items()
                )
            ]
        proof_path.write_bytes(checker.canonical_json_bytes(proof))

    with pytest.raises(driver.DriverError) as exc:
        _recover(root)

    assert (
        exc.value.failures[0].error
        == "retained ledger native_members do not match finalized wheels"
    )
