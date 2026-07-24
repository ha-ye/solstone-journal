#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Generate and validate native install/smoke release proofs."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.check_rust_release_manifest import (
    LANES,
    SHA256_RE,
    SOURCE_COMMIT_RE,
    Failure,
    canonical_json_bytes,
)
from scripts.check_wheel_contents import CORE_SCRIPT_NAMES
from scripts.release_digest import file_sha256_size
from scripts.release_public_evidence import validate_public_evidence_tree

PROOF_TARGETS: tuple[str, ...] = (
    "linux-x86_64-musl",
    "linux-aarch64-musl",
    "macos-arm64",
)
TOP_LEVEL_KEYS = frozenset(
    (
        "schema_version",
        "kind",
        "target",
        "version",
        "source_commit",
        "candidate_digest",
        "ledger_sha256",
        "core_lock_sha256",
        "candidate_files",
        "environment",
        "install",
        "installed_members",
        "smoke",
        "recorded_at",
    )
)
PROOF_KIND = "solstone-native-install-proof"
CORE_SMOKE_STDOUT = {
    "sol": "solstone-core-sol",
    "solstone": "solstone-core-sol",
    "solstone-core": "solstone-core",
}
ENVROOT = "ENVROOT"
CANDIDATE = "CANDIDATE"
RETAINED_PROOF_REPAIR = (
    "restore the retained install proof from unmodified release evidence; "
    "--recover validates only and cannot repair mutated proof bytes"
)
SCRUBBED_COMMAND_ENV: Mapping[str, str] = {
    "PIP_NO_INDEX": "1",
    "PYTHONNOUSERSITE": "1",
}
FORBIDDEN_INSTALL_TOKENS = frozenset(
    (
        "--index-url",
        "--extra-index-url",
        "--find-links",
        "--trusted-host",
        "--editable",
        "-e",
        "--requirement",
        "-r",
        "--constraint",
        "-c",
        "--proxy",
        "--cert",
        "--client-cert",
        "--config-settings",
        "-C",
    )
)
FORBIDDEN_ENV_KEY_PARTS = (
    "INDEX",
    "FIND_LINKS",
    "PROXY",
    "TOKEN",
    "PASSWORD",
    "SECRET",
    "CREDENTIAL",
)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str = ""
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InstallObservation:
    env_root: Path
    preexisting_distributions: tuple[str, ...]
    install: CommandResult
    installed_distributions: tuple[Mapping[str, str], ...]
    installed_members: tuple[Mapping[str, Any], ...]
    smoke: Mapping[str, CommandResult]


@dataclass(frozen=True)
class InstallSmokeServices:
    create_environment: Callable[[str], Path]
    observe_install: Callable[[str, Path, Sequence[Path]], InstallObservation]
    clock: Callable[[], datetime]
    cleanup_environment: Callable[[Path], None] = lambda _path: None


class InstallProofError(RuntimeError):
    def __init__(self, failures: Sequence[Failure]) -> None:
        self.failures = tuple(failures)
        super().__init__("; ".join(failure.error for failure in self.failures))


def _failure(error: str, *, expected: str, actual: str, repair: str) -> Failure:
    return Failure(error=error, expected=expected, actual=actual, repair=repair)


def _format_utc(value: datetime) -> str:
    return (
        value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _default_create_environment(target: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix=f"solstone-proof-{target}-"))
    venv.EnvBuilder(with_pip=True, symlinks=False).create(root)
    return root


def _env_python(env_root: Path) -> Path:
    if sys.platform == "win32":
        return env_root / "Scripts" / "python.exe"
    return env_root / "bin" / "python"


def _env_bin(env_root: Path, name: str) -> Path:
    if sys.platform == "win32":
        return env_root / "Scripts" / f"{name}.exe"
    return env_root / "bin" / name


def _run_command(argv: Sequence[str]) -> CommandResult:
    result = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        env=dict(SCRUBBED_COMMAND_ENV),
    )
    return CommandResult(
        argv=tuple(argv),
        exit_code=result.returncode,
        stdout=result.stdout.strip(),
        stderr=result.stderr.strip(),
        env=SCRUBBED_COMMAND_ENV,
    )


