#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Provider-neutral release-candidate finalizer driver."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.check_release_preflight import (
    LANE_TOOL_KEYS,
    check_lane_tool_evidence,
    collect_lane_tool_evidence,
    expected_lane_tool_evidence,
    finalize_macos_tool_evidence,
)
from scripts.check_rust_release_manifest import (
    LANES,
    NATIVE_TOOL_KEYS,
    SOURCE_COMMIT_RE,
    Failure,
    LaneEvidence,
    _format_failures,
    build_and_promote_candidate,
    canonical_json_bytes,
    expected_package_names,
    rust_artifact_targets,
    validate_release_dir,
)
from scripts.check_wheel_contents import (
    EXPECTED_MODEL_SHA256,
    MAX_BASE_WHEEL_BYTES,
    check_dist,
)
from scripts.record_macos_native_wheel import validate_macos_native_record
from scripts.release_advisory_policy import (
    PolicyRun,
    is_normalized_utc_timestamp,
    prepare_policy_run,
    validate_snapshot_identity,
)
from scripts.release_build_host import (
    BuildHostResult,
    ExternalBuildHostChannel,
    SourceBundle,
    create_source_bundle,
)
from scripts.release_digest import bundle_digest, candidate_digest, file_sha256_size
from scripts.release_install_smoke import (
    CANDIDATE,
    ENVROOT,
    PROOF_TARGETS,
    RETAINED_PROOF_REPAIR,
    InstallProofError,
    candidate_file_entries,
    target_install_paths_from_ledger,
    validate_install_proof_bytes,
)
from scripts.release_ledger import (
    LedgerError,
    read_retained_ledger,
    validate_native_members_against_release_dir,
    write_ledger,
)
from scripts.release_proof_host import (
    ProofHostError,
    proof_channels_from_env,
    run_install_proof_with_channels,
)
from scripts.release_public_evidence import validate_public_evidence_tree
from scripts.release_tool_pins import (
    RUSTC_BINARY_PIN,
    RUSTC_COMMIT_DATE_PIN,
    RUSTC_COMMIT_HASH_PIN,
    RUSTC_LLVM_PIN,
    RUSTC_RELEASE_PIN,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]

CORE_X86_64_MATURIN_ARGS = (
    "--locked --zig --compatibility manylinux2014 --target x86_64-unknown-linux-musl"
)
CORE_AARCH64_MATURIN_ARGS = (
    "--locked --zig --compatibility manylinux2014 --target aarch64-unknown-linux-musl"
)


@dataclass(frozen=True)
class CandidateReport:
    heading: str
    version: str
    release_dir: Path
    evidence_dir: Path
    payload_files: int
    candidate_digest: str
    ledger_sha256: str
    proof_sha256: Mapping[str, str]
    bundle_digest: str


@dataclass(frozen=True)
class DryRunPlan:
    models_decision: str
    artifacts: tuple[str, ...]
    tool_evidence: Mapping[str, Mapping[str, str]]
    linux_maturin_args: Mapping[str, str]
    publication_lockout: Mapping[str, bool]


@dataclass(frozen=True)
class CandidateServices:
    git_head: Callable[[Path], str]
    git_status: Callable[[Path], str]
    core_lock_sha256: Callable[[Path], str]
    clean_outputs: Callable[[Path, str], None]
    build_local_dist: Callable[[Path, bool], None]
    prepare_policy: Callable[[Path, Mapping[str, str]], PolicyRun]
    coordinator_tool_evidence: Callable[[], Mapping[str, Mapping[str, str]]]
    create_source_bundle: Callable[[Path, str, Path], SourceBundle]
    build_host: Callable[[SourceBundle, str, Path], BuildHostResult]
    cleanup_transients: Callable[[Sequence[Path]], None]
    run_install_proof: Callable[..., Path]
    transaction_hook: Callable[[str], None]


class DriverError(RuntimeError):
    def __init__(self, failures: Sequence[Failure]) -> None:
        self.failures = tuple(failures)
        super().__init__("; ".join(failure.error for failure in self.failures))


def _failure(error: str, *, expected: str, actual: str, repair: str) -> Failure:
    return Failure(error=error, expected=expected, actual=actual, repair=repair)