def _solstone_distributions(env_python: Path) -> tuple[Mapping[str, str], ...]:
    # importlib.metadata can report the same dist-info directory twice when a
    # venv exposes both lib64 and lib site-packages. De-duplicate by canonical
    # dist-info realpath, not by (name, version): two distinct real dist-info
    # directories with the same name/version are polluted install evidence and
    # must still be rejected downstream. _path is private API; when it is absent
    # we fall back to a unique per-entry sentinel so the entry is never collapsed
    # -- we merge only entries proven to be the same dist-info directory.
    script = """
import importlib.metadata as m
import os

seen = set()
entries = []
unkeyed = 0
for d in m.distributions():
    name = d.metadata.get('Name', '')
    if not name.lower().startswith('solstone'):
        continue
    path = getattr(d, '_path', None)
    if path is None:
        key = ('unkeyed', unkeyed)
        unkeyed += 1
    else:
        key = ('realpath', os.path.realpath(str(path)))
    if key in seen:
        continue
    seen.add(key)
    entries.append(f"{name}=={d.version}")
print('\\n'.join(sorted(entries)))
"""
    result = subprocess.run(
        [str(env_python), "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=dict(SCRUBBED_COMMAND_ENV),
    )
    if result.returncode != 0:
        return ({"name": "metadata-query-failed", "version": "unknown"},)
    entries: list[Mapping[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name, version = line.split("==", 1)
        entries.append({"name": name, "version": version})
    return tuple(entries)


def _select_names_for_target(target: str, names: Sequence[str]) -> tuple[str, ...]:
    selected: list[str] = []
    for name in sorted(names):
        if not name.endswith(".whl"):
            continue
        if name.startswith("solstone_core-"):
            if target == "linux-x86_64-musl" and "x86_64" in name:
                selected.append(name)
            elif target == "linux-aarch64-musl" and "aarch64" in name:
                selected.append(name)
            elif target == "macos-arm64" and "macosx_14_0_arm64" in name:
                selected.append(name)
            continue
        if name.startswith("solstone-"):
            if target == "macos-arm64":
                if "macosx_14_0_arm64" in name:
                    selected.append(name)
            elif name.endswith("-py3-none-any.whl"):
                selected.append(name)
            continue
        if name.startswith(
            (
                "solstone_journal-",
                "solstone_journal_cuda-",
                "solstone_journal_models-",
            )
        ) and name.endswith("-py3-none-any.whl"):
            selected.append(name)
    return tuple(selected)


def target_install_paths_from_ledger(
    ledger_payload: Mapping[str, Any],
    *,
    target: str,
    candidate_dir: Path,
) -> tuple[Path, ...]:
    candidate = ledger_payload.get("candidate")
    entries = candidate.get("files", []) if isinstance(candidate, Mapping) else []
    names = [
        str(entry.get("name"))
        for entry in entries
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
    ]
    selected = _select_names_for_target(target, names)
    if not selected:
        raise InstallProofError(
            [
                _failure(
                    "install proof target install set is empty",
                    expected=f"{target} wheel install set from retained ledger",
                    actual="<empty>",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        )
    return tuple(candidate_dir / name for name in selected)


def _install_paths_for_target(
    target: str, candidate_paths: Sequence[Path]
) -> tuple[Path, ...]:
    by_name = {path.name: path for path in candidate_paths}
    return tuple(
        by_name[name]
        for name in _select_names_for_target(target, tuple(by_name))
        if name in by_name
    )


def _installed_member(path: Path, name: str) -> Mapping[str, Any]:
    sha256, _byte_count = file_sha256_size(path)
    return {
        "name": name,
        "path": path,
        "sha256": sha256,
        "symlink": path.is_symlink(),
    }


def _find_single(root: Path, name: str) -> Path | None:
    matches = sorted(path for path in root.rglob(name) if path.is_file())
    return matches[0] if len(matches) == 1 else None


def _distribution_from_wheel_metadata(path: Path) -> Mapping[str, str] | None:
    try:
        with zipfile.ZipFile(path) as wheel:
            metadata_names = sorted(
                name
                for name in wheel.namelist()
                if name.endswith(".dist-info/METADATA")
            )
            if len(metadata_names) != 1:
                return None
            metadata = wheel.read(metadata_names[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile):
        return None
    fields: dict[str, str] = {}
    for line in metadata.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key in {"Name", "Version"}:
            fields[key] = value.strip()
    if set(fields) != {"Name", "Version"}:
        return None
    return {"name": fields["Name"], "version": fields["Version"]}


def expected_distribution_entries(
    paths: Sequence[Path],
) -> tuple[Mapping[str, str], ...]:
    entries: list[Mapping[str, str]] = []
    failures: list[Failure] = []
    for path in paths:
        entry = _distribution_from_wheel_metadata(path)
        if entry is None:
            failures.append(
                _failure(
                    "install proof candidate wheel metadata is invalid",
                    expected=f"{path.name} exactly one readable METADATA with Name and Version",
                    actual="missing or malformed",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
            continue
        entries.append(entry)
    if failures:
        raise InstallProofError(failures)
    return tuple(sorted(entries, key=lambda item: (item["name"], item["version"])))


def _default_observe_install(
    target: str, env_root: Path, candidate_paths: Sequence[Path]
) -> InstallObservation:
    env_python = _env_python(env_root)
    before = tuple(entry["name"] for entry in _solstone_distributions(env_python))
    install_paths = _install_paths_for_target(target, candidate_paths)
    install = _run_command(
        (
            str(env_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            *(str(path) for path in install_paths),
        )
    )
    after = _solstone_distributions(env_python)
    installed_members: list[Mapping[str, Any]] = []
    core_paths = {name: _env_bin(env_root, name) for name in CORE_SCRIPT_NAMES}
    for name, core_path in core_paths.items():
        if core_path.is_file() or core_path.is_symlink():
            installed_members.append(_installed_member(core_path, name))
    helper_path = _find_single(env_root, "parakeet-helper")
    if target == "macos-arm64" and helper_path is not None:
        installed_members.append(_installed_member(helper_path, "parakeet-helper"))
    smoke: dict[str, CommandResult] = {}
    for name, core_path in core_paths.items():
        if core_path.exists() or core_path.is_symlink():
            smoke[name] = _run_command((str(core_path), "--version"))
        else:
            smoke[name] = CommandResult(
                argv=(str(core_path), "--version"),
                exit_code=127,
                stdout="",
                stderr="missing executable",
                env=SCRUBBED_COMMAND_ENV,
            )
    return InstallObservation(
        env_root=env_root,
        preexisting_distributions=before,
        install=install,
        installed_distributions=after,
        installed_members=tuple(installed_members),
        smoke=smoke,
    )


def default_services() -> InstallSmokeServices:
    return InstallSmokeServices(
        create_environment=_default_create_environment,
        observe_install=_default_observe_install,
        clock=_utc_now,
        cleanup_environment=lambda path: shutil.rmtree(path),
    )


def normalize_argv(argv: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    for token in argv:
        if token.startswith("--") and "=" in token:
            flag, value = token.split("=", 1)
            normalized.extend((flag, value))
        else:
            normalized.append(token)
    return normalized


def normalize_path(path: Path | str, *, env_root: Path, candidate_dir: Path) -> str:
    text = str(path)
    env_text = str(env_root)
    candidate_text = str(candidate_dir)
    if text == env_text:
        return ENVROOT
    if text.startswith(f"{env_text}{os.sep}"):
        return f"{ENVROOT}/{Path(text).relative_to(env_root).as_posix()}"
    if text == candidate_text:
        return CANDIDATE
    if text.startswith(f"{candidate_text}{os.sep}"):
        return f"{CANDIDATE}/{Path(text).relative_to(candidate_dir).as_posix()}"
    return text


def normalize_command_text(text: str, *, env_root: Path, candidate_dir: Path) -> str:
    # argv is rail-controlled and records the literal paths we passed; stdout/stderr
    # come from external tools (pip today, other proof tooling later) that may print
    # symlink-resolved spellings, such as macOS /tmp -> /private/tmp. The rail creates
    # env_root with tempfile.mkdtemp, and this scrub is limited to env_root/candidate_dir.
    root_spellings: dict[str, str] = {}
    for root, sentinel in ((env_root, ENVROOT), (candidate_dir, CANDIDATE)):
        root_spellings.setdefault(str(root), sentinel)
        root_spellings.setdefault(os.path.realpath(root), sentinel)
    escaped_roots = "|".join(
        re.escape(spelling)
        for spelling in sorted(root_spellings, key=len, reverse=True)
    )
    # The lookahead allowlist fails closed: unknown following characters leave the
    # absolute path in place so public-evidence validation rejects loudly.
    pattern = re.compile(f"(?:{escaped_roots})(?=[/\\s\"'),:;]|$)")
    return pattern.sub(lambda match: root_spellings[match.group(0)], text)


def _candidate_path_token(path: Path) -> str:
    return f"{CANDIDATE}/{path.name}"


def candidate_file_entries(paths: Sequence[Path]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    failures: list[Failure] = []
    for path in sorted(paths, key=lambda item: item.name):
        if path.is_symlink():
            failures.append(
                _failure(
                    "proof candidate file is a symlink",
                    expected="regular candidate file",
                    actual=path.name,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
            continue
        if path.name in seen:
            failures.append(
                _failure(
                    "proof candidate file basename is duplicated",
                    expected="unique candidate file basenames",
                    actual=path.name,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
            continue
        seen.add(path.name)
        sha256, byte_count = file_sha256_size(path)
        entries.append({"basename": path.name, "bytes": byte_count, "sha256": sha256})
    if failures:
        raise InstallProofError(failures)
    return entries


def _expected_native_members(
    ledger_payload: Mapping[str, Any], target: str
) -> Mapping[str, Mapping[str, Any]]:
    native = ledger_payload.get("native_members", {})
    if not isinstance(native, Mapping):
        return {}
    members = native.get(target)
    if not isinstance(members, Mapping):
        return {}
    return {
        str(name): member
        for name, member in members.items()
        if isinstance(member, Mapping)
    }


def _command_payload(
    result: CommandResult,
    *,
    env_root: Path,
    candidate_dir: Path,
) -> dict[str, Any]:
    return {
        "argv": [
            normalize_path(token, env_root=env_root, candidate_dir=candidate_dir)
            for token in normalize_argv(result.argv)
        ],
        "exit_code": result.exit_code,
        "stdout": normalize_command_text(
            result.stdout, env_root=env_root, candidate_dir=candidate_dir
        ),
        "stderr": normalize_command_text(
            result.stderr, env_root=env_root, candidate_dir=candidate_dir
        ),
        "env": dict(sorted(result.env.items())),
    }


def _forbidden_command_tokens(argv: Sequence[str]) -> list[str]:
    forbidden: list[str] = []
    for token in normalize_argv(argv):
        if token in FORBIDDEN_INSTALL_TOKENS:
            forbidden.append(token)
        elif token.startswith(("http://", "https://", "git+", "file://")):
            forbidden.append(token)
    return forbidden


def _env_failures(label: str, env: Mapping[str, str]) -> list[Failure]:
    failures: list[Failure] = []
    if dict(env) != dict(SCRUBBED_COMMAND_ENV):
        failures.append(
            _failure(
                f"{label} command environment is not scrubbed",
                expected=repr(dict(SCRUBBED_COMMAND_ENV)),
                actual=repr(dict(env)),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    for key in env:
        if key in SCRUBBED_COMMAND_ENV and env[key] == SCRUBBED_COMMAND_ENV[key]:
            continue
        upper = key.upper()
        if any(part in upper for part in FORBIDDEN_ENV_KEY_PARTS):
            failures.append(
                _failure(
                    f"{label} command environment carries resolver state",
                    expected="minimal proof command environment",
                    actual=key,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    return failures


def _expected_install_argv(
    *,
    env_root: Path,
    candidate_paths: Sequence[Path],
) -> tuple[str, ...]:
    return (
        f"{ENVROOT}/{_env_python(env_root).relative_to(env_root).as_posix()}",
        "-m",
        "pip",
        "install",
        "--no-index",
        "--no-deps",
        *(
            _candidate_path_token(path)
            for path in sorted(candidate_paths, key=lambda item: item.name)
        ),
    )


def _distribution_entries(
    value: Sequence[Mapping[str, str]],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted((str(entry.get("name")), str(entry.get("version"))) for entry in value)
    )


def _member_path_failures(
    *,
    env_root: Path,
    member: Mapping[str, Any],
    name: str,
) -> list[Failure]:
    failures: list[Failure] = []
    raw_path = member.get("path")
    try:
        path = Path(raw_path)
    except TypeError:
        path = Path("")
    if not path.is_absolute():
        return [
            _failure(
                "install proof member path is not absolute",
                expected="absolute executable path under isolated ENVROOT",
                actual=str(raw_path),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        ]
    try:
        path.lstat()
    except OSError as exc:
        return [
            _failure(
                "install proof member path cannot be inspected",
                expected="installed executable under isolated ENVROOT",
                actual=type(exc).__name__,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        ]
    if path.is_symlink():
        failures.append(
            _failure(
                "install proof member is a symlink",
                expected="regular installed executable",
                actual=name,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if not path.is_file() and not path.is_symlink():
        failures.append(
            _failure(
                "install proof member path is not a regular file",
                expected="regular installed executable",
                actual=name,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    try:
        resolved = path.resolve(strict=True)
        root = env_root.resolve(strict=True)
    except OSError as exc:
        failures.append(
            _failure(
                "install proof member path containment cannot be verified",
                expected="real path inside isolated ENVROOT",
                actual=type(exc).__name__,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    else:
        if resolved != root and root not in resolved.parents:
            failures.append(
                _failure(
                    "install proof member path escapes ENVROOT",
                    expected="real path inside isolated ENVROOT",
                    actual=name,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    return failures


def _validate_observation(
    *,
    target: str,
    version: str,
    observation: InstallObservation,
    ledger_payload: Mapping[str, Any],
    candidate_dir: Path,
    install_paths: Sequence[Path],
) -> list[Failure]:
    failures: list[Failure] = []
    if observation.preexisting_distributions:
        failures.append(
            _failure(
                "install proof environment already has solstone distributions",
                expected="empty isolated environment",
                actual=", ".join(observation.preexisting_distributions),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if observation.install.exit_code != 0:
        failures.append(
            _failure(
                "install proof command failed",
                expected="exit 0",
                actual=str(observation.install.exit_code),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    normalized_install_argv = tuple(
        normalize_path(
            token, env_root=observation.env_root, candidate_dir=candidate_dir
        )
        for token in normalize_argv(observation.install.argv)
    )
    expected_install_argv = _expected_install_argv(
        env_root=observation.env_root,
        candidate_paths=install_paths,
    )
    forbidden_tokens = _forbidden_command_tokens(observation.install.argv)
    if forbidden_tokens:
        failures.append(
            _failure(
                "install proof command contains forbidden resolver option",
                expected="offline install with no index, find-links, URL, or source option",
                actual=", ".join(forbidden_tokens),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if normalized_install_argv != expected_install_argv:
        failures.append(
            _failure(
                "install proof command argv is not exact",
                expected=repr(expected_install_argv),
                actual=repr(normalized_install_argv),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    failures.extend(_env_failures("install proof install", observation.install.env))
    try:
        expected_distributions = _distribution_entries(
            expected_distribution_entries(install_paths)
        )
    except InstallProofError as exc:
        failures.extend(exc.failures)
        expected_distributions = set()
    actual_distributions = _distribution_entries(observation.installed_distributions)
    if actual_distributions != expected_distributions:
        failures.append(
            _failure(
                "install proof installed distribution set is invalid",
                expected=repr(expected_distributions),
                actual=repr(actual_distributions),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    expected_members = _expected_native_members(ledger_payload, target)
    if not expected_members:
        failures.append(
            _failure(
                "install proof retained native members are missing",
                expected=f"{target} retained native member map",
                actual="<empty>",
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    seen_members: set[str] = set()
    for member in observation.installed_members:
        name = str(member.get("name", ""))
        if "expected_sha256" in member:
            failures.append(
                _failure(
                    "install proof observation supplies forbidden expected hash",
                    expected="expected native member hashes from retained ledger",
                    actual=name or "<unnamed>",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        if "wheel_member_path" in member:
            failures.append(
                _failure(
                    "install proof observation supplies forbidden wheel member path",
                    expected="wheel member path from retained ledger",
                    actual=name or "<unnamed>",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        if name in seen_members:
            failures.append(
                _failure(
                    "install proof member is duplicated",
                    expected="one installed member per executable",
                    actual=name,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        seen_members.add(name)
        if member.get("symlink") is True:
            failures.append(
                _failure(
                    "install proof member is a symlink",
                    expected="regular installed executable",
                    actual=name,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        failures.extend(
            _member_path_failures(
                env_root=observation.env_root,
                member=member,
                name=name,
            )
        )
        expected = expected_members.get(name)
        if expected is None:
            failures.append(
                _failure(
                    "install proof member is not retained in ledger",
                    expected=f"{target} retained native member",
                    actual=name or "<unnamed>",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
            continue
        expected_sha256 = expected.get("sha256")
        if isinstance(expected_sha256, str) and member.get("sha256") != expected_sha256:
            failures.append(
                _failure(
                    "installed member hash does not match ledger",
                    expected=expected_sha256,
                    actual=str(member.get("sha256")),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        elif not isinstance(expected_sha256, str):
            failures.append(
                _failure(
                    "retained ledger native member hash is invalid",
                    expected="lowercase SHA-256",
                    actual=repr(expected_sha256),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    expected_names = set(expected_members)
    if seen_members != expected_names:
        failures.append(
            _failure(
                "install proof member set does not match retained ledger",
                expected=", ".join(sorted(expected_names)) or "<empty>",
                actual=", ".join(sorted(seen_members)) or "<empty>",
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    expected_smoke_names = set(CORE_SCRIPT_NAMES)
    if set(observation.smoke) != expected_smoke_names:
        failures.append(
            _failure(
                "install proof smoke command set is invalid",
                expected=", ".join(sorted(expected_smoke_names)),
                actual=", ".join(sorted(observation.smoke)) or "<empty>",
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    for name, result in observation.smoke.items():
        normalized_smoke_argv = tuple(
            normalize_path(
                token,
                env_root=observation.env_root,
                candidate_dir=candidate_dir,
            )
            for token in normalize_argv(result.argv)
        )
        expected_smoke_argv = (f"{ENVROOT}/bin/{name}", "--version")
        if normalized_smoke_argv != expected_smoke_argv:
            failures.append(
                _failure(
                    "install proof smoke command argv is not exact",
                    expected=repr(expected_smoke_argv),
                    actual=repr(normalized_smoke_argv),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        failures.extend(_env_failures("install proof smoke", result.env))
        if result.exit_code != 0:
            failures.append(
                _failure(
                    "install proof smoke command failed",
                    expected=f"{name} version smoke exit 0",
                    actual=str(result.exit_code),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        expected_stdout = f"{CORE_SMOKE_STDOUT.get(name, name)} {version}"
        if result.stdout != expected_stdout:
            failures.append(
                _failure(
                    "install proof smoke stdout is not exact",
                    expected=expected_stdout,
                    actual=result.stdout,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    return failures


def build_install_proof(
    *,
    target: str,
    version: str,
    source_commit: str,
    core_lock_sha256: str,
    candidate_digest: str,
    ledger_sha256: str,
    candidate_dir: Path,
    candidate_paths: Sequence[Path],
    ledger_payload: Mapping[str, Any],
    observation: InstallObservation,
    recorded_at: datetime,
) -> dict[str, Any]:
    failures: list[Failure] = []
    if target not in PROOF_TARGETS:
        failures.append(
            _failure(
                "install proof target is invalid",
                expected=", ".join(PROOF_TARGETS),
                actual=target,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if not SOURCE_COMMIT_RE.fullmatch(source_commit):
        failures.append(
            _failure(
                "install proof source commit is invalid",
                expected="40 or 64 lowercase hexadecimal characters",
                actual=source_commit,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    for label, value in (
        ("core lock", core_lock_sha256),
        ("candidate digest", candidate_digest),
        ("ledger sha256", ledger_sha256),
    ):
        if not SHA256_RE.fullmatch(value):
            failures.append(
                _failure(
                    f"install proof {label} is invalid",
                    expected="lowercase SHA-256",
                    actual=value,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    try:
        install_paths = target_install_paths_from_ledger(
            ledger_payload,
            target=target,
            candidate_dir=candidate_dir,
        )
        files = candidate_file_entries(install_paths)
    except InstallProofError as exc:
        install_paths = ()
        files = []
        failures.extend(exc.failures)
    failures.extend(
        _validate_observation(
            target=target,
            version=version,
            observation=observation,
            ledger_payload=ledger_payload,
            candidate_dir=candidate_dir,
            install_paths=install_paths,
        )
    )
    if failures:
        raise InstallProofError(failures)

    env_root = observation.env_root
    installed_members = []
    for member in observation.installed_members:
        name = str(member["name"])
        expected_member = _expected_native_members(ledger_payload, target)[name]
        installed_members.append(
            {
                **{
                    key: value
                    for key, value in member.items()
                    if key
                    not in {
                        "path",
                        "symlink",
                        "expected_sha256",
                        "wheel_member_path",
                    }
                },
                "wheel_member_path": expected_member["path"],
                "installed_path": normalize_path(
                    member["path"],
                    env_root=env_root,
                    candidate_dir=candidate_dir,
                ),
            }
        )
    proof: dict[str, Any] = {
        "schema_version": 1,
        "kind": PROOF_KIND,
        "target": target,
        "version": version,
        "source_commit": source_commit,
        "candidate_digest": candidate_digest,
        "ledger_sha256": ledger_sha256,
        "core_lock_sha256": core_lock_sha256,
        "candidate_files": files,
        "environment": {
            "root": ENVROOT,
            "candidate_dir": CANDIDATE,
            "index_access": "disabled",
            "system_site_packages": False,
        },
        "install": {
            "command": _command_payload(
                observation.install,
                env_root=env_root,
                candidate_dir=candidate_dir,
            ),
            "installed_distributions": [
                dict(entry)
                for entry in sorted(
                    observation.installed_distributions,
                    key=lambda item: (item["name"], item["version"]),
                )
            ],
        },
        "installed_members": installed_members,
        "smoke": {
            name: _command_payload(
                result, env_root=env_root, candidate_dir=candidate_dir
            )
            for name, result in sorted(observation.smoke.items())
        },
        "recorded_at": _format_utc(recorded_at),
    }
    if set(proof) != TOP_LEVEL_KEYS:
        raise AssertionError("install proof key set drifted")
    public_failures = validate_public_evidence_tree("install_proof", proof)
    if public_failures:
        raise InstallProofError(public_failures)
    return proof


def write_install_proof(
    path: Path,
    proof: Mapping[str, Any],
    *,
    target: str,
    version: str,
    source_commit: str,
    core_lock_sha256: str,
    candidate_digest: str,
    ledger_sha256: str,
    candidate_dir: Path,
    ledger_payload: Mapping[str, Any],
) -> Path:
    failures = validate_install_proof(
        proof,
        target=target,
        version=version,
        source_commit=source_commit,
        core_lock_sha256=core_lock_sha256,
        candidate_digest=candidate_digest,
        ledger_sha256=ledger_sha256,
        candidate_dir=candidate_dir,
        ledger_payload=ledger_payload,
    )
    if failures:
        raise InstallProofError(failures)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json_bytes(proof)
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        with temp_path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    readback_failures = validate_install_proof_bytes(
        path.read_bytes(),
        target=target,
        version=version,
        source_commit=source_commit,
        core_lock_sha256=core_lock_sha256,
        candidate_digest=candidate_digest,
        ledger_sha256=ledger_sha256,
        candidate_dir=candidate_dir,
        ledger_payload=ledger_payload,
    )
    if readback_failures:
        raise InstallProofError(readback_failures)
    return path


def _validate_command_payload(label: str, value: Any) -> list[Failure]:
    failures: list[Failure] = []
    if not isinstance(value, Mapping) or set(value) != {
        "argv",
        "env",
        "exit_code",
        "stderr",
        "stdout",
    }:
        return [
            _failure(
                f"install proof {label} command payload is invalid",
                expected="argv, env, exit_code, stdout, stderr",
                actual=repr(value),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        ]
    if not isinstance(value.get("argv"), list) or not all(
        isinstance(token, str) for token in value.get("argv", [])
    ):
        failures.append(
            _failure(
                f"install proof {label} argv is invalid",
                expected="list of normalized string tokens",
                actual=repr(value.get("argv")),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    else:
        forbidden_tokens = _forbidden_command_tokens(value["argv"])
        if forbidden_tokens:
            failures.append(
                _failure(
                    f"install proof {label} command contains forbidden resolver option",
                    expected="offline install with no index, find-links, URL, or source option",
                    actual=", ".join(forbidden_tokens),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    if not isinstance(value.get("env"), Mapping):
        failures.append(
            _failure(
                f"install proof {label} env is invalid",
                expected="scrubbed environment object",
                actual=type(value.get("env")).__name__,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    else:
        failures.extend(_env_failures(f"install proof {label}", value["env"]))
    if not isinstance(value.get("exit_code"), int):
        failures.append(
            _failure(
                f"install proof {label} exit code is invalid",
                expected="integer exit code",
                actual=repr(value.get("exit_code")),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    for key in ("stdout", "stderr"):
        if not isinstance(value.get(key), str):
            failures.append(
                _failure(
                    f"install proof {label} {key} is invalid",
                    expected="string",
                    actual=repr(value.get(key)),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    return failures


def _proof_candidate_entries(
    proof: Mapping[str, Any],
) -> tuple[set[tuple[str, int, str]], list[Failure]]:
    entries = proof.get("candidate_files", [])
    if not isinstance(entries, list):
        return set(), []
    parsed: set[tuple[str, int, str]] = set()
    failures: list[Failure] = []
    for entry in entries:
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
        parsed.add(
            (
                str(entry.get("basename")),
                byte_count,
                str(entry.get("sha256")),
            )
        )
    return parsed, failures


def _candidate_entries_for_paths(paths: Sequence[Path]) -> set[tuple[str, int, str]]:
    return {
        (str(entry["basename"]), int(entry["bytes"]), str(entry["sha256"]))
        for entry in candidate_file_entries(paths)
    }


def _validate_proof_semantics(
    proof: Mapping[str, Any],
    *,
    target: str,
    version: str,
    ledger_payload: Mapping[str, Any],
    candidate_dir: Path,
) -> list[Failure]:
    failures: list[Failure] = []
    try:
        install_paths = target_install_paths_from_ledger(
            ledger_payload,
            target=target,
            candidate_dir=candidate_dir,
        )
    except InstallProofError as exc:
        return list(exc.failures)
    expected_entries = _candidate_entries_for_paths(install_paths)
    proof_entries, proof_entry_failures = _proof_candidate_entries(proof)
    failures.extend(proof_entry_failures)
    if proof_entries != expected_entries:
        failures.append(
            _failure(
                "install proof candidate inventory does not match target install set",
                expected="retained ledger target wheel install set",
                actual="proof candidate files differ",
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    install = proof.get("install")
    command = install.get("command") if isinstance(install, Mapping) else {}
    argv = tuple(command.get("argv", [])) if isinstance(command, Mapping) else ()
    expected_argv = _expected_install_argv(
        env_root=Path("/envroot"),
        candidate_paths=install_paths,
    )
    if argv != expected_argv:
        failures.append(
            _failure(
                "install proof command argv is not bound to target install set",
                expected=repr(expected_argv),
                actual=repr(argv),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    distributions = (
        install.get("installed_distributions", [])
        if isinstance(install, Mapping)
        else []
    )
    if not isinstance(distributions, list):
        failures.append(
            _failure(
                "install proof installed distributions are invalid",
                expected="list of name/version objects",
                actual=type(distributions).__name__,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
        distributions = []
    try:
        expected_distributions = _distribution_entries(
            expected_distribution_entries(install_paths)
        )
    except InstallProofError as exc:
        failures.extend(exc.failures)
        expected_distributions = set()
    actual_distributions = _distribution_entries(
        [entry for entry in distributions if isinstance(entry, Mapping)]
    )
    if actual_distributions != expected_distributions:
        failures.append(
            _failure(
                "install proof installed distributions do not match target wheels",
                expected=repr(expected_distributions),
                actual=repr(actual_distributions),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    expected_members = _expected_native_members(ledger_payload, target)
    proof_members = proof.get("installed_members", [])
    seen: dict[str, Mapping[str, Any]] = {}
    if isinstance(proof_members, list):
        seen = {
            str(member.get("name")): member
            for member in proof_members
            if isinstance(member, Mapping)
        }
    if set(seen) != set(expected_members):
        failures.append(
            _failure(
                "install proof installed member set does not match ledger",
                expected=", ".join(sorted(expected_members)) or "<empty>",
                actual=", ".join(sorted(seen)) or "<empty>",
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    for name, expected in expected_members.items():
        member = seen.get(name)
        if member is None:
            continue
        if member.get("wheel_member_path") != expected.get("path"):
            failures.append(
                _failure(
                    "install proof wheel member path does not match ledger",
                    expected=str(expected.get("path")),
                    actual=str(member.get("wheel_member_path")),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        installed_path = member.get("installed_path")
        if not isinstance(installed_path, str) or not installed_path.startswith(
            f"{ENVROOT}/"
        ):
            failures.append(
                _failure(
                    "install proof installed path is invalid",
                    expected="normalized installed executable path beneath ENVROOT",
                    actual=repr(installed_path),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        if installed_path == member.get("wheel_member_path"):
            failures.append(
                _failure(
                    "install proof member paths are conflated",
                    expected="distinct wheel_member_path and installed_path",
                    actual=repr(installed_path),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        if member.get("sha256") != expected.get("sha256"):
            failures.append(
                _failure(
                    "install proof installed member hash does not match ledger",
                    expected=str(expected.get("sha256")),
                    actual=str(member.get("sha256")),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    smoke = proof.get("smoke")
    smoke_items = smoke if isinstance(smoke, Mapping) else {}
    if set(smoke_items) != set(CORE_SCRIPT_NAMES):
        failures.append(
            _failure(
                "install proof smoke command set does not match release executables",
                expected=", ".join(CORE_SCRIPT_NAMES),
                actual=", ".join(sorted(str(key) for key in smoke_items)) or "<empty>",
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    for name in CORE_SCRIPT_NAMES:
        smoke_entry = smoke_items.get(name) if isinstance(smoke_items, Mapping) else {}
        if not isinstance(smoke_entry, Mapping):
            failures.append(
                _failure(
                    "install proof smoke result does not match release version",
                    expected=f"{name} smoke result object",
                    actual=type(smoke_entry).__name__,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
            continue
        expected_stdout = f"{CORE_SMOKE_STDOUT[name]} {version}"
        expected_smoke = {
            "argv": [f"{ENVROOT}/bin/{name}", "--version"],
            "env": dict(SCRUBBED_COMMAND_ENV),
            "exit_code": 0,
            "stdout": expected_stdout,
            "stderr": "",
        }
        if dict(smoke_entry) != expected_smoke:
            failures.append(
                _failure(
                    "install proof smoke result does not match release version",
                    expected=repr(expected_smoke),
                    actual=repr(dict(smoke_entry)),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    return failures


def validate_install_proof_bytes(
    data: bytes,
    *,
    target: str,
    version: str,
    source_commit: str,
    core_lock_sha256: str,
    candidate_digest: str,
    ledger_sha256: str,
    candidate_dir: Path,
    ledger_payload: Mapping[str, Any],
) -> list[Failure]:
    import json

    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [
            _failure(
                "install proof is not valid JSON",
                expected="canonical JSON object",
                actual=str(exc),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        ]
    if not isinstance(payload, Mapping):
        return [
            _failure(
                "install proof is not an object",
                expected="JSON object",
                actual=type(payload).__name__,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        ]
    failures: list[Failure] = []
    if data != canonical_json_bytes(payload):
        failures.append(
            _failure(
                "install proof bytes are not canonical",
                expected="canonical sorted-key JSON bytes",
                actual="non-canonical JSON",
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    failures.extend(
        validate_install_proof(
            payload,
            target=target,
            version=version,
            source_commit=source_commit,
            core_lock_sha256=core_lock_sha256,
            candidate_digest=candidate_digest,
            ledger_sha256=ledger_sha256,
            candidate_dir=candidate_dir,
            ledger_payload=ledger_payload,
        )
    )
    return failures


def validate_install_proof(
    proof: Mapping[str, Any],
    *,
    target: str,
    version: str,
    source_commit: str,
    core_lock_sha256: str,
    candidate_digest: str,
    ledger_sha256: str,
    candidate_dir: Path,
    ledger_payload: Mapping[str, Any],
) -> list[Failure]:
    failures: list[Failure] = []
    if set(proof) != TOP_LEVEL_KEYS:
        failures.append(
            _failure(
                "install proof key set is invalid",
                expected=", ".join(sorted(TOP_LEVEL_KEYS)),
                actual=", ".join(sorted(str(key) for key in proof)),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if proof.get("kind") != PROOF_KIND:
        failures.append(
            _failure(
                "install proof kind is invalid",
                expected=PROOF_KIND,
                actual=str(proof.get("kind")),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if proof.get("target") not in PROOF_TARGETS:
        failures.append(
            _failure(
                "install proof target is invalid",
                expected=", ".join(PROOF_TARGETS),
                actual=str(proof.get("target")),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    for key, expected in (
        ("target", target),
        ("version", version),
        ("source_commit", source_commit),
        ("core_lock_sha256", core_lock_sha256),
        ("candidate_digest", candidate_digest),
        ("ledger_sha256", ledger_sha256),
    ):
        if proof.get(key) != expected:
            failures.append(
                _failure(
                    f"install proof {key} is not bound to retained candidate",
                    expected=str(expected),
                    actual=str(proof.get(key)),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    if not isinstance(proof.get("version"), str) or not proof.get("version"):
        failures.append(
            _failure(
                "install proof version is invalid",
                expected="non-empty version string",
                actual=repr(proof.get("version")),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if not isinstance(
        proof.get("source_commit"), str
    ) or not SOURCE_COMMIT_RE.fullmatch(str(proof.get("source_commit"))):
        failures.append(
            _failure(
                "install proof source commit is invalid",
                expected="40 or 64 lowercase hexadecimal characters",
                actual=repr(proof.get("source_commit")),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    for label in ("candidate_digest", "ledger_sha256", "core_lock_sha256"):
        value = proof.get(label)
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            failures.append(
                _failure(
                    f"install proof {label} is invalid",
                    expected="lowercase SHA-256",
                    actual=repr(value),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    recorded_at = proof.get("recorded_at")
    if not isinstance(recorded_at, str) or not recorded_at.endswith("Z"):
        failures.append(
            _failure(
                "install proof recorded_at is invalid",
                expected="UTC RFC3339 timestamp",
                actual=repr(proof.get("recorded_at")),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    environment = proof.get("environment")
    if environment != {
        "root": ENVROOT,
        "candidate_dir": CANDIDATE,
        "index_access": "disabled",
        "system_site_packages": False,
    }:
        failures.append(
            _failure(
                "install proof environment is invalid",
                expected="isolated ENVROOT with disabled index access",
                actual=repr(environment),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    seen_files: set[str] = set()
    for entry in proof.get("candidate_files", []):
        if not isinstance(entry, Mapping) or set(entry) != {
            "basename",
            "bytes",
            "sha256",
        }:
            failures.append(
                _failure(
                    "install proof candidate file entry is invalid",
                    expected="basename, bytes, sha256",
                    actual=repr(entry),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
            continue
        basename = entry["basename"]
        if (
            not isinstance(basename, str)
            or not basename
            or Path(basename).name != basename
            or "/" in basename
            or "\\" in basename
        ):
            failures.append(
                _failure(
                    "install proof candidate file basename is invalid",
                    expected="safe candidate file basename",
                    actual=repr(basename),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        if basename in seen_files:
            failures.append(
                _failure(
                    "install proof candidate file is duplicated",
                    expected="unique candidate file basenames",
                    actual=str(basename),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        seen_files.add(str(basename))
        if not isinstance(entry["bytes"], int) or entry["bytes"] < 0:
            failures.append(
                _failure(
                    "install proof candidate file byte count is invalid",
                    expected="non-negative integer",
                    actual=repr(entry["bytes"]),
                    repair=RETAINED_PROOF_REPAIR,
                )
            )
        if not isinstance(entry["sha256"], str) or not SHA256_RE.fullmatch(
            entry["sha256"]
        ):
            failures.append(
                _failure(
                    "install proof candidate file sha256 is invalid",
                    expected="lowercase SHA-256",
                    actual=str(entry["sha256"]),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    installed_members = proof.get("installed_members")
    if not isinstance(installed_members, list):
        failures.append(
            _failure(
                "install proof installed_members is invalid",
                expected="list of installed native members",
                actual=type(installed_members).__name__,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
        installed_members = []
    seen_members: set[str] = set()
    for entry in installed_members:
        if not isinstance(entry, Mapping) or set(entry) != {
            "name",
            "wheel_member_path",
            "installed_path",
            "sha256",
        }:
            failures.append(
                _failure(
                    "install proof installed member entry is invalid",
                    expected="name, wheel_member_path, installed_path, sha256",
                    actual=repr(entry),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
            continue
        name = str(entry["name"])
        if name in seen_members:
            failures.append(
                _failure(
                    "install proof installed member is duplicated",
                    expected="unique installed native members",
                    actual=name,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        seen_members.add(name)
        wheel_member_path = entry["wheel_member_path"]
        installed_path = entry["installed_path"]
        if not isinstance(wheel_member_path, str) or not wheel_member_path:
            failures.append(
                _failure(
                    "install proof wheel member path is invalid",
                    expected="retained wheel-internal member path",
                    actual=repr(wheel_member_path),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        if not isinstance(installed_path, str) or not installed_path.startswith(
            f"{ENVROOT}/"
        ):
            failures.append(
                _failure(
                    "install proof installed path is invalid",
                    expected="normalized installed executable path beneath ENVROOT",
                    actual=repr(installed_path),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        if wheel_member_path == installed_path:
            failures.append(
                _failure(
                    "install proof member paths are conflated",
                    expected="distinct wheel_member_path and installed_path",
                    actual=repr(wheel_member_path),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        if not isinstance(entry["sha256"], str) or not SHA256_RE.fullmatch(
            entry["sha256"]
        ):
            failures.append(
                _failure(
                    "install proof installed member sha256 is invalid",
                    expected="lowercase SHA-256",
                    actual=repr(entry["sha256"]),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    install = proof.get("install")
    if not isinstance(install, Mapping) or set(install) != {
        "command",
        "installed_distributions",
    }:
        failures.append(
            _failure(
                "install proof install section is invalid",
                expected="command and installed_distributions",
                actual=repr(install),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
        install = {}
    command = install.get("command") if isinstance(install, Mapping) else None
    failures.extend(_validate_command_payload("install", command))
    distributions = (
        install.get("installed_distributions", [])
        if isinstance(install, Mapping)
        else []
    )
    if not isinstance(distributions, list):
        failures.append(
            _failure(
                "install proof installed distributions are invalid",
                expected="list of name/version objects",
                actual=type(distributions).__name__,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    else:
        seen_distributions: set[tuple[str, str]] = set()
        for entry in distributions:
            if not isinstance(entry, Mapping) or set(entry) != {"name", "version"}:
                failures.append(
                    _failure(
                        "install proof installed distribution entry is invalid",
                        expected="name, version",
                        actual=repr(entry),
                        repair="python3 scripts/check_rust_release_manifest.py",
                    )
                )
                continue
            pair = (str(entry["name"]), str(entry["version"]))
            if pair in seen_distributions:
                failures.append(
                    _failure(
                        "install proof installed distribution is duplicated",
                        expected="unique installed distribution entries",
                        actual=f"{pair[0]}=={pair[1]}",
                        repair="python3 scripts/check_rust_release_manifest.py",
                    )
                )
            seen_distributions.add(pair)
            if not all(
                isinstance(entry[key], str) and entry[key]
                for key in ("name", "version")
            ):
                failures.append(
                    _failure(
                        "install proof installed distribution scalar is invalid",
                        expected="non-empty name and version strings",
                        actual=repr(entry),
                        repair="python3 scripts/check_rust_release_manifest.py",
                    )
                )
    smoke = proof.get("smoke")
    if not isinstance(smoke, Mapping) or set(smoke) != set(CORE_SCRIPT_NAMES):
        failures.append(
            _failure(
                "install proof smoke section is invalid",
                expected=", ".join(CORE_SCRIPT_NAMES) + " smoke results",
                actual=repr(smoke),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
        smoke = {}
    for name, value in smoke.items():
        failures.extend(_validate_command_payload(f"smoke {name}", value))
    failures.extend(
        _validate_proof_semantics(
            proof,
            target=target,
            version=version,
            ledger_payload=ledger_payload,
            candidate_dir=candidate_dir,
        )
    )
    failures.extend(validate_public_evidence_tree("install_proof", proof))
    return failures


def run_install_proof(
    *,
    target: str,
    version: str,
    source_commit: str,
    core_lock_sha256: str,
    candidate_digest: str,
    ledger_sha256: str,
    candidate_dir: Path,
    candidate_paths: Sequence[Path],
    ledger_payload: Mapping[str, Any],
    output_path: Path,
    services: InstallSmokeServices | None = None,
) -> Path:
    resolved_services = services or default_services()
    env_root = resolved_services.create_environment(target)
    try:
        install_paths = target_install_paths_from_ledger(
            ledger_payload,
            target=target,
            candidate_dir=candidate_dir,
        )
        observation = resolved_services.observe_install(target, env_root, install_paths)
        proof = build_install_proof(
            target=target,
            version=version,
            source_commit=source_commit,
            core_lock_sha256=core_lock_sha256,
            candidate_digest=candidate_digest,
            ledger_sha256=ledger_sha256,
            candidate_dir=candidate_dir,
            candidate_paths=install_paths,
            ledger_payload=ledger_payload,
            observation=observation,
            recorded_at=resolved_services.clock(),
        )
        return write_install_proof(
            output_path,
            proof,
            target=target,
            version=version,
            source_commit=source_commit,
            core_lock_sha256=core_lock_sha256,
            candidate_digest=candidate_digest,
            ledger_sha256=ledger_sha256,
            candidate_dir=candidate_dir,
            ledger_payload=ledger_payload,
        )
    finally:
        resolved_services.cleanup_environment(env_root)


def proof_targets_match_lanes() -> bool:
    return set(PROOF_TARGETS) | {"source"} == set(LANES)