def _run_stdout(
    runner: Runner,
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> str:
    result = runner(
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DriverError(
            [
                _failure(
                    "release driver command failed",
                    expected="exit 0",
                    actual=result.stderr.strip()
                    or result.stdout.strip()
                    or f"exit {result.returncode}",
                    repair="bash scripts/release.sh --candidate",
                )
            ]
        )
    return result.stdout.strip()


def _project_version(root: Path) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _local_models_package_version(root: Path) -> str:
    data = tomllib.loads(
        (root / "packages" / "solstone-journal-models" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    return str(data["project"]["version"])


def _default_git_head(root: Path) -> str:
    return _run_stdout(subprocess.run, ["git", "rev-parse", "HEAD"], cwd=root)


def _default_git_status(root: Path) -> str:
    return _run_stdout(
        subprocess.run,
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=root,
    )


def _default_core_lock_sha256(root: Path) -> str:
    digest, _bytes = file_sha256_size(root / "core" / "Cargo.lock")
    return digest


def _is_missing(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def _remove_owned_path(path: Path, *, label: str) -> list[Failure]:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return []
    if stat.S_ISLNK(entry.st_mode):
        return [
            _failure(
                "fresh release cleanup refused symlink residue",
                expected=f"{label} owned non-symlink path",
                actual=path.name,
                repair="bash scripts/release.sh --candidate",
            )
        ]
    try:
        if stat.S_ISDIR(entry.st_mode):
            shutil.rmtree(path)
        elif stat.S_ISREG(entry.st_mode):
            path.unlink()
        else:
            return [
                _failure(
                    "fresh release cleanup refused non-regular residue",
                    expected=f"{label} owned directory or regular file",
                    actual=path.name,
                    repair="bash scripts/release.sh --candidate",
                )
            ]
    except OSError as exc:
        return [
            _failure(
                "fresh release cleanup could not remove owned residue",
                expected=f"{label} removed",
                actual=type(exc).__name__,
                repair="bash scripts/release.sh --candidate",
            )
        ]
    return []


def _remove_owned_relative(root: Path, relative: Path) -> list[Failure]:
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            entry = current.lstat()
        except FileNotFoundError:
            return []
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
            return [
                _failure(
                    "fresh release cleanup refused unsafe parent",
                    expected=f"{relative.as_posix()} parent is an owned directory",
                    actual=part,
                    repair="bash scripts/release.sh --candidate",
                )
            ]
    return _remove_owned_path(root / relative, label=relative.as_posix())


def _owned_glob(
    parent: Path, pattern: str, *, label: str
) -> tuple[list[Path], list[Failure]]:
    try:
        entry = parent.lstat()
    except FileNotFoundError:
        return [], []
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        return [], [
            _failure(
                "fresh release cleanup refused unsafe parent",
                expected=f"{label} owned non-symlink directory",
                actual=parent.name,
                repair="bash scripts/release.sh --candidate",
            )
        ]
    return list(parent.glob(pattern)), []


def _clean_raw_dist_outputs(root: Path) -> list[Failure]:
    dist = root / "dist"
    try:
        entry = dist.lstat()
    except FileNotFoundError:
        return []
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        return _remove_owned_path(dist, label="dist")
    failures: list[Failure] = []
    try:
        children = sorted(dist.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        return [
            _failure(
                "fresh release cleanup could not inspect raw dist outputs",
                expected="readable dist directory",
                actual=type(exc).__name__,
                repair="bash scripts/release.sh --candidate",
            )
        ]
    for child in children:
        if child.name == "release-candidate":
            continue
        failures.extend(_remove_owned_path(child, label=f"dist/{child.name}"))
    return failures


def _payload_transient_paths(root: Path, version: str) -> tuple[Path, ...]:
    ready_path = root / "dist" / "release-candidate" / version
    payload_staging = ready_path.parent / f"{version}.payload-staging"
    return (
        ready_path,
        payload_staging,
        payload_staging.parent / f"{payload_staging.name}.staging",
        payload_staging.parent / f"{payload_staging.name}.quarantine",
    )


def _default_clean_outputs(root: Path, version: str) -> None:
    failures: list[Failure] = []
    failures.extend(_remove_owned_path(root / "build", label="build"))
    failures.extend(_clean_raw_dist_outputs(root))
    root_egg_infos, root_glob_failures = _owned_glob(
        root, "*.egg-info", label="repository root"
    )
    failures.extend(root_glob_failures)
    for egg_info in root_egg_infos:
        failures.extend(_remove_owned_path(egg_info, label="root egg-info"))
    for package in (
        "solstone-journal",
        "solstone-journal-cuda",
        "solstone-journal-models",
    ):
        package_dir = root / "packages" / package
        egg_infos, glob_failures = _owned_glob(
            package_dir, "*.egg-info", label=f"{package} package directory"
        )
        failures.extend(glob_failures)
        for egg_info in egg_infos:
            failures.extend(_remove_owned_path(egg_info, label=f"{package} egg-info"))
    for relative in (
        Path("target") / "release-evidence" / version,
        Path("target") / "release-evidence" / f"{version}.staging",
        Path("target") / "release-transfer" / version,
    ):
        failures.extend(_remove_owned_relative(root, relative))
    for path in _payload_transient_paths(root, version):
        failures.extend(_remove_owned_path(path, label=path.name))
    transfer_parent = root / "target" / "release-transfer"
    request_siblings, request_failures = _owned_glob(
        transfer_parent,
        f".{version}.request-*",
        label="release transfer directory",
    )
    failures.extend(request_failures)
    for path in request_siblings:
        failures.extend(_remove_owned_path(path, label="release transfer request"))
    if failures:
        raise DriverError(failures)


def _linux_maturin_tokens(target: str) -> tuple[str, ...]:
    return (
        "--locked",
        "--zig",
        "--compatibility",
        "manylinux2014",
        "--target",
        target,
    )


def validate_linux_maturin_args(args: str, *, target: str) -> list[Failure]:
    try:
        tokens = tuple(shlex.split(args))
    except ValueError as exc:
        return [
            _failure(
                "Linux maturin arguments are not parseable",
                expected="exact Linux maturin token contract",
                actual=str(exc),
                repair="bash scripts/release.sh --candidate",
            )
        ]
    expected = _linux_maturin_tokens(target)
    if tokens != expected:
        return [
            _failure(
                "Linux maturin arguments do not match release contract",
                expected=" ".join(expected),
                actual=" ".join(tokens),
                repair="bash scripts/release.sh --candidate",
            )
        ]
    return []


def _scrubbed_build_env(maturin_args: str) -> dict[str, str]:
    return {
        "MATURIN_PEP517_ARGS": maturin_args,
        "PATH": os.environ.get("PATH", ""),
        "PYTHONNOUSERSITE": "1",
    }


def _default_build_local_dist(
    root: Path,
    include_models: bool,
    *,
    runner: Runner = subprocess.run,
) -> None:
    linux_contracts = (
        (CORE_X86_64_MATURIN_ARGS, "x86_64-unknown-linux-musl"),
        (CORE_AARCH64_MATURIN_ARGS, "aarch64-unknown-linux-musl"),
    )
    failures: list[Failure] = []
    for args, target in linux_contracts:
        failures.extend(validate_linux_maturin_args(args, target=target))
    if failures:
        raise DriverError(failures)
    _run_stdout(
        runner,
        ["python3", "scripts/render_packaging.py", "--check"],
        cwd=root,
        env=_scrubbed_build_env(""),
    )
    x86_argv = ["uv", "build", "--all-packages"]
    if not include_models:
        x86_argv.extend(["--exclude", "solstone-journal-models"])
    _run_stdout(
        runner,
        x86_argv,
        cwd=root,
        env=_scrubbed_build_env(CORE_X86_64_MATURIN_ARGS),
    )
    _run_stdout(
        runner,
        ["uv", "build", "--package", "solstone-core", "--wheel"],
        cwd=root,
        env=_scrubbed_build_env(CORE_AARCH64_MATURIN_ARGS),
    )
    _validate_local_dist_inventory(root / "dist", include_models=include_models)


def _default_prepare_policy(root: Path, env: Mapping[str, str]) -> PolicyRun:
    return prepare_policy_run(
        root,
        advisory_source_id=env["RELEASE_ADVISORY_SOURCE_NAME"],
        db_urls=(env["RELEASE_ADVISORY_DB_URL"],),
        db_root=Path(env["RELEASE_ADVISORY_DB_ROOT"]),
    )


def _default_create_source_bundle(
    root: Path, commit: str, output_path: Path
) -> SourceBundle:
    return create_source_bundle(root, expected_commit=commit, output_path=output_path)


def _default_build_host_from_env(
    env: Mapping[str, str],
) -> Callable[[SourceBundle, str, Path], BuildHostResult]:
    channel = ExternalBuildHostChannel.from_env(env)

    def build_host(
        source_bundle: SourceBundle, commit: str, output_dir: Path
    ) -> BuildHostResult:
        return channel.build_macos(
            source_bundle=source_bundle,
            expected_commit=commit,
            output_dir=output_dir,
        )

    return build_host


def _default_coordinator_tool_evidence() -> dict[str, dict[str, str]]:
    return {
        lane: collect_lane_tool_evidence(lane)
        for lane in ("source", "linux-x86_64-musl", "linux-aarch64-musl")
    }


def _default_cleanup_transients(paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()
        except OSError as exc:
            raise DriverError(
                [
                    _failure(
                        "release transient cleanup failed",
                        expected="owned release transients removed",
                        actual=type(exc).__name__,
                        repair="bash scripts/release.sh --candidate",
                    )
                ]
            ) from None


def default_services(env: Mapping[str, str] | None = None) -> CandidateServices:
    build_host = (
        _default_build_host_from_env(env) if env is not None else _missing_build_host
    )
    proof_runner = (
        _default_proof_hosts_from_env(env) if env is not None else _missing_proof_host
    )
    return CandidateServices(
        git_head=_default_git_head,
        git_status=_default_git_status,
        core_lock_sha256=_default_core_lock_sha256,
        clean_outputs=_default_clean_outputs,
        build_local_dist=_default_build_local_dist,
        prepare_policy=_default_prepare_policy,
        coordinator_tool_evidence=_default_coordinator_tool_evidence,
        create_source_bundle=_default_create_source_bundle,
        build_host=build_host,
        cleanup_transients=_default_cleanup_transients,
        run_install_proof=proof_runner,
        transaction_hook=lambda _point: None,
    )


def _missing_build_host(
    _source_bundle: SourceBundle, _commit: str, _output_dir: Path
) -> BuildHostResult:
    raise DriverError(
        [
            _failure(
                "build-host service is not injected",
                expected="external build-host channel",
                actual="<missing>",
                repair="bash scripts/release.sh --candidate",
            )
        ]
    )


def _default_proof_hosts_from_env(env: Mapping[str, str]) -> Callable[..., Path]:
    try:
        channels = proof_channels_from_env(env)
    except ProofHostError as exc:
        raise DriverError(exc.failures) from None

    def proof_runner(**kwargs: Any) -> Path:
        try:
            return run_install_proof_with_channels(channels, **kwargs)
        except ProofHostError as exc:
            raise DriverError(exc.failures) from None

    return proof_runner


def _missing_proof_host(**_kwargs: Any) -> Path:
    raise DriverError(
        [
            _failure(
                "proof-host service is not injected",
                expected="configured proof-host channels for all targets",
                actual="<missing>",
                repair="bash scripts/release.sh --candidate",
            )
        ]
    )


def _expected_commit(env: Mapping[str, str]) -> str:
    value = env.get("EXPECTED_RELEASE_COMMIT", "")
    if not SOURCE_COMMIT_RE.fullmatch(value):
        raise DriverError(
            [
                _failure(
                    "EXPECTED_RELEASE_COMMIT is invalid",
                    expected="40 or 64 lowercase hexadecimal characters",
                    actual=value or "<missing>",
                    repair="bash scripts/release.sh --candidate",
                )
            ]
        )
    return value


def _models_include(env: Mapping[str, str]) -> bool:
    value = env.get("RELEASE_MODEL_PACKAGES", "")
    if value not in {"include", "exclude"}:
        raise DriverError(
            [
                _failure(
                    "release model package decision is invalid",
                    expected="RELEASE_MODEL_PACKAGES=include or exclude",
                    actual=value or "<missing>",
                    repair="bash scripts/release.sh --candidate",
                )
            ]
        )
    return value == "include"


def _assert_clean_identity(
    root: Path,
    *,
    expected_commit: str,
    expected_lock_sha256: str,
    services: CandidateServices,
) -> None:
    head = services.git_head(root)
    if head != expected_commit:
        raise DriverError(
            [
                _failure(
                    "release source commit does not match EXPECTED_RELEASE_COMMIT",
                    expected=expected_commit,
                    actual=head,
                    repair="bash scripts/release.sh --candidate",
                )
            ]
        )
    status = services.git_status(root)
    if status:
        raise DriverError(
            [
                _failure(
                    "release source tree is not clean",
                    expected="empty git status",
                    actual=status,
                    repair="bash scripts/release.sh --candidate",
                )
            ]
        )
    actual_lock = services.core_lock_sha256(root)
    if actual_lock != expected_lock_sha256:
        raise DriverError(
            [
                _failure(
                    "core lock hash changed before finalization",
                    expected=expected_lock_sha256,
                    actual=actual_lock,
                    repair="bash scripts/release.sh --candidate",
                )
            ]
        )


def _macos_wheel_role(path: Path) -> str | None:
    name = path.name
    expected_wheels = expected_package_names(include_models=False)
    expected_root = next(
        item
        for item in expected_wheels
        if item.startswith("solstone-") and "macosx_14_0_arm64" in item
    )
    expected_core = next(
        item
        for item in expected_wheels
        if item.startswith("solstone_core-") and "macosx_14_0_arm64" in item
    )
    if name == expected_core:
        return "core"
    if name == expected_root:
        return "root"
    return None


def _macos_wheel_names() -> frozenset[str]:
    return frozenset(
        name
        for name in expected_package_names(include_models=False)
        if _macos_wheel_role(Path(name)) is not None
    )


def _expected_local_dist_names(*, include_models: bool) -> frozenset[str]:
    return (
        frozenset(expected_package_names(include_models=include_models))
        - _macos_wheel_names()
    )


def _validate_local_dist_inventory(dist_dir: Path, *, include_models: bool) -> None:
    failures: list[Failure] = []
    if _is_missing(dist_dir):
        raise DriverError(
            [
                _failure(
                    "local release build did not produce dist",
                    expected="dist directory with local package artifacts",
                    actual="missing",
                    repair="bash scripts/release.sh --candidate",
                )
            ]
        )
    try:
        entry = dist_dir.lstat()
    except FileNotFoundError:
        entry = None
    if entry is None or stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        raise DriverError(
            [
                _failure(
                    "local release build dist is not an owned directory",
                    expected="non-symlink dist directory",
                    actual=dist_dir.name,
                    repair="bash scripts/release.sh --candidate",
                )
            ]
        )
    names: set[str] = set()
    for path in dist_dir.iterdir():
        child = path.lstat()
        if stat.S_ISREG(child.st_mode):
            names.add(path.name)
        else:
            failures.append(
                _failure(
                    "local release build produced unsafe dist entry",
                    expected="regular package artifact files only",
                    actual=path.name,
                    repair="bash scripts/release.sh --candidate",
                )
            )
    actual = frozenset(names)
    expected = _expected_local_dist_names(include_models=include_models)
    if actual != expected:
        failures.append(
            _failure(
                "local release build artifact inventory does not match models decision",
                expected=", ".join(sorted(expected)),
                actual=", ".join(sorted(actual)) or "<empty>",
                repair="bash scripts/release.sh --candidate",
            )
        )
    linux_core_wheels = [
        name
        for name in actual
        if name.startswith("solstone_core-")
        and ("manylinux2014_x86_64" in name or "manylinux2014_aarch64" in name)
    ]
    if len(linux_core_wheels) != 2:
        failures.append(
            _failure(
                "local release build did not produce both Linux musl core wheels",
                expected="x86_64 and aarch64 musl solstone-core wheels",
                actual=", ".join(sorted(linux_core_wheels)) or "<empty>",
                repair="bash scripts/release.sh --candidate",
            )
        )
    if failures:
        raise DriverError(failures)


def _validate_candidate_wheel_contents(
    release_dir: Path, *, models_decision: str
) -> None:
    wheel_models_decision = "publish" if models_decision == "include" else "skip"
    errors = check_dist(
        release_dir,
        EXPECTED_MODEL_SHA256,
        MAX_BASE_WHEEL_BYTES,
        required_core_platforms=(),
        release_scope="all-hosts",
        models_decision=wheel_models_decision,
    )
    if errors:
        raise DriverError(
            [
                _failure(
                    "release candidate wheel content check failed",
                    expected="candidate wheels matching platform content policy",
                    actual=error,
                    repair=(
                        "rebuild the candidate with bash scripts/release.sh --candidate "
                        "after fixing the reported wheel content"
                    ),
                )
                for error in errors
            ]
        )


def _native_record_role(path: Path) -> str | None:
    if path.name == "macos-native-root.json":
        return "root"
    if path.name == "macos-native-core.json":
        return "core"
    return None


def _native_record_payloads(
    host_result: BuildHostResult,
    *,
    source_commit: str,
    core_lock_sha256: str,
) -> list[dict[str, Any]]:
    failures: list[Failure] = []
    wheel_by_role: dict[str, Path] = {}
    for wheel in host_result.macos_wheels:
        role = _macos_wheel_role(wheel)
        if role is None:
            failures.append(
                _failure(
                    "build-host macOS wheel role is invalid",
                    expected="root or core macOS arm64 wheel",
                    actual=wheel.name,
                    repair="bash scripts/release.sh --candidate",
                )
            )
            continue
        if role in wheel_by_role:
            failures.append(
                _failure(
                    "build-host macOS wheel role is duplicated",
                    expected="one root wheel and one core wheel",
                    actual=role,
                    repair="bash scripts/release.sh --candidate",
                )
            )
        wheel_by_role[role] = wheel
    records: list[dict[str, Any]] = []
    record_roles: set[str] = set()
    for path in host_result.native_records:
        role = _native_record_role(path)
        if role is None:
            failures.append(
                _failure(
                    "build-host native record role is invalid",
                    expected="macos-native-root.json and macos-native-core.json",
                    actual=path.name,
                    repair="bash scripts/release.sh --candidate",
                )
            )
            continue
        if role in record_roles:
            failures.append(
                _failure(
                    "build-host native record role is duplicated",
                    expected="one root record and one core record",
                    actual=role,
                    repair="bash scripts/release.sh --candidate",
                )
            )
        record_roles.add(role)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            failures.append(
                _failure(
                    "native record is not an object",
                    expected="JSON object",
                    actual=type(payload).__name__,
                    repair="bash scripts/release.sh --candidate",
                )
            )
            continue
        wheel = wheel_by_role.get(role)
        if wheel is not None:
            failures.extend(
                validate_macos_native_record(
                    payload,
                    role=role,  # type: ignore[arg-type]
                    wheel_path=wheel,
                    source_commit=source_commit,
                    core_lock_sha256=core_lock_sha256,
                )
            )
        records.append(payload)
    if set(wheel_by_role) != {"root", "core"}:
        failures.append(
            _failure(
                "build-host macOS wheel set is incomplete",
                expected="root and core macOS wheels",
                actual=", ".join(sorted(wheel_by_role)) or "<empty>",
                repair="bash scripts/release.sh --candidate",
            )
        )
    if record_roles != {"root", "core"}:
        failures.append(
            _failure(
                "build-host native record set is incomplete",
                expected="root and core native records",
                actual=", ".join(sorted(record_roles)) or "<empty>",
                repair="bash scripts/release.sh --candidate",
            )
        )
    if failures:
        raise DriverError(failures)
    return records


def _native_records_by_role(
    native_records: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    records: dict[str, Mapping[str, Any]] = {}
    failures: list[Failure] = []
    for record in native_records:
        role = record.get("role")
        if role not in {"root", "core"}:
            failures.append(
                _failure(
                    "native record role is invalid",
                    expected="root or core",
                    actual=str(role),
                    repair="bash scripts/release.sh --candidate",
                )
            )
            continue
        if str(role) in records:
            failures.append(
                _failure(
                    "native record role is duplicated",
                    expected="one root record and one core record",
                    actual=str(role),
                    repair="bash scripts/release.sh --candidate",
                )
            )
        records[str(role)] = record
    if set(records) != {"root", "core"}:
        failures.append(
            _failure(
                "native record set is incomplete",
                expected="root and core native records",
                actual=", ".join(sorted(records)) or "<empty>",
                repair="bash scripts/release.sh --candidate",
            )
        )
    if failures:
        raise DriverError(failures)
    return records


def _revalidate_macos_wheels(
    release_dir: Path,
    native_records: Sequence[Mapping[str, Any]],
    *,
    source_commit: str,
    core_lock_sha256: str,
) -> None:
    failures: list[Failure] = []
    records_by_role = _native_records_by_role(native_records)
    for role, record in sorted(records_by_role.items()):
        wheel = record.get("wheel")
        name = wheel.get("name") if isinstance(wheel, Mapping) else None
        if not isinstance(name, str):
            failures.append(
                _failure(
                    "native record wheel name is invalid",
                    expected=f"{role} native record wheel name",
                    actual=repr(wheel),
                    repair="bash scripts/release.sh --candidate",
                )
            )
            continue
        failures.extend(
            validate_macos_native_record(
                record,
                role=role,  # type: ignore[arg-type]
                wheel_path=release_dir / name,
                source_commit=source_commit,
                core_lock_sha256=core_lock_sha256,
            )
        )
    if failures:
        raise DriverError(failures)


def _validated_full_tool_evidence(
    coordinator_evidence: Mapping[str, Mapping[str, str]],
    host_result: BuildHostResult,
    native_records: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    failures: list[Failure] = []
    if "macos-arm64" in coordinator_evidence:
        failures.append(
            _failure(
                "macOS release tool evidence must be attested by the build host",
                expected="macos-arm64 absent from coordinator tool evidence",
                actual="macos-arm64 present",
                repair="bash scripts/release.sh --candidate",
            )
        )
    expected_coordinator_lanes = set(LANES) - {"macos-arm64"}
    if set(coordinator_evidence) - {"macos-arm64"} != expected_coordinator_lanes:
        failures.append(
            _failure(
                "coordinator release tool evidence lanes are invalid",
                expected=", ".join(sorted(expected_coordinator_lanes)),
                actual=", ".join(sorted(str(key) for key in coordinator_evidence))
                or "<empty>",
                repair="bash scripts/release.sh --candidate",
            )
        )
    combined: dict[str, dict[str, str]] = {}
    for lane, evidence in coordinator_evidence.items():
        if lane == "macos-arm64":
            continue
        if not isinstance(evidence, Mapping):
            failures.append(
                _failure(
                    "coordinator release tool evidence lane is invalid",
                    expected=f"{lane} tool evidence object",
                    actual=type(evidence).__name__,
                    repair="bash scripts/release.sh --candidate",
                )
            )
            continue
        combined[str(lane)] = dict(evidence)
    if not isinstance(host_result.tool_evidence, Mapping):
        failures.append(
            _failure(
                "build-host macOS release tool evidence is unattested",
                expected="host-attested macOS tool evidence object",
                actual=type(host_result.tool_evidence).__name__,
                repair="bash scripts/release.sh --candidate",
            )
        )
    else:
        final_macos, macos_failures = finalize_macos_tool_evidence(
            {str(key): str(value) for key, value in host_result.tool_evidence.items()},
            native_records,
        )
        failures.extend(macos_failures)
        if final_macos is not None:
            combined["macos-arm64"] = final_macos
    if set(combined) != set(LANES):
        failures.append(
            _failure(
                "release tool evidence lanes are incomplete",
                expected=", ".join(LANES),
                actual=", ".join(sorted(combined)) or "<empty>",
                repair="bash scripts/release.sh --candidate",
            )
        )
    for lane in sorted(set(combined) & set(LANE_TOOL_KEYS)):
        failures.extend(check_lane_tool_evidence(lane, combined[lane]))
    failures.extend(validate_public_evidence_tree("release_tool_evidence", combined))
    if failures:
        raise DriverError(failures)
    return combined


def _validate_coordinator_tool_evidence(
    coordinator_evidence: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, str]]:
    failures: list[Failure] = []
    if "macos-arm64" in coordinator_evidence:
        failures.append(
            _failure(
                "macOS release tool evidence must be attested by the build host",
                expected="macos-arm64 absent from coordinator tool evidence",
                actual="macos-arm64 present",
                repair="bash scripts/release.sh --candidate",
            )
        )
    expected_lanes = set(LANES) - {"macos-arm64"}
    actual_lanes = set(coordinator_evidence) - {"macos-arm64"}
    if actual_lanes != expected_lanes:
        failures.append(
            _failure(
                "coordinator release tool evidence lanes are invalid",
                expected=", ".join(sorted(expected_lanes)),
                actual=", ".join(sorted(str(key) for key in actual_lanes)) or "<empty>",
                repair="bash scripts/release.sh --candidate",
            )
        )
    frozen: dict[str, dict[str, str]] = {}
    for lane in sorted(actual_lanes & expected_lanes):
        evidence = coordinator_evidence[lane]
        if not isinstance(evidence, Mapping):
            failures.append(
                _failure(
                    "coordinator release tool evidence lane is invalid",
                    expected=f"{lane} tool evidence object",
                    actual=type(evidence).__name__,
                    repair="bash scripts/release.sh --candidate",
                )
            )
            continue
        frozen[lane] = {str(key): str(value) for key, value in evidence.items()}
        failures.extend(check_lane_tool_evidence(lane, frozen[lane]))
    failures.extend(validate_public_evidence_tree("coordinator_tool_evidence", frozen))
    if failures:
        raise DriverError(failures)
    return frozen


def _manifest_tools_from_full(
    lane: str,
    full_tool_evidence: Mapping[str, Mapping[str, str]],
) -> dict[str, str]:
    allowed = NATIVE_TOOL_KEYS[lane]  # type: ignore[index]
    tools = full_tool_evidence.get(lane, {})
    return {key: tools[key] for key in sorted(allowed)}


def _rustc_verbose_from_full_tool_evidence(
    lane: str, full_tool_evidence: Mapping[str, Mapping[str, str]]
) -> str:
    tools = full_tool_evidence[lane]
    host = {
        "source": "x86_64-unknown-linux-gnu",
        "linux-x86_64-musl": "x86_64-unknown-linux-gnu",
        "linux-aarch64-musl": "x86_64-unknown-linux-gnu",
        "macos-arm64": "aarch64-apple-darwin",
    }[lane]
    return "\n".join(
        [
            tools["rustc"],
            f"binary: {RUSTC_BINARY_PIN}",
            f"commit-hash: {RUSTC_COMMIT_HASH_PIN}",
            f"commit-date: {RUSTC_COMMIT_DATE_PIN}",
            f"host: {host}",
            f"release: {RUSTC_RELEASE_PIN}",
            f"LLVM version: {RUSTC_LLVM_PIN}",
        ]
    )


def _lane_evidence_from_full_tool_evidence(
    full_tool_evidence: Mapping[str, Mapping[str, str]],
    *,
    policy_run: PolicyRun,
) -> dict[str, LaneEvidence]:
    failures: list[Failure] = []
    if set(full_tool_evidence) != set(LANES):
        failures.append(
            _failure(
                "release tool evidence lanes are incomplete",
                expected=", ".join(LANES),
                actual=", ".join(sorted(str(key) for key in full_tool_evidence))
                or "<empty>",
                repair="bash scripts/release.sh --candidate",
            )
        )
    evidence_by_lane: dict[str, LaneEvidence] = {}
    for lane in sorted(set(full_tool_evidence) & set(LANES)):
        tools = full_tool_evidence[lane]
        try:
            native_tools = _manifest_tools_from_full(lane, full_tool_evidence)
            evidence_by_lane[lane] = LaneEvidence(
                rustc_verbose=_rustc_verbose_from_full_tool_evidence(
                    lane, full_tool_evidence
                ),
                cargo_version=tools["cargo"],
                native_tools=native_tools,
                cargo_deny_version=tools["cargo-deny"],
                advisory_checked_at=policy_run.policy_checked_at,
            )
        except KeyError as exc:
            failures.append(
                _failure(
                    "lane evidence cannot be derived from frozen tool observation",
                    expected=f"{lane} full release tool evidence contains manifest keys",
                    actual=str(exc),
                    repair="bash scripts/release.sh --candidate",
                )
            )
    if failures:
        raise DriverError(failures)
    return evidence_by_lane


def _copy_macos_wheels(host_result: BuildHostResult, dist_dir: Path) -> None:
    failures: list[Failure] = []
    if len(host_result.macos_wheels) != 2:
        failures.append(
            _failure(
                "build-host macOS wheel set has wrong size",
                expected="exactly two macOS wheels",
                actual=str(len(host_result.macos_wheels)),
                repair="bash scripts/release.sh --candidate",
            )
        )
    for wheel in host_result.macos_wheels:
        if not wheel.is_file() or wheel.is_symlink():
            failures.append(
                _failure(
                    "build-host macOS wheel is not a regular file",
                    expected="regular macOS wheel",
                    actual=wheel.name,
                    repair="bash scripts/release.sh --candidate",
                )
            )
            continue
        shutil.copy2(wheel, dist_dir / wheel.name)
    if failures:
        raise DriverError(failures)


def _expected_members(
    ledger: Mapping[str, Any], target: str
) -> tuple[Mapping[str, Mapping[str, Any]], list[Failure]]:
    native = ledger.get("native_members", {})
    if not isinstance(native, Mapping):
        return {}, [
            _failure(
                "retained ledger native members are invalid",
                expected="native_members object",
                actual=type(native).__name__,
                repair="bash scripts/release.sh --recover",
            )
        ]
    target_members = native.get(target)
    if not isinstance(target_members, Mapping):
        return {}, [
            _failure(
                "retained ledger target native members are invalid",
                expected=f"{target} native member object",
                actual=type(target_members).__name__,
                repair="bash scripts/release.sh --recover",
            )
        ]
    failures: list[Failure] = []
    members: dict[str, Mapping[str, Any]] = {}
    for member_name, member in target_members.items():
        if not isinstance(member, Mapping):
            failures.append(
                _failure(
                    "retained ledger native member is invalid",
                    expected="native member object",
                    actual=repr(member_name),
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        members[str(member_name)] = member
    return members, failures


def _validate_proof_binding(
    proof: Mapping[str, Any],
    *,
    target: str,
    ledger: Mapping[str, Any],
    digest: str,
    ledger_sha256: str,
    release_dir: Path,
) -> list[Failure]:
    failures: list[Failure] = []
    expected_scalars = {
        "target": target,
        "source_commit": ledger.get("source_commit"),
        "candidate_digest": digest,
        "ledger_sha256": ledger_sha256,
        "core_lock_sha256": ledger.get("core_lock_sha256"),
    }
    for key, expected in expected_scalars.items():
        if proof.get(key) != expected:
            failures.append(
                _failure(
                    f"install proof {key} is not bound to retained candidate",
                    expected=str(expected),
                    actual=str(proof.get(key)),
                    repair="bash scripts/release.sh --recover",
                )
            )
    proof_entries: set[tuple[str, int, str]] = set()
    for entry in proof.get("candidate_files", []):
        if not isinstance(entry, Mapping):
            continue
        byte_count = entry.get("bytes", -1)
        if type(byte_count) is not int or byte_count < 0:
            failures.append(
                _failure(
                    "install proof candidate file byte count is invalid",
                    expected="non-negative integer",
                    actual=repr(byte_count),
                    repair=RETAINED_PROOF_REPAIR,
                )
            )
            continue
        proof_entries.add(
            (
                str(entry.get("basename")),
                byte_count,
                str(entry.get("sha256")),
            )
        )
    try:
        expected_install_paths = target_install_paths_from_ledger(
            ledger,
            target=target,
            candidate_dir=release_dir,
        )
        expected_entries = {
            (
                str(entry["basename"]),
                int(entry["bytes"]),
                str(entry["sha256"]),
            )
            for entry in candidate_file_entries(expected_install_paths)
        }
    except InstallProofError as exc:
        failures.extend(exc.failures)
        expected_entries = set()
    if proof_entries != expected_entries:
        failures.append(
            _failure(
                "install proof candidate inventory does not match target install set",
                expected="retained ledger target install files",
                actual="proof candidate files differ",
                repair="bash scripts/release.sh --recover",
            )
        )
    expected_members, expected_member_failures = _expected_members(ledger, target)
    failures.extend(expected_member_failures)
    installed_members = proof.get("installed_members", [])
    if not isinstance(installed_members, list):
        installed_members = []
    seen_members: set[str] = set()
    for member in installed_members:
        if not isinstance(member, Mapping):
            continue
        name = str(member.get("name"))
        seen_members.add(name)
        if "expected_sha256" in member:
            failures.append(
                _failure(
                    "install proof installed member carries forbidden expected hash",
                    expected="expected hashes retained only in ledger",
                    actual=name,
                    repair="bash scripts/release.sh --recover",
                )
            )
        expected = expected_members.get(name)
        if expected is None:
            failures.append(
                _failure(
                    "install proof installed member is not retained in ledger",
                    expected=f"{target} retained native member",
                    actual=name,
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        if member.get("wheel_member_path") != expected.get("path"):
            failures.append(
                _failure(
                    "install proof wheel member path does not match ledger",
                    expected=str(expected.get("path")),
                    actual=str(member.get("wheel_member_path")),
                    repair="bash scripts/release.sh --recover",
                )
            )
        installed_path = member.get("installed_path")
        if not isinstance(installed_path, str) or not installed_path.startswith(
            f"{ENVROOT}/"
        ):
            failures.append(
                _failure(
                    "install proof installed path is invalid",
                    expected=f"normalized installed executable path beneath {ENVROOT}",
                    actual=repr(installed_path),
                    repair="bash scripts/release.sh --recover",
                )
            )
        if installed_path == member.get("wheel_member_path"):
            failures.append(
                _failure(
                    "install proof member paths are conflated",
                    expected="distinct wheel_member_path and installed_path",
                    actual=repr(installed_path),
                    repair="bash scripts/release.sh --recover",
                )
            )
        if member.get("sha256") != expected.get("sha256"):
            failures.append(
                _failure(
                    "install proof installed member hash does not match ledger",
                    expected=str(expected.get("sha256")),
                    actual=str(member.get("sha256")),
                    repair="bash scripts/release.sh --recover",
                )
            )
    expected_names = set(expected_members)
    if seen_members != expected_names:
        failures.append(
            _failure(
                "install proof installed member set does not match ledger",
                expected=", ".join(sorted(expected_names)) or "<empty>",
                actual=", ".join(sorted(seen_members)) or "<empty>",
                repair="bash scripts/release.sh --recover",
            )
        )
    return failures


def _validate_models_binding(
    root: Path,
    release_dir: Path,
    ledger: Mapping[str, Any],
    *,
    check_local_version: bool = True,
) -> list[Failure]:
    models = ledger.get("models")
    if not isinstance(models, Mapping):
        return [
            _failure(
                "retained ledger models binding is invalid",
                expected="models decision object",
                actual=type(models).__name__,
                repair="bash scripts/release.sh --recover",
            )
        ]
    decision = models.get("decision")
    package_version = models.get("package_version")
    if decision not in {"include", "exclude"}:
        return [
            _failure(
                "retained ledger models decision is invalid",
                expected="include or exclude",
                actual=str(decision),
                repair="bash scripts/release.sh --recover",
            )
        ]
    failures: list[Failure] = []
    if check_local_version:
        actual_version = _local_models_package_version(root)
        if package_version != actual_version:
            failures.append(
                _failure(
                    "retained ledger models package version changed",
                    expected=str(package_version),
                    actual=actual_version,
                    repair="bash scripts/release.sh --recover",
                )
            )
    expected = set(expected_package_names(include_models=decision == "include"))
    actual = {path.name for path in release_dir.iterdir() if path.is_file()}
    manifests = {
        name for name in actual if name.endswith(".rust-release-manifest.json")
    }
    package_names = actual - manifests
    if package_names != expected:
        failures.append(
            _failure(
                "retained candidate package inventory does not match models decision",
                expected=", ".join(sorted(expected)),
                actual=", ".join(sorted(package_names)) or "<empty>",
                repair="bash scripts/release.sh --recover",
            )
        )
    return failures


def _expected_payload_file_names(*, include_models: bool) -> frozenset[str]:
    packages = set(expected_package_names(include_models=include_models))
    manifests = {
        f"{name}.rust-release-manifest.json" for name in rust_artifact_targets()
    }
    return frozenset(packages | manifests)


def _safe_retained_basename(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and Path(value).name == value
        and "/" not in value
        and "\\" not in value
    )


def _ledger_candidate_file_names(
    ledger: Mapping[str, Any],
) -> tuple[frozenset[str], list[Failure]]:
    candidate = ledger.get("candidate")
    if not isinstance(candidate, Mapping):
        return frozenset(), [
            _failure(
                "retained ledger candidate inventory is invalid",
                expected="candidate object",
                actual=type(candidate).__name__,
                repair="bash scripts/release.sh --recover",
            )
        ]
    files = candidate.get("files")
    if not isinstance(files, list):
        return frozenset(), [
            _failure(
                "retained ledger candidate file list is invalid",
                expected="candidate.files list",
                actual=type(files).__name__,
                repair="bash scripts/release.sh --recover",
            )
        ]
    names: list[str] = []
    failures: list[Failure] = []
    for index, item in enumerate(files):
        name = item.get("name") if isinstance(item, Mapping) else None
        if not _safe_retained_basename(name):
            failures.append(
                _failure(
                    "retained ledger candidate filename is invalid",
                    expected=f"candidate.files[{index}].name safe basename",
                    actual=repr(name),
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        names.append(str(name))
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        failures.append(
            _failure(
                "retained ledger candidate file list has duplicate names",
                expected="unique retained candidate file basenames",
                actual=", ".join(duplicates),
                repair="bash scripts/release.sh --recover",
            )
        )
    return frozenset(names), failures


def _validate_flat_payload_inventory(
    release_dir: Path,
    *,
    include_models: bool | None = None,
    expected_names: frozenset[str] | None = None,
) -> list[Failure]:
    failures: list[Failure] = []
    try:
        root_entry = release_dir.lstat()
    except FileNotFoundError:
        return [
            _failure(
                "release payload directory is missing",
                expected="final release payload directory",
                actual="missing",
                repair="bash scripts/release.sh --recover",
            )
        ]
    if stat.S_ISLNK(root_entry.st_mode) or not stat.S_ISDIR(root_entry.st_mode):
        return [
            _failure(
                "release payload is not an owned directory",
                expected="non-symlink release payload directory",
                actual=release_dir.name,
                repair="bash scripts/release.sh --recover",
            )
        ]
    names: set[str] = set()
    for path in release_dir.iterdir():
        entry = path.lstat()
        if not stat.S_ISREG(entry.st_mode):
            failures.append(
                _failure(
                    "release payload inventory contains unsafe entry",
                    expected="flat regular payload files only",
                    actual=path.name,
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        names.add(path.name)
    if expected_names is None:
        if include_models is None:
            raise AssertionError("include_models is required without expected_names")
        expected = _expected_payload_file_names(include_models=include_models)
    else:
        expected = expected_names
    if names != expected:
        failures.append(
            _failure(
                "release payload inventory is not exact",
                expected=", ".join(sorted(expected)),
                actual=", ".join(sorted(names)) or "<empty>",
                repair="bash scripts/release.sh --recover",
            )
        )
    return failures


def _retained_rust_targets_by_artifact(
    ledger: Mapping[str, Any],
) -> tuple[dict[str, tuple[str, dict[str, Any]]], list[Failure]]:
    raw_targets = ledger.get("rust_targets")
    if not isinstance(raw_targets, list):
        return {}, [
            _failure(
                "retained ledger Rust targets are invalid",
                expected="rust_targets list",
                actual=type(raw_targets).__name__,
                repair="bash scripts/release.sh --recover",
            )
        ]
    targets: dict[str, tuple[str, dict[str, Any]]] = {}
    failures: list[Failure] = []
    for index, item in enumerate(raw_targets):
        if not isinstance(item, Mapping):
            failures.append(
                _failure(
                    "retained ledger Rust target entry is invalid",
                    expected=f"rust_targets[{index}] object",
                    actual=type(item).__name__,
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        artifact = item.get("artifact")
        lane = item.get("lane")
        if not _safe_retained_basename(artifact) or lane not in LANES:
            failures.append(
                _failure(
                    "retained ledger Rust target entry is invalid",
                    expected=f"rust_targets[{index}] artifact basename and lane",
                    actual=repr(item),
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        target = {
            str(key): value
            for key, value in item.items()
            if key not in {"artifact", "lane"}
        }
        if str(artifact) in targets:
            failures.append(
                _failure(
                    "retained ledger Rust targets contain duplicate artifacts",
                    expected="one Rust target per retained artifact",
                    actual=str(artifact),
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        targets[str(artifact)] = (str(lane), target)
    return targets, failures


def _manifest_artifact_payload(
    manifest_name: str, payload: Mapping[str, Any]
) -> tuple[Mapping[str, Any] | None, str | None, list[Failure]]:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        return (
            None,
            None,
            [
                _failure(
                    "retained manifest must contain exactly one artifact",
                    expected=f"{manifest_name} one artifact entry",
                    actual=repr(artifacts),
                    repair="bash scripts/release.sh --recover",
                )
            ],
        )
    artifact = artifacts[0]
    if not isinstance(artifact, Mapping):
        return (
            None,
            None,
            [
                _failure(
                    "retained manifest artifact entry is invalid",
                    expected=f"{manifest_name} artifact object",
                    actual=type(artifact).__name__,
                    repair="bash scripts/release.sh --recover",
                )
            ],
        )
    artifact_name = artifact.get("path")
    if not _safe_retained_basename(artifact_name):
        return (
            artifact,
            None,
            [
                _failure(
                    "retained manifest artifact path is invalid",
                    expected=f"{manifest_name} artifact basename",
                    actual=repr(artifact_name),
                    repair="bash scripts/release.sh --recover",
                )
            ],
        )
    return artifact, str(artifact_name), []


def _expected_manifest_native_tools(
    ledger: Mapping[str, Any], lane: str
) -> Mapping[str, str] | None:
    tool_evidence = ledger.get("tool_evidence")
    if not isinstance(tool_evidence, Mapping):
        return None
    lane_tools = tool_evidence.get(lane)
    if not isinstance(lane_tools, Mapping) or lane not in NATIVE_TOOL_KEYS:
        return None
    return {
        key: str(lane_tools[key])
        for key in NATIVE_TOOL_KEYS[lane]
        if isinstance(lane_tools.get(key), str)
    }


def _validate_retained_manifest_files(
    release_dir: Path,
    ledger: Mapping[str, Any],
) -> list[Failure]:
    expected_names, failures = _ledger_candidate_file_names(ledger)
    manifest_names = sorted(
        name for name in expected_names if name.endswith(".rust-release-manifest.json")
    )
    if len(manifest_names) != 4:
        failures.append(
            _failure(
                "retained payload manifest count is invalid",
                expected="four retained Rust companion manifests",
                actual=str(len(manifest_names)),
                repair="bash scripts/release.sh --recover",
            )
        )
    rust_targets, target_failures = _retained_rust_targets_by_artifact(ledger)
    failures.extend(target_failures)
    dependency_policy = ledger.get("dependency_policy")
    expected_scalars = {
        "product": "solstone-core",
        "version": ledger.get("version"),
        "source_commit": ledger.get("source_commit"),
        "source_dirty": False,
        "cargo_lock_sha256": ledger.get("core_lock_sha256"),
        "dependency_policy": dependency_policy,
        "active_exceptions": [],
    }
    for manifest_name in manifest_names:
        manifest_path = release_dir / manifest_name
        try:
            entry = manifest_path.lstat()
        except OSError as exc:
            failures.append(
                _failure(
                    "retained manifest could not be inspected",
                    expected=f"{manifest_name} regular file",
                    actual=type(exc).__name__,
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        if not stat.S_ISREG(entry.st_mode):
            failures.append(
                _failure(
                    "retained manifest is not a regular file",
                    expected=f"{manifest_name} regular file",
                    actual="non-regular",
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            failures.append(
                _failure(
                    "retained manifest is not readable JSON",
                    expected=f"{manifest_name} JSON object",
                    actual=type(exc).__name__,
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        if not isinstance(payload, Mapping):
            failures.append(
                _failure(
                    "retained manifest is not a JSON object",
                    expected=f"{manifest_name} object",
                    actual=type(payload).__name__,
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        for key, expected in expected_scalars.items():
            if payload.get(key) != expected:
                failures.append(
                    _failure(
                        f"retained manifest {key} is not bound to ledger",
                        expected=repr(expected),
                        actual=repr(payload.get(key)),
                        repair="bash scripts/release.sh --recover",
                    )
                )
        artifact, artifact_name, artifact_failures = _manifest_artifact_payload(
            manifest_name, payload
        )
        failures.extend(artifact_failures)
        if artifact is None or artifact_name is None:
            continue
        if manifest_name != f"{artifact_name}.rust-release-manifest.json":
            failures.append(
                _failure(
                    "retained manifest filename is not the artifact companion name",
                    expected=f"{artifact_name}.rust-release-manifest.json",
                    actual=manifest_name,
                    repair="bash scripts/release.sh --recover",
                )
            )
        if artifact_name not in expected_names:
            failures.append(
                _failure(
                    "retained manifest artifact is not in retained candidate inventory",
                    expected="artifact named by ledger candidate files",
                    actual=artifact_name,
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        try:
            artifact_sha256, artifact_bytes = file_sha256_size(
                release_dir / artifact_name
            )
        except OSError as exc:
            failures.append(
                _failure(
                    "retained manifest artifact could not be read",
                    expected=artifact_name,
                    actual=type(exc).__name__,
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        if (
            artifact.get("sha256") != artifact_sha256
            or artifact.get("bytes") != artifact_bytes
        ):
            failures.append(
                _failure(
                    "retained manifest artifact digest does not match final bytes",
                    expected=f"{artifact_sha256}/{artifact_bytes}",
                    actual=repr(artifact),
                    repair="bash scripts/release.sh --recover",
                )
            )
        retained_target = rust_targets.get(artifact_name)
        if retained_target is None:
            failures.append(
                _failure(
                    "retained manifest artifact has no retained Rust target",
                    expected=artifact_name,
                    actual="missing",
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        lane, target = retained_target
        if payload.get("target") != target:
            failures.append(
                _failure(
                    "retained manifest target does not match ledger Rust target",
                    expected=repr(target),
                    actual=repr(payload.get("target")),
                    repair="bash scripts/release.sh --recover",
                )
            )
        expected_native_tools = _expected_manifest_native_tools(ledger, lane)
        if payload.get("native_tools") != expected_native_tools:
            failures.append(
                _failure(
                    "retained manifest native tools do not match ledger tool evidence",
                    expected=repr(expected_native_tools),
                    actual=repr(payload.get("native_tools")),
                    repair="bash scripts/release.sh --recover",
                )
            )
    return failures


def _validate_evidence_inventory(evidence_dir: Path) -> list[Failure]:
    failures: list[Failure] = []
    try:
        evidence_entry = evidence_dir.lstat()
    except FileNotFoundError:
        return [
            _failure(
                "release evidence directory is missing",
                expected="ledger.json and proofs directory",
                actual="missing",
                repair="bash scripts/release.sh --recover",
            )
        ]
    if stat.S_ISLNK(evidence_entry.st_mode) or not stat.S_ISDIR(evidence_entry.st_mode):
        return [
            _failure(
                "release evidence is not an owned directory",
                expected="non-symlink evidence directory",
                actual=evidence_dir.name,
                repair="bash scripts/release.sh --recover",
            )
        ]
    entries = {path.name: path for path in evidence_dir.iterdir()}
    if set(entries) != {"ledger.json", "proofs"}:
        failures.append(
            _failure(
                "release evidence inventory is not exact",
                expected="ledger.json, proofs",
                actual=", ".join(sorted(entries)) or "<empty>",
                repair="bash scripts/release.sh --recover",
            )
        )
    ledger_path = entries.get("ledger.json")
    if ledger_path is not None:
        ledger_entry = ledger_path.lstat()
        if not stat.S_ISREG(ledger_entry.st_mode):
            failures.append(
                _failure(
                    "release ledger is not a regular file",
                    expected="regular ledger.json",
                    actual="non-regular",
                    repair="bash scripts/release.sh --recover",
                )
            )
    proofs_dir = entries.get("proofs")
    if proofs_dir is None:
        return failures
    proofs_entry = proofs_dir.lstat()
    if stat.S_ISLNK(proofs_entry.st_mode) or not stat.S_ISDIR(proofs_entry.st_mode):
        failures.append(
            _failure(
                "release proofs entry is not an owned directory",
                expected="non-symlink proofs directory",
                actual="non-directory",
                repair="bash scripts/release.sh --recover",
            )
        )
        return failures
    proof_entries = {path.name: path for path in proofs_dir.iterdir()}
    expected_proofs = {f"{target}.json" for target in PROOF_TARGETS}
    if set(proof_entries) != expected_proofs:
        failures.append(
            _failure(
                "release proof inventory is not exact",
                expected=", ".join(sorted(expected_proofs)),
                actual=", ".join(sorted(proof_entries)) or "<empty>",
                repair="bash scripts/release.sh --recover",
            )
        )
    for proof_name, proof_path in sorted(proof_entries.items()):
        proof_entry = proof_path.lstat()
        if not stat.S_ISREG(proof_entry.st_mode):
            failures.append(
                _failure(
                    "release proof is not a regular file",
                    expected=f"regular proof file {proof_name}",
                    actual="non-regular",
                    repair="bash scripts/release.sh --recover",
                )
            )
    return failures


def _candidate_file_entries_from_dir(release_dir: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(release_dir.iterdir(), key=lambda item: item.name):
        entry = path.lstat()
        if not stat.S_ISREG(entry.st_mode):
            continue
        sha256, byte_count = file_sha256_size(path)
        files.append({"name": path.name, "sha256": sha256, "bytes": byte_count})
    return files


def _validate_policy_payload(policy_run: Mapping[str, Any]) -> list[Failure]:
    failures: list[Failure] = []
    if (
        not isinstance(policy_run.get("advisory_source_id"), str)
        or not policy_run["advisory_source_id"]
    ):
        failures.append(
            _failure(
                "retained ledger advisory source ID is invalid",
                expected="non-empty public advisory source ID",
                actual=repr(policy_run.get("advisory_source_id")),
                repair="bash scripts/release.sh --recover",
            )
        )
    failures.extend(
        validate_snapshot_identity(
            "retained ledger policy_run",
            db_commit=policy_run.get("db_commit"),
            db_archive_sha256=policy_run.get("db_archive_sha256"),
        )
    )
    snapshot = policy_run.get("db_snapshot_basename")
    if not _safe_retained_basename(snapshot):
        failures.append(
            _failure(
                "retained ledger db snapshot basename is invalid",
                expected="safe snapshot directory basename",
                actual=repr(snapshot),
                repair="bash scripts/release.sh --recover",
            )
        )
    advisory_count = policy_run.get("advisory_count")
    if type(advisory_count) is not int or advisory_count <= 0:
        failures.append(
            _failure(
                "retained ledger advisory count is invalid",
                expected="positive integer advisory count",
                actual=repr(advisory_count),
                repair="bash scripts/release.sh --recover",
            )
        )
    for key in (
        "advisory_acquired_at",
        "db_commit_timestamp",
        "policy_checked_at",
    ):
        value = policy_run.get(key)
        if not is_normalized_utc_timestamp(value):
            failures.append(
                _failure(
                    f"retained ledger {key} is invalid",
                    expected="RFC3339 UTC timestamp normalized with Z",
                    actual=repr(value),
                    repair="bash scripts/release.sh --recover",
                )
            )
    if policy_run.get("result") != "pass":
        failures.append(
            _failure(
                "retained ledger policy result is invalid",
                expected="pass",
                actual=str(policy_run.get("result")),
                repair="bash scripts/release.sh --recover",
            )
        )
    return failures


def _validate_native_summary(
    release_dir: Path, ledger: Mapping[str, Any]
) -> list[Failure]:
    failures: list[Failure] = []
    summary = ledger.get("native_summary")
    members = ledger.get("native_members")
    if not isinstance(summary, Mapping) or not isinstance(members, Mapping):
        return [
            _failure(
                "retained ledger native summary is invalid",
                expected="native_summary and native_members objects",
                actual="missing or malformed",
                repair="bash scripts/release.sh --recover",
            )
        ]
    macos_members = members.get("macos-arm64")
    if not isinstance(macos_members, Mapping):
        return [
            _failure(
                "retained ledger macOS native members are invalid",
                expected="macos-arm64 native member map",
                actual=type(macos_members).__name__,
                repair="bash scripts/release.sh --recover",
            )
        ]
    summary_member_expectations = {
        "macos_root_helper": "parakeet-helper",
        "macos_core_script": "solstone-core",
    }
    for summary_key, member_key in summary_member_expectations.items():
        item = summary.get(summary_key)
        expected_member = macos_members.get(member_key)
        if not isinstance(item, Mapping) or not isinstance(expected_member, Mapping):
            failures.append(
                _failure(
                    "retained ledger native summary member is invalid",
                    expected=f"{summary_key} summary and {member_key} member",
                    actual=summary_key,
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        if item.get("member") != expected_member:
            failures.append(
                _failure(
                    "retained ledger native summary disagrees with native member map",
                    expected=repr(expected_member),
                    actual=repr(item.get("member")),
                    repair="bash scripts/release.sh --recover",
                )
            )
        wheel = item.get("wheel")
        if not isinstance(wheel, Mapping) or not isinstance(wheel.get("name"), str):
            failures.append(
                _failure(
                    "retained ledger native summary wheel is invalid",
                    expected=f"{summary_key} wheel name",
                    actual=repr(wheel),
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        try:
            expected_sha256, expected_bytes = file_sha256_size(
                release_dir / wheel["name"]
            )
        except OSError as exc:
            failures.append(
                _failure(
                    "retained ledger native summary wheel could not be read",
                    expected="final wheel named by native summary",
                    actual=type(exc).__name__,
                    repair="bash scripts/release.sh --recover",
                )
            )
            continue
        if (
            wheel.get("sha256") != expected_sha256
            or wheel.get("bytes") != expected_bytes
        ):
            failures.append(
                _failure(
                    "retained ledger native summary wheel disagrees with final bytes",
                    expected=f"{expected_sha256}/{expected_bytes}",
                    actual=repr(wheel),
                    repair="bash scripts/release.sh --recover",
                )
            )
    return failures


def _validate_deep_ledger_binding(
    *,
    root: Path,
    version: str,
    source_commit: str,
    expected_lock_sha256: str,
    release_dir: Path,
    evidence_dir: Path,
    ledger_path: Path,
    ledger: Mapping[str, Any],
    policy_run: PolicyRun | None = None,
    check_local_models_version: bool = True,
    validate_current_release_metadata: bool = True,
) -> list[Failure]:
    failures: list[Failure] = []
    try:
        if ledger_path.read_bytes() != canonical_json_bytes(ledger):
            failures.append(
                _failure(
                    "retained ledger bytes are not canonical JSON",
                    expected="canonical_json_bytes(ledger)",
                    actual="ledger bytes differ",
                    repair="bash scripts/release.sh --recover",
                )
            )
    except OSError as exc:
        failures.append(
            _failure(
                "retained ledger could not be read",
                expected="readable ledger.json",
                actual=type(exc).__name__,
                repair="bash scripts/release.sh --recover",
            )
        )
    expected_scalars = {
        "kind": "solstone-release-ledger",
        "product": "solstone",
        "version": version,
        "source_commit": source_commit,
        "core_lock_sha256": expected_lock_sha256,
    }
    for key, expected in expected_scalars.items():
        if ledger.get(key) != expected:
            failures.append(
                _failure(
                    f"retained ledger {key} is not bound to candidate",
                    expected=str(expected),
                    actual=str(ledger.get(key)),
                    repair="bash scripts/release.sh --recover",
                )
            )
    candidate_files = _candidate_file_entries_from_dir(release_dir)
    candidate = ledger.get("candidate")
    if not isinstance(candidate, Mapping):
        failures.append(
            _failure(
                "retained ledger candidate binding is invalid",
                expected="candidate object",
                actual=type(candidate).__name__,
                repair="bash scripts/release.sh --recover",
            )
        )
    else:
        package_file_count = sum(
            1
            for item in candidate_files
            if not item["name"].endswith(".rust-release-manifest.json")
        )
        manifest_file_count = len(candidate_files) - package_file_count
        expected_candidate = {
            "path": CANDIDATE,
            "file_count": len(candidate_files),
            "package_file_count": package_file_count,
            "manifest_file_count": manifest_file_count,
            "candidate_digest": candidate_digest(release_dir),
            "files": candidate_files,
        }
        if candidate != expected_candidate:
            failures.append(
                _failure(
                    "retained ledger candidate inventory disagrees with final payload",
                    expected="candidate names, counts, bytes, hashes, digest",
                    actual="candidate object differs",
                    repair="bash scripts/release.sh --recover",
                )
            )
    if validate_current_release_metadata:
        expected_targets = [
            {"lane": lane, "artifact": artifact, **target}
            for artifact, (lane, target) in sorted(rust_artifact_targets().items())
        ]
        if ledger.get("rust_targets") != expected_targets:
            failures.append(
                _failure(
                    "retained ledger Rust targets are invalid",
                    expected=repr(expected_targets),
                    actual=repr(ledger.get("rust_targets")),
                    repair="bash scripts/release.sh --recover",
                )
            )
    policy_payload = ledger.get("policy_run")
    if isinstance(policy_payload, Mapping):
        failures.extend(_validate_policy_payload(policy_payload))
        if policy_run is not None and policy_payload != {
            "advisory_source_id": policy_run.advisory_source_id,
            "db_snapshot_basename": policy_run.db_snapshot_basename,
            "db_commit": policy_run.db_commit,
            "db_archive_sha256": policy_run.db_archive_sha256,
            "advisory_count": policy_run.advisory_count,
            "advisory_acquired_at": policy_run.advisory_acquired_at,
            "db_commit_timestamp": policy_run.db_commit_timestamp,
            "policy_checked_at": policy_run.policy_checked_at,
            "result": policy_run.result,
        }:
            failures.append(
                _failure(
                    "retained ledger policy run disagrees with finalized policy cohort",
                    expected="policy run used for candidate finalization",
                    actual="policy_run differs",
                    repair="bash scripts/release.sh --recover",
                )
            )
    if ledger.get("proofs") != {"expected_targets": list(PROOF_TARGETS)}:
        failures.append(
            _failure(
                "retained ledger expected proof IDs are invalid",
                expected=", ".join(PROOF_TARGETS),
                actual=repr(ledger.get("proofs")),
                repair="bash scripts/release.sh --recover",
            )
        )
    if ledger.get("redaction") != {"validator": "recursive-key-value-public-evidence"}:
        failures.append(
            _failure(
                "retained ledger redaction marker is invalid",
                expected="recursive-key-value-public-evidence",
                actual=repr(ledger.get("redaction")),
                repair="bash scripts/release.sh --recover",
            )
        )
    if validate_current_release_metadata:
        model_failures = _validate_models_binding(
            root,
            release_dir,
            ledger,
            check_local_version=check_local_models_version,
        )
        failures.extend(model_failures)
    native_member_failures = validate_native_members_against_release_dir(
        release_dir, ledger
    )
    failures.extend(native_member_failures)
    failures.extend(_validate_native_summary(release_dir, ledger))
    failures.extend(validate_public_evidence_tree("ledger", ledger))
    _ = evidence_dir
    return failures


def _cleanup_owned_cohorts(paths: Sequence[Path]) -> list[Failure]:
    failures: list[Failure] = []
    for path in paths:
        failures.extend(_remove_owned_path(path, label=path.name))
    return failures


def _aggregate_finalization_error(
    error: BaseException, cleanup_failures: Sequence[Failure]
) -> DriverError:
    if isinstance(error, DriverError):
        failures = [*error.failures, *cleanup_failures]
    else:
        failures = [
            _failure(
                "release candidate finalization transaction failed",
                expected="payload and evidence pair-promoted",
                actual=type(error).__name__,
                repair="bash scripts/release.sh --candidate",
            ),
            *cleanup_failures,
        ]
    return DriverError(failures)


def _pair_promote_payload_and_evidence(
    *,
    payload_staging: Path,
    ready_path: Path,
    evidence_staging: Path,
    evidence_dir: Path,
    include_models: bool,
    hook: Callable[[str], None],
) -> None:
    promoted_payload = False
    promoted_evidence = False
    try:
        failures = [
            *_validate_flat_payload_inventory(
                payload_staging, include_models=include_models
            ),
            *_validate_evidence_inventory(evidence_staging),
        ]
        for final_path, label in (
            (ready_path, "final payload"),
            (evidence_dir, "final evidence"),
        ):
            if not _is_missing(final_path):
                failures.append(
                    _failure(
                        "release finalization target already exists",
                        expected=f"absent {label} path",
                        actual=final_path.name,
                        repair="bash scripts/release.sh --candidate",
                    )
                )
        if failures:
            raise DriverError(failures)
        os.rename(payload_staging, ready_path)
        promoted_payload = True
        hook("after-payload-rename")
        hook("between-renames")
        os.rename(evidence_staging, evidence_dir)
        promoted_evidence = True
        hook("after-evidence-rename")
    except BaseException as exc:
        cleanup_targets = [
            ready_path if promoted_payload else payload_staging,
            evidence_dir if promoted_evidence else evidence_staging,
        ]
        cleanup_failures = _cleanup_owned_cohorts(cleanup_targets)
        raise _aggregate_finalization_error(exc, cleanup_failures) from None


def _proof_hashes(
    proofs_dir: Path,
    *,
    ledger: Mapping[str, Any],
    digest: str,
    ledger_sha256: str,
    release_dir: Path,
    version: str,
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for target in PROOF_TARGETS:
        path = proofs_dir / f"{target}.json"
        if not path.is_file() or path.is_symlink():
            raise DriverError(
                [
                    _failure(
                        "release proof is missing",
                        expected=f"{target} proof",
                        actual="missing",
                        repair="bash scripts/release.sh --recover",
                    )
                ]
            )
        data = path.read_bytes()
        failures = validate_install_proof_bytes(
            data,
            target=target,
            version=version,
            source_commit=str(ledger.get("source_commit")),
            core_lock_sha256=str(ledger.get("core_lock_sha256")),
            candidate_digest=digest,
            ledger_sha256=ledger_sha256,
            candidate_dir=release_dir,
            ledger_payload=ledger,
        )
        if failures:
            raise DriverError(failures)
        proof = json.loads(data.decode("utf-8"))
        binding_failures = _validate_proof_binding(
            proof,
            target=target,
            ledger=ledger,
            digest=digest,
            ledger_sha256=ledger_sha256,
            release_dir=release_dir,
        )
        if binding_failures:
            raise DriverError(binding_failures)
        hashes[target] = file_sha256_size(path)[0]
    extras = sorted(
        path.name
        for path in proofs_dir.glob("*.json")
        if path.stem not in PROOF_TARGETS
    )
    if extras:
        raise DriverError(
            [
                _failure(
                    "release proof set has extra targets",
                    expected=", ".join(PROOF_TARGETS),
                    actual=", ".join(extras),
                    repair="bash scripts/release.sh --recover",
                )
            ]
        )
    return hashes


def _report(
    *,
    heading: str,
    root: Path,
    version: str,
    source_commit: str,
    expected_lock_sha256: str,
    release_dir: Path,
    evidence_dir: Path,
    policy_run: PolicyRun | None = None,
    check_local_models_version: bool = True,
    validate_current_release_metadata: bool = True,
) -> CandidateReport:
    inventory_failures = _validate_evidence_inventory(evidence_dir)
    if inventory_failures:
        raise DriverError(inventory_failures)
    ledger_path = evidence_dir / "ledger.json"
    try:
        ledger = read_retained_ledger(ledger_path)
    except LedgerError as exc:
        raise DriverError(exc.failures) from None
    models = ledger.get("models")
    include_models = isinstance(models, Mapping) and models.get("decision") == "include"
    if validate_current_release_metadata:
        payload_failures = _validate_flat_payload_inventory(
            release_dir, include_models=include_models
        )
        payload_failures.extend(
            validate_release_dir(release_dir, expected_source_commit=source_commit)
        )
    else:
        expected_names, payload_failures = _ledger_candidate_file_names(ledger)
        payload_failures.extend(
            _validate_flat_payload_inventory(release_dir, expected_names=expected_names)
        )
        payload_failures.extend(_validate_retained_manifest_files(release_dir, ledger))
    if payload_failures:
        raise DriverError(payload_failures)
    digest = candidate_digest(release_dir)
    deep_failures = _validate_deep_ledger_binding(
        root=root,
        version=version,
        source_commit=source_commit,
        expected_lock_sha256=expected_lock_sha256,
        release_dir=release_dir,
        evidence_dir=evidence_dir,
        ledger_path=ledger_path,
        ledger=ledger,
        policy_run=policy_run,
        check_local_models_version=check_local_models_version,
        validate_current_release_metadata=validate_current_release_metadata,
    )
    if ledger["candidate"]["candidate_digest"] != digest:
        deep_failures.append(
            _failure(
                "candidate digest does not match retained ledger",
                expected=str(ledger["candidate"]["candidate_digest"]),
                actual=digest,
                repair="bash scripts/release.sh --recover",
            )
        )
    if deep_failures:
        raise DriverError(deep_failures)
    ledger_sha256 = file_sha256_size(ledger_path)[0]
    proof_hashes = _proof_hashes(
        evidence_dir / "proofs",
        ledger=ledger,
        digest=digest,
        ledger_sha256=ledger_sha256,
        release_dir=release_dir,
        version=version,
    )
    return CandidateReport(
        heading=heading,
        version=version,
        release_dir=release_dir,
        evidence_dir=evidence_dir,
        payload_files=sum(1 for path in release_dir.iterdir() if path.is_file()),
        candidate_digest=digest,
        ledger_sha256=ledger_sha256,
        proof_sha256=proof_hashes,
        bundle_digest=bundle_digest(digest, ledger_sha256, proof_hashes),
    )


def _inventory_entry(path: Path, *, name: str) -> dict[str, Any]:
    sha256, byte_count = file_sha256_size(path)
    return {"name": name, "sha256": sha256, "bytes": byte_count}


def _payload_report_inventory(release_dir: Path) -> list[dict[str, Any]]:
    return [
        _inventory_entry(path, name=path.name)
        for path in sorted(release_dir.iterdir(), key=lambda item: item.name)
        if stat.S_ISREG(path.lstat().st_mode)
    ]


def _evidence_report_inventory(evidence_dir: Path) -> list[dict[str, Any]]:
    entries = [
        _inventory_entry(evidence_dir / "ledger.json", name="ledger.json"),
    ]
    proofs_dir = evidence_dir / "proofs"
    entries.extend(
        _inventory_entry(path, name=f"proofs/{path.name}")
        for path in sorted(proofs_dir.iterdir(), key=lambda item: item.name)
    )
    return entries


def _proof_report_inventory(evidence_dir: Path) -> dict[str, dict[str, Any]]:
    proofs_dir = evidence_dir / "proofs"
    return {
        target: _inventory_entry(proofs_dir / f"{target}.json", name=f"{target}.json")
        for target in sorted(PROOF_TARGETS)
    }


def format_report(report: CandidateReport) -> str:
    payload = {
        "schema_version": 1,
        "kind": "solstone-release-candidate-report",
        "verdict": report.heading,
        "publication_authorization": "local candidate evidence only; not publication authorization",
        "version": report.version,
        "release_dir": CANDIDATE,
        "evidence_dir": "EVIDENCE",
        "payload_files": report.payload_files,
        "candidate_digest": report.candidate_digest,
        "ledger_sha256": report.ledger_sha256,
        "bundle_digest": report.bundle_digest,
        "payload_inventory": _payload_report_inventory(report.release_dir),
        "evidence_inventory": _evidence_report_inventory(report.evidence_dir),
        "proof_inventory": _proof_report_inventory(report.evidence_dir),
        "proof_sha256": {
            target: report.proof_sha256[target]
            for target in sorted(report.proof_sha256)
        },
    }
    return canonical_json_bytes(payload).decode("utf-8")


def default_dry_run_plan(env: Mapping[str, str]) -> DryRunPlan:
    include_models = _models_include(env)
    return DryRunPlan(
        models_decision="include" if include_models else "exclude",
        artifacts=tuple(sorted(expected_package_names(include_models=include_models))),
        tool_evidence={
            lane: expected_lane_tool_evidence(lane)
            for lane in ("source", "linux-x86_64-musl", "linux-aarch64-musl")
        },
        linux_maturin_args={
            "x86_64-unknown-linux-musl": CORE_X86_64_MATURIN_ARGS,
            "aarch64-unknown-linux-musl": CORE_AARCH64_MATURIN_ARGS,
        },
        publication_lockout={
            "default": True,
            "--test": True,
            "make release": True,
            "make release-test": True,
        },
    )


def validate_dry_run_plan(plan: DryRunPlan) -> list[Failure]:
    failures: list[Failure] = []
    if plan.models_decision not in {"include", "exclude"}:
        failures.append(
            _failure(
                "dry-run model decision is invalid",
                expected="include or exclude",
                actual=plan.models_decision,
                repair="bash scripts/release.sh --dry-run-linux",
            )
        )
        include_models = False
    else:
        include_models = plan.models_decision == "include"
    expected_artifacts = set(expected_package_names(include_models=include_models))
    actual_artifacts = set(plan.artifacts)
    if actual_artifacts != expected_artifacts:
        failures.append(
            _failure(
                "dry-run artifact plan is invalid",
                expected=", ".join(sorted(expected_artifacts)),
                actual=", ".join(sorted(actual_artifacts)) or "<empty>",
                repair="bash scripts/release.sh --dry-run-linux",
            )
        )
    linux_core_wheels = [
        name
        for name in actual_artifacts
        if name.startswith("solstone_core-")
        and ("manylinux2014_x86_64" in name or "manylinux2014_aarch64" in name)
    ]
    if len(linux_core_wheels) != 2:
        failures.append(
            _failure(
                "dry-run Linux core wheel plan is incomplete",
                expected="x86_64 and aarch64 musl core wheels",
                actual=", ".join(sorted(linux_core_wheels)) or "<empty>",
                repair="bash scripts/release.sh --dry-run-linux",
            )
        )
    expected_tool_lanes = {"source", "linux-x86_64-musl", "linux-aarch64-musl"}
    if set(plan.tool_evidence) != expected_tool_lanes:
        failures.append(
            _failure(
                "dry-run tool evidence lanes are invalid",
                expected=", ".join(sorted(expected_tool_lanes)),
                actual=", ".join(sorted(str(key) for key in plan.tool_evidence))
                or "<empty>",
                repair="bash scripts/release.sh --dry-run-linux",
            )
        )
    for lane in sorted(set(plan.tool_evidence) & expected_tool_lanes):
        failures.extend(check_lane_tool_evidence(lane, plan.tool_evidence[lane]))
    expected_args = {
        "x86_64-unknown-linux-musl": CORE_X86_64_MATURIN_ARGS,
        "aarch64-unknown-linux-musl": CORE_AARCH64_MATURIN_ARGS,
    }
    if set(plan.linux_maturin_args) != set(expected_args):
        failures.append(
            _failure(
                "dry-run Linux build-arg targets are invalid",
                expected=", ".join(sorted(expected_args)),
                actual=", ".join(sorted(str(key) for key in plan.linux_maturin_args))
                or "<empty>",
                repair="bash scripts/release.sh --dry-run-linux",
            )
        )
    for target, expected in sorted(expected_args.items()):
        actual = plan.linux_maturin_args.get(target, "")
        if actual != expected:
            failures.append(
                _failure(
                    "dry-run Linux build arguments are invalid",
                    expected=expected,
                    actual=actual or "<missing>",
                    repair="bash scripts/release.sh --dry-run-linux",
                )
            )
        failures.extend(validate_linux_maturin_args(actual, target=target))
    expected_lockouts = {
        "default": True,
        "--test": True,
        "make release": True,
        "make release-test": True,
    }
    if dict(plan.publication_lockout) != expected_lockouts:
        failures.append(
            _failure(
                "dry-run publication lockout plan is invalid",
                expected=repr(expected_lockouts),
                actual=repr(dict(plan.publication_lockout)),
                repair="bash scripts/release.sh --dry-run-linux",
            )
        )
    return failures


def run_candidate(
    root: Path,
    env: Mapping[str, str],
    services: CandidateServices | None = None,
) -> CandidateReport:
    svc = services or default_services(env)
    version = _project_version(root)
    expected_commit = _expected_commit(env)
    include_models = _models_include(env)
    models_decision = "include" if include_models else "exclude"
    models_package_version = _local_models_package_version(root)
    expected_lock = svc.core_lock_sha256(root)
    _assert_clean_identity(
        root,
        expected_commit=expected_commit,
        expected_lock_sha256=expected_lock,
        services=svc,
    )
    svc.clean_outputs(root, version)
    policy_run = svc.prepare_policy(root, env)
    coordinator_tool_evidence = _validate_coordinator_tool_evidence(
        svc.coordinator_tool_evidence()
    )
    svc.build_local_dist(root, include_models)
    transfer_dir = root / "target" / "release-transfer" / version
    try:
        source_bundle = svc.create_source_bundle(
            root, expected_commit, transfer_dir / "source.bundle"
        )
        host_result = svc.build_host(source_bundle, expected_commit, transfer_dir)
        native_records = _native_record_payloads(
            host_result,
            source_commit=expected_commit,
            core_lock_sha256=expected_lock,
        )
        full_tool_evidence = _validated_full_tool_evidence(
            coordinator_tool_evidence,
            host_result,
            native_records,
        )
        lane_evidence = _lane_evidence_from_full_tool_evidence(
            full_tool_evidence,
            policy_run=policy_run,
        )
        _copy_macos_wheels(host_result, root / "dist")
        _revalidate_macos_wheels(
            root / "dist",
            native_records,
            source_commit=expected_commit,
            core_lock_sha256=expected_lock,
        )
    finally:
        svc.cleanup_transients((transfer_dir,))
    _assert_clean_identity(
        root,
        expected_commit=expected_commit,
        expected_lock_sha256=expected_lock,
        services=svc,
    )
    ready_path = root / "dist" / "release-candidate" / version
    ready_path.parent.mkdir(parents=True, exist_ok=True)
    payload_staging = ready_path.parent / f"{version}.payload-staging"
    evidence_root = root / "target" / "release-evidence"
    evidence_dir = evidence_root / version
    evidence_staging = evidence_root / f"{version}.staging"
    proof_paths: dict[str, Path] = {}

    def post_promote_hook(release_dir: Path) -> None:
        _validate_candidate_wheel_contents(
            release_dir,
            models_decision=models_decision,
        )
        _revalidate_macos_wheels(
            release_dir,
            native_records,
            source_commit=expected_commit,
            core_lock_sha256=expected_lock,
        )
        ledger_path = write_ledger(
            evidence_root=evidence_root,
            version=version,
            source_commit=expected_commit,
            release_dir=release_dir,
            core_lock_path=root / "core" / "Cargo.lock",
            tool_evidence={
                lane: full_tool_evidence[lane] for lane in sorted(full_tool_evidence)
            },
            policy_run=policy_run,
            native_records=native_records,
            models={
                "decision": models_decision,
                "package_version": models_package_version,
            },
            output_dir=evidence_staging,
        )
        ledger_sha256 = file_sha256_size(ledger_path)[0]
        ledger_payload = read_retained_ledger(ledger_path)
        candidate_paths = sorted(
            path for path in release_dir.iterdir() if path.is_file()
        )
        proofs_dir = evidence_staging / "proofs"
        for target in PROOF_TARGETS:
            proof_paths[target] = svc.run_install_proof(
                target=target,
                version=version,
                source_commit=expected_commit,
                core_lock_sha256=expected_lock,
                candidate_digest=ledger_payload["candidate"]["candidate_digest"],
                ledger_sha256=ledger_sha256,
                candidate_dir=release_dir,
                candidate_paths=candidate_paths,
                ledger_payload=ledger_payload,
                output_path=proofs_dir / f"{target}.json",
            )
        failures = validate_public_evidence_tree("ledger", ledger_payload)
        if failures:
            raise DriverError(failures)

    try:
        failures = build_and_promote_candidate(
            root / "dist",
            payload_staging,
            source_commit=expected_commit,
            evidence_by_lane=lane_evidence,
            include_models=include_models,
            cargo_lock_path=root / "core" / "Cargo.lock",
            _post_promote_hook=post_promote_hook,
        )
    except BaseException as exc:
        cleanup_failures = _cleanup_owned_cohorts(
            (payload_staging, evidence_staging, ready_path, evidence_dir)
        )
        raise _aggregate_finalization_error(exc, cleanup_failures) from None
    if failures:
        cleanup_failures = _cleanup_owned_cohorts((payload_staging, evidence_staging))
        raise DriverError([*failures, *cleanup_failures])
    try:
        _pair_promote_payload_and_evidence(
            payload_staging=payload_staging,
            ready_path=ready_path,
            evidence_staging=evidence_staging,
            evidence_dir=evidence_dir,
            include_models=include_models,
            hook=svc.transaction_hook,
        )
        _assert_clean_identity(
            root,
            expected_commit=expected_commit,
            expected_lock_sha256=expected_lock,
            services=svc,
        )
        report = _report(
            heading="candidate-proven",
            root=root,
            version=version,
            source_commit=expected_commit,
            expected_lock_sha256=expected_lock,
            release_dir=ready_path,
            evidence_dir=evidence_dir,
            policy_run=policy_run,
        )
    except BaseException as exc:
        cleanup_failures = _cleanup_owned_cohorts((ready_path, evidence_dir))
        raise _aggregate_finalization_error(exc, cleanup_failures) from None
    _ = proof_paths
    return report


def run_recover(
    root: Path,
    *,
    version: str,
    source_commit: str,
) -> CandidateReport:
    if not version:
        raise DriverError(
            [
                _failure(
                    "retained release version selector is missing",
                    expected="explicit retained release version",
                    actual="<missing>",
                    repair="bash scripts/release.sh --recover",
                )
            ]
        )
    if not _safe_retained_basename(version):
        raise DriverError(
            [
                _failure(
                    "retained release version selector is unsafe",
                    expected="safe retained release version basename",
                    actual=repr(version),
                    repair="bash scripts/release.sh --recover",
                )
            ]
        )
    if not SOURCE_COMMIT_RE.fullmatch(source_commit):
        raise DriverError(
            [
                _failure(
                    "retained release source selector is invalid",
                    expected="40 or 64 lowercase hexadecimal characters",
                    actual=source_commit or "<missing>",
                    repair="bash scripts/release.sh --recover",
                )
            ]
        )
    release_dir = root / "dist" / "release-candidate" / version
    evidence_dir = root / "target" / "release-evidence" / version
    try:
        ledger = read_retained_ledger(evidence_dir / "ledger.json")
    except OSError as exc:
        raise DriverError(
            [
                _failure(
                    "retained ledger could not be read for selector",
                    expected="retained ledger.json for explicit version",
                    actual=type(exc).__name__,
                    repair="bash scripts/release.sh --recover",
                )
            ]
        ) from None
    except LedgerError as exc:
        raise DriverError(exc.failures) from None
    selector_failures: list[Failure] = []
    if ledger.get("version") != version:
        selector_failures.append(
            _failure(
                "retained ledger version does not match selector",
                expected=version,
                actual=str(ledger.get("version")),
                repair="bash scripts/release.sh --recover",
            )
        )
    if ledger.get("source_commit") != source_commit:
        selector_failures.append(
            _failure(
                "retained ledger source commit does not match selector",
                expected=source_commit,
                actual=str(ledger.get("source_commit")),
                repair="bash scripts/release.sh --recover",
            )
        )
    if selector_failures:
        raise DriverError(selector_failures)
    return _report(
        heading="retained-candidate-valid",
        root=root,
        version=version,
        source_commit=source_commit,
        expected_lock_sha256=str(ledger["core_lock_sha256"]),
        release_dir=release_dir,
        evidence_dir=evidence_dir,
        check_local_models_version=False,
        validate_current_release_metadata=False,
    )


def run_dry_run_linux(
    _root: Path,
    env: Mapping[str, str],
    *,
    plan: DryRunPlan | None = None,
) -> str:
    dry_run_plan = plan or default_dry_run_plan(env)
    failures = validate_dry_run_plan(dry_run_plan)
    if failures:
        raise DriverError(failures)
    return (
        "linux structural dry-run validated\n"
        "no release candidate, manifest, ledger, proof, or clean-source claim emitted"
    )


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    services: CandidateServices | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Finalize solstone release candidates."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("candidate")
    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--version", required=True)
    recover_parser.add_argument("--source-commit", required=True)
    subparsers.add_parser("dry-run-linux")
    args = parser.parse_args(argv)
    runtime_env = os.environ if env is None else env
    root = Path(__file__).resolve().parent.parent
    try:
        if args.command == "candidate":
            sys.stdout.write(format_report(run_candidate(root, runtime_env, services)))
        elif args.command == "recover":
            sys.stdout.write(
                format_report(
                    run_recover(
                        root,
                        version=args.version,
                        source_commit=args.source_commit,
                    )
                )
            )
        else:
            print(run_dry_run_linux(root, runtime_env))
    except (DriverError, Exception) as exc:
        if isinstance(exc, DriverError):
            _format_failures(exc.failures)
        else:
            _format_failures(
                [
                    _failure(
                        "release candidate driver failed",
                        expected="successful candidate operation",
                        actual=type(exc).__name__,
                        repair="bash scripts/release.sh --candidate",
                    )
                ]
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
