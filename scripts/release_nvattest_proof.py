#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Generate and validate native nvattest compatibility release receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import urllib.request
import venv
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from scripts.build_nvattest_authority import render_nvattest_authority_json
from scripts.check_rust_release_manifest import (
    SHA256_RE,
    SOURCE_COMMIT_RE,
    Failure,
    canonical_json_bytes,
)
from scripts.check_wheel_contents import NVATTEST_AUTHORITY_MEMBER
from scripts.release_digest import file_sha256_size
from scripts.release_install_smoke import (
    CANDIDATE,
    ENVROOT,
    FORBIDDEN_INSTALL_TOKENS,
    PROOF_TARGETS,
    SCRUBBED_COMMAND_ENV,
    CommandResult,
    _env_failures,
    _env_python,
    _forbidden_command_tokens,
    _format_utc,
    _run_command,
    _solstone_distributions,
    candidate_file_entries,
    expected_distribution_entries,
)
from scripts.release_public_evidence import validate_public_evidence_tree
from scripts.release_target_policy import TARGET_POLICY
from solstone.think.providers.nvattest_authority import (
    NvattestTargetKey,
    authority_payload,
    nvattest_target_key,
    validate_authority_payload,
)
from solstone.think.providers.nvattest_install import (
    SIDECAR_NAME,
    SIDECAR_SCHEMA_VERSION,
    SPP_NVATTEST_DIR_ENV,
    cache_root,
)

NVATTEST_PROOF_KIND = "solstone-nvattest-compatibility-receipt"
NVATTEST_CACHE_ROOT = "NVATTEST_CACHE_ROOT"
SUPPORT = "SUPPORT"
PYTHON_SITE = "PYTHON_SITE"
DRIVER = "DRIVER"
MANIFEST_SCHEMA_VERSION = 2
CHALLENGE_RE = re.compile(r"^[0-9a-f]{64}$")
REPAIR = "regenerate the retained nvattest proof from the original release inputs"

NVATTEST_TOP_LEVEL_KEYS = frozenset(
    (
        "archive_fetch",
        "cache_install",
        "candidate_digest",
        "candidate_distributions",
        "challenge",
        "companion_manifest",
        "core_lock_sha256",
        "host",
        "installed_authority",
        "installed_package",
        "integrity",
        "kind",
        "ledger_sha256",
        "manifest_fetch",
        "nvattest",
        "recorded_at",
        "smoke",
        "source_commit",
        "support_distributions",
        "target",
        "version",
    )
)

SUPPORT_DISTRIBUTION_NAMES = frozenset(
    (
        "anyio",
        "certifi",
        "h11",
        "httpcore",
        "httpx",
        "idna",
        "sniffio",
        "typing-extensions",
    )
)


@dataclass(frozen=True)
class HostObservation:
    os: str
    arch: str


@dataclass(frozen=True)
class FetchObservation:
    label: Literal["archive", "manifest"]
    url: str
    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class DriverObservation:
    command: CommandResult
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class NvattestProofObservation:
    env_root: Path
    journal_path: Path
    cache_root: Path
    host: HostObservation
    install: CommandResult
    candidate_distributions: tuple[Mapping[str, str], ...]
    archive_fetch: FetchObservation
    manifest_fetch: FetchObservation
    installed_authority_path: Path
    installed_authority_bytes: bytes
    driver: DriverObservation
    integrity: Mapping[str, Any]
    smoke: CommandResult


@dataclass(frozen=True)
class NvattestProofServices:
    create_environment: Callable[[str], Path]
    install_wheels: Callable[
        [Path, Sequence[Path], Sequence[Path]],
        CommandResult,
    ]
    fetch: Callable[[Literal["archive", "manifest"], str, Path], FetchObservation]
    run_package_install: Callable[
        [Path, Path, str, Path],
        DriverObservation,
    ]
    integrity_recheck: Callable[
        [Path, str, Sequence[FetchObservation], DriverObservation],
        Mapping[str, Any],
    ]
    run_smoke: Callable[[Path], CommandResult]
    clock: Callable[[], datetime]
    cleanup: Callable[[Path], None]
    observe_host: Callable[[], HostObservation]


class NvattestProofError(RuntimeError):
    def __init__(self, failures: Sequence[Failure]):
        self.failures = tuple(failures)
        super().__init__("; ".join(failure.error for failure in self.failures))


def _failure(
    error: str, *, expected: str, actual: str, repair: str = REPAIR
) -> Failure:
    return Failure(error=error, expected=expected, actual=actual, repair=repair)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _default_create_environment(target: str) -> Path:
    env_root = Path(tempfile.mkdtemp(prefix=f"solstone-nvattest-{target}-"))
    venv.EnvBuilder(with_pip=True, clear=True, symlinks=True).create(env_root)
    return env_root


def _default_cleanup(path: Path) -> None:
    shutil.rmtree(path)


def _default_observe_host() -> HostObservation:
    return HostObservation(os=platform.system(), arch=platform.machine())


def _default_install_wheels(
    env_python: Path,
    candidate_wheels: Sequence[Path],
    support_wheels: Sequence[Path],
) -> CommandResult:
    support_by_name = {
        str(entry["name"]): path
        for entry, path in _support_wheel_entries_with_paths(support_wheels)
    }
    ordered_support = [
        support_by_name[name] for name in sorted(SUPPORT_DISTRIBUTION_NAMES)
    ]
    argv = (
        str(env_python),
        "-m",
        "pip",
        "install",
        "--no-index",
        "--no-deps",
        *(str(path) for path in candidate_wheels),
        *(str(path) for path in ordered_support),
    )
    return _run_command(argv)


def _default_fetch(
    label: Literal["archive", "manifest"],
    url: str,
    dest: Path,
) -> FetchObservation:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.tmp")
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            with tmp.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return FetchObservation(
        label=label,
        url=url,
        path=dest,
        sha256=digest.hexdigest(),
        size_bytes=size,
    )


def _default_run_package_install(
    env_python: Path,
    driver_path: Path,
    target_key: str,
    journal_path: Path,
) -> DriverObservation:
    result = _run_command(
        (
            str(env_python),
            str(driver_path),
            "--target-key",
            target_key,
            "--journal-path",
            str(journal_path),
        )
    )
    if result.exit_code != 0:
        raise NvattestProofError(
            [
                _failure(
                    "nvattest package driver failed",
                    expected="driver exit code 0",
                    actual=str(result.exit_code),
                )
            ]
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise NvattestProofError(
            [
                _failure(
                    "nvattest package driver did not emit JSON",
                    expected="single JSON object on stdout",
                    actual=str(exc),
                )
            ]
        ) from exc
    if not isinstance(payload, Mapping):
        raise NvattestProofError(
            [
                _failure(
                    "nvattest package driver payload is not an object",
                    expected="JSON object",
                    actual=type(payload).__name__,
                )
            ]
        )
    return DriverObservation(command=result, payload=payload)


def _default_integrity_recheck(
    journal_path: Path,
    target_key: str,
    proof_fetches: Sequence[FetchObservation],
    driver: DriverObservation,
) -> Mapping[str, Any]:
    del journal_path, target_key, proof_fetches
    payload = driver.payload
    sidecar = payload.get("sidecar")
    members = payload.get("members")
    if not isinstance(sidecar, Mapping) or not isinstance(members, list):
        raise NvattestProofError(
            [
                _failure(
                    "nvattest driver integrity payload is invalid",
                    expected="sidecar object and member list",
                    actual=repr(payload),
                )
            ]
        )
    return {
        "members": members,
        "sidecar": dict(sidecar),
        "sidecar_path": str(payload.get("sidecar_path", "")),
        "sidecar_sha256": str(payload.get("sidecar_sha256", "")),
        "sidecar_size_bytes": payload.get("sidecar_size_bytes"),
        "tree_fingerprint_sha256": str(payload.get("tree_fingerprint_sha256", "")),
    }


def _default_run_smoke(nvattest_bin: Path) -> CommandResult:
    return _run_command((str(nvattest_bin), "--help"))


def default_services() -> NvattestProofServices:
    return NvattestProofServices(
        create_environment=_default_create_environment,
        install_wheels=_default_install_wheels,
        fetch=_default_fetch,
        run_package_install=_default_run_package_install,
        integrity_recheck=_default_integrity_recheck,
        run_smoke=_default_run_smoke,
        clock=_utc_now,
        cleanup=_default_cleanup,
        observe_host=_default_observe_host,
    )


def _call_stage[T](reason: str, callback: Callable[[], T]) -> T:
    try:
        return callback()
    except NvattestProofError:
        raise
    except Exception as exc:
        raise NvattestProofError(
            [
                _failure(
                    reason,
                    expected=f"{reason} stage completed",
                    actual=str(exc),
                )
            ]
        ) from exc


def _canonical_authority_bytes() -> bytes:
    return render_nvattest_authority_json().encode("utf-8")


def _load_canonical_authority(data: bytes) -> Mapping[str, Any]:
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("nvattest authority must be a JSON object")
    validate_authority_payload(payload)
    if payload != authority_payload() and data == _canonical_authority_bytes():
        raise ValueError("nvattest canonical authority payload drifted")
    return payload


def _authority_target(
    canonical_authority_payload: Mapping[str, Any],
    target_key: str,
) -> Mapping[str, Any]:
    targets = canonical_authority_payload.get("targets")
    if not isinstance(targets, Mapping):
        raise NvattestProofError(
            [
                _failure(
                    "nvattest authority targets are invalid",
                    expected="targets object",
                    actual=type(targets).__name__,
                )
            ]
        )
    target = targets.get(target_key)
    if not isinstance(target, Mapping):
        raise NvattestProofError(
            [
                _failure(
                    "nvattest authority target is missing",
                    expected=target_key,
                    actual=repr(target),
                )
            ]
        )
    return target


def _target_key_from_policy(target: str, host: HostObservation) -> NvattestTargetKey:
    if target not in PROOF_TARGETS or target not in TARGET_POLICY:
        raise NvattestProofError(
            [
                _failure(
                    "nvattest proof target is invalid",
                    expected=", ".join(PROOF_TARGETS),
                    actual=target,
                )
            ]
        )
    policy_os, policy_arch = TARGET_POLICY[target]
    policy_key = nvattest_target_key(os_name=policy_os.lower(), arch=policy_arch)
    observed_key = nvattest_target_key(os_name=host.os.lower(), arch=host.arch)
    if policy_key is None or observed_key != policy_key:
        raise NvattestProofError(
            [
                _failure(
                    "host-validation",
                    expected=f"{policy_os}/{policy_arch} native host for {target}",
                    actual=f"{host.os}/{host.arch}",
                )
            ]
        )
    return policy_key


def _driver_script() -> str:
    return r"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import sysconfig
from pathlib import Path

from solstone.think.providers import nvattest_authority, nvattest_install


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _solstone_distribution_paths() -> list[dict[str, str]]:
    entries = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name", "")
        if not name.lower().replace("_", "-").startswith("solstone"):
            continue
        raw_path = getattr(dist, "_path", None)
        entries.append(
            {
                "name": name,
                "version": dist.version,
                "dist_info_path": str(Path(str(raw_path)).resolve()) if raw_path else "",
            }
        )
    return sorted(entries, key=lambda item: (item["name"], item["version"]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-key", required=True)
    parser.add_argument("--journal-path", required=True)
    args = parser.parse_args()

    spec = importlib.util.find_spec("solstone.think.providers.nvattest_install")
    authority_spec = importlib.util.find_spec(
        "solstone.think.providers.nvattest_authority"
    )
    entry = nvattest_authority.authority_entry(args.target_key)
    root = nvattest_install.install_nvattest(
        entry=entry,
        journal_path=args.journal_path,
    )
    authority_path = Path(nvattest_authority.__file__).with_name(
        "nvattest_authority_v1.json"
    )
    sidecar_path = root / nvattest_install.SIDECAR_NAME
    sidecar_bytes = sidecar_path.read_bytes()
    payload = {
        "authority_module_file": nvattest_authority.__file__,
        "authority_origin": authority_spec.origin if authority_spec else "",
        "authority_path": str(authority_path),
        "authority_sha256": _sha256(authority_path),
        "authority_size_bytes": authority_path.stat().st_size,
        "cache_root": str(root),
        "dist_info": _solstone_distribution_paths(),
        "journal_path": str(Path(args.journal_path)),
        "members": nvattest_install._payload_member_facts(root, entry),
        "module_file": nvattest_install.__file__,
        "module_origin": spec.origin if spec else "",
        "sidecar": json.loads(sidecar_bytes.decode("utf-8")),
        "sidecar_path": str(sidecar_path),
        "sidecar_sha256": hashlib.sha256(sidecar_bytes).hexdigest(),
        "sidecar_size_bytes": len(sidecar_bytes),
        "site_packages": sorted(
            {
                sysconfig.get_paths().get("purelib", ""),
                sysconfig.get_paths().get("platlib", ""),
            }
            - {""}
        ),
        "solstone_journal_present": "SOLSTONE_JOURNAL" in os.environ,
        "spp_nvattest_dir_present": nvattest_install.SPP_NVATTEST_DIR_ENV in os.environ,
        "tree_fingerprint_sha256": nvattest_install._tree_fingerprint_sha256(
            root,
            entry,
        ),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""".lstrip()


def _write_driver(root: Path) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="solstone-nvattest-driver-",
        suffix=".py",
        dir=root,
        delete=False,
    )
    with handle:
        handle.write(_driver_script())
        handle.flush()
        os.fsync(handle.fileno())
    return Path(handle.name)


def _read_installed_authority(env_root: Path) -> tuple[Path, bytes]:
    matches = sorted(env_root.rglob(NVATTEST_AUTHORITY_MEMBER))
    site_matches = [path for path in matches if "site-packages" in path.parts]
    if len(site_matches) != 1:
        raise NvattestProofError(
            [
                _failure(
                    "installed authority member is not unique",
                    expected="one authority JSON under env site packages",
                    actual=", ".join(str(path) for path in matches) or "<missing>",
                )
            ]
        )
    return site_matches[0], site_matches[0].read_bytes()


def _support_wheel_entries_with_paths(
    paths: Sequence[Path],
) -> tuple[tuple[Mapping[str, Any], Path], ...]:
    failures: list[Failure] = []
    entries: list[tuple[dict[str, Any], Path]] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_absolute() or not path.is_file() or path.suffix != ".whl":
            failures.append(
                _failure(
                    "nvattest support wheel path is invalid",
                    expected="absolute local wheel file path",
                    actual=str(raw_path),
                )
            )
            continue
        metadata = _wheel_distribution_metadata(path)
        if metadata is None:
            failures.append(
                _failure(
                    "nvattest support wheel metadata is invalid",
                    expected="one readable dist-info/METADATA with Name and Version",
                    actual=path.name,
                )
            )
            continue
        name = _normalize_distribution_name(metadata["name"])
        if name in seen:
            failures.append(
                _failure(
                    "nvattest support distribution is duplicated",
                    expected="one wheel per support distribution",
                    actual=name,
                )
            )
        seen.add(name)
        sha256, size_bytes = file_sha256_size(path)
        entries.append(
            (
                {
                    "bytes": size_bytes,
                    "filename": path.name,
                    "name": name,
                    "sha256": sha256,
                    "version": metadata["version"],
                },
                path,
            )
        )
    observed_names = {str(entry["name"]) for entry, _path in entries}
    if observed_names != SUPPORT_DISTRIBUTION_NAMES:
        failures.append(
            _failure(
                "nvattest support distribution set is invalid",
                expected=", ".join(sorted(SUPPORT_DISTRIBUTION_NAMES)),
                actual=", ".join(sorted(observed_names)) or "<empty>",
            )
        )
    if failures:
        raise NvattestProofError(failures)
    return tuple(
        sorted(entries, key=lambda item: (item[0]["name"], item[0]["version"]))
    )


def support_distribution_entries(paths: Sequence[Path]) -> list[dict[str, Any]]:
    return [dict(entry) for entry, _path in _support_wheel_entries_with_paths(paths)]


def _wheel_distribution_metadata(path: Path) -> Mapping[str, str] | None:
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


def _normalize_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _validate_support_declaration(
    value: Any,
    *,
    expected_support_distributions: Sequence[Mapping[str, Any]],
) -> list[Failure]:
    failures: list[Failure] = []
    expected = [dict(entry) for entry in expected_support_distributions]
    if not isinstance(value, list):
        return [
            _failure(
                "nvattest proof support declaration is invalid",
                expected="canonical support distribution list",
                actual=type(value).__name__,
            )
        ]
    actual = [dict(entry) for entry in value if isinstance(entry, Mapping)]
    if len(actual) != len(value):
        failures.append(
            _failure(
                "nvattest proof support declaration contains non-object entries",
                expected="support distribution objects",
                actual=repr(value),
            )
        )
    required_keys = {"bytes", "filename", "name", "sha256", "version"}
    for entry in actual:
        if set(entry) != required_keys:
            failures.append(
                _failure(
                    "nvattest proof support distribution entry is invalid",
                    expected=", ".join(sorted(required_keys)),
                    actual=", ".join(sorted(str(key) for key in entry)),
                )
            )
            continue
        if (
            not isinstance(entry["name"], str)
            or _normalize_distribution_name(entry["name"]) != entry["name"]
            or entry["name"] not in SUPPORT_DISTRIBUTION_NAMES
        ):
            failures.append(
                _failure(
                    "nvattest proof support distribution name is invalid",
                    expected=", ".join(sorted(SUPPORT_DISTRIBUTION_NAMES)),
                    actual=repr(entry["name"]),
                )
            )
        if (
            not isinstance(entry["filename"], str)
            or Path(entry["filename"]).name != entry["filename"]
            or not entry["filename"].endswith(".whl")
        ):
            failures.append(
                _failure(
                    "nvattest proof support wheel filename is invalid",
                    expected="safe wheel basename",
                    actual=repr(entry["filename"]),
                )
            )
        if not isinstance(entry["version"], str) or not entry["version"]:
            failures.append(
                _failure(
                    "nvattest proof support version is invalid",
                    expected="non-empty version string",
                    actual=repr(entry["version"]),
                )
            )
        if not isinstance(entry["bytes"], int) or entry["bytes"] <= 0:
            failures.append(
                _failure(
                    "nvattest proof support wheel byte count is invalid",
                    expected="positive integer",
                    actual=repr(entry["bytes"]),
                )
            )
        if not isinstance(entry["sha256"], str) or not SHA256_RE.fullmatch(
            entry["sha256"]
        ):
            failures.append(
                _failure(
                    "nvattest proof support wheel sha256 is invalid",
                    expected="lowercase SHA-256",
                    actual=repr(entry["sha256"]),
                )
            )
    names = [str(entry.get("name")) for entry in actual]
    if len(names) != len(set(names)):
        failures.append(
            _failure(
                "nvattest proof support distribution is duplicated",
                expected="unique support distribution names",
                actual=", ".join(names),
            )
        )
    if set(names) != SUPPORT_DISTRIBUTION_NAMES:
        failures.append(
            _failure(
                "nvattest proof support distribution set is invalid",
                expected=", ".join(sorted(SUPPORT_DISTRIBUTION_NAMES)),
                actual=", ".join(sorted(set(names))) or "<empty>",
            )
        )
    canonical = sorted(actual, key=lambda item: (item["name"], item["version"]))
    if actual != canonical:
        failures.append(
            _failure(
                "nvattest proof support declaration is not canonical",
                expected="support distributions sorted by normalized name and version",
                actual=repr(actual),
            )
        )
    if actual != expected:
        failures.append(
            _failure(
                "nvattest proof support declaration is not bound to expected wheels",
                expected=repr(expected),
                actual=repr(actual),
            )
        )
    return failures


def validate_companion_manifest_bytes(
    data: bytes,
    *,
    target_key: str,
    authority_target: Mapping[str, Any],
) -> list[Failure]:
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [
            _failure(
                "nvattest companion manifest is not valid JSON",
                expected="manifest JSON object",
                actual=str(exc),
            )
        ]
    if not isinstance(manifest, Mapping):
        return [
            _failure(
                "nvattest companion manifest is not an object",
                expected="manifest JSON object",
                actual=type(manifest).__name__,
            )
        ]
    return _validate_companion_manifest(manifest, target_key, authority_target)


def _validate_companion_manifest(
    manifest: Mapping[str, Any],
    target_key: str,
    authority_target: Mapping[str, Any],
) -> list[Failure]:
    failures: list[Failure] = []
    source = _section(authority_target, "source")
    artifact = _section(authority_target, "artifact")
    inventory = authority_target.get("inventory")
    if not isinstance(inventory, list):
        return [
            _failure(
                "nvattest authority inventory is invalid",
                expected="inventory list",
                actual=type(inventory).__name__,
            )
        ]
    comparisons = (
        ("schema_version", manifest.get("schema_version"), MANIFEST_SCHEMA_VERSION),
        (
            "release.version",
            _get_path(manifest, ("release", "version")),
            source.get("version"),
        ),
        ("target.id", _get_path(manifest, ("target", "id")), target_key),
        (
            "source.commit",
            _get_path(manifest, ("source", "commit")),
            source.get("fork_commit"),
        ),
        (
            "source.upstream_base_commit",
            _get_path(manifest, ("source", "upstream_base_commit")),
            source.get("upstream_base"),
        ),
        (
            "artifact.name",
            _get_path(manifest, ("artifact", "name")),
            artifact.get("name"),
        ),
        (
            "artifact.size",
            _get_path(manifest, ("artifact", "size")),
            artifact.get("size_bytes"),
        ),
        (
            "artifact.sha256",
            _get_path(manifest, ("artifact", "sha256")),
            artifact.get("sha256"),
        ),
    )
    for label, actual, expected in comparisons:
        if actual != expected:
            failures.append(
                _failure(
                    f"nvattest companion manifest {label} does not match authority",
                    expected=repr(expected),
                    actual=repr(actual),
                )
            )
    members = manifest.get("archive_members")
    if not isinstance(members, list):
        failures.append(
            _failure(
                "nvattest companion manifest archive_members is invalid",
                expected="archive member list",
                actual=type(members).__name__,
            )
        )
        return failures
    expected_members = {
        str(member.get("relpath")): {
            "kind": member.get("kind"),
            "link_target": member.get("symlink_target"),
            "path": member.get("relpath"),
        }
        for member in inventory
        if isinstance(member, Mapping)
    }
    observed_members: dict[str, Mapping[str, Any]] = {}
    for member in members:
        if not isinstance(member, Mapping) or set(member) != {
            "kind",
            "link_target",
            "path",
        }:
            failures.append(
                _failure(
                    "nvattest companion manifest archive member is invalid",
                    expected="kind, link_target, path",
                    actual=repr(member),
                )
            )
            continue
        path = str(member.get("path"))
        if path in observed_members:
            failures.append(
                _failure(
                    "nvattest companion manifest archive member is duplicated",
                    expected="unique archive member paths",
                    actual=path,
                )
            )
        observed_members[path] = member
    if set(observed_members) != set(expected_members) or len(observed_members) != 7:
        failures.append(
            _failure(
                "nvattest companion manifest archive member set does not match authority",
                expected=", ".join(sorted(expected_members)),
                actual=", ".join(sorted(observed_members)) or "<empty>",
            )
        )
    for path, expected in expected_members.items():
        actual = observed_members.get(path)
        if actual is None:
            continue
        # The manifest intentionally has no executable field. The executable bit
        # is authority-only here and is rechecked against the installed tree.
        if {
            "kind": actual.get("kind"),
            "link_target": actual.get("link_target"),
            "path": actual.get("path"),
        } != expected:
            failures.append(
                _failure(
                    "nvattest companion manifest archive member does not match authority",
                    expected=repr(expected),
                    actual=repr(dict(actual)),
                )
            )
    return failures


def _section(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    section = value.get(key)
    return section if isinstance(section, Mapping) else {}


def _get_path(value: Mapping[str, Any], keys: Sequence[str]) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def build_nvattest_proof(
    *,
    target: str,
    version: str,
    source_commit: str,
    core_lock_sha256: str,
    candidate_digest: str,
    ledger_sha256: str,
    challenge: str,
    candidate_dir: Path,
    candidate_paths: Sequence[Path],
    support_distributions: Sequence[Mapping[str, Any]],
    observation: NvattestProofObservation,
    recorded_at: datetime,
    canonical_authority_bytes: bytes | None = None,
) -> dict[str, Any]:
    canonical_bytes = canonical_authority_bytes or _canonical_authority_bytes()
    failures = _validate_build_inputs(
        target=target,
        version=version,
        source_commit=source_commit,
        core_lock_sha256=core_lock_sha256,
        candidate_digest=candidate_digest,
        ledger_sha256=ledger_sha256,
        challenge=challenge,
    )
    try:
        target_key = _target_key_from_policy(target, observation.host)
    except NvattestProofError as exc:
        target_key = None
        failures.extend(exc.failures)
    try:
        canonical_payload = _load_canonical_authority(canonical_bytes)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        canonical_payload = {}
        failures.append(
            _failure(
                "nvattest canonical authority payload is invalid",
                expected="canonical authority JSON payload",
                actual=str(exc),
            )
        )
    if failures:
        raise NvattestProofError(failures)
    assert target_key is not None
    authority_target = _authority_target(canonical_payload, target_key)
    authority_source = _section(authority_target, "source")
    authority_artifact = _section(authority_target, "artifact")
    authority_manifest = _section(authority_target, "companion_manifest")

    failures.extend(
        _validate_observation(
            observation,
            target_key=target_key,
            candidate_paths=candidate_paths,
            support_distributions=support_distributions,
            authority_target=authority_target,
            canonical_authority_bytes=canonical_bytes,
        )
    )
    if failures:
        raise NvattestProofError(failures)

    env_root = observation.env_root
    cache = observation.cache_root
    site_roots = _driver_site_roots(observation.driver.payload)
    proof = {
        "archive_fetch": {
            "sha256": observation.archive_fetch.sha256,
            "size_bytes": observation.archive_fetch.size_bytes,
            "url": observation.archive_fetch.url,
        },
        "cache_install": {
            "cache_root": NVATTEST_CACHE_ROOT,
            "journal_path": _normalize_receipt_path(
                observation.journal_path,
                env_root=env_root,
                candidate_dir=candidate_dir,
                cache_root=cache,
                site_roots=site_roots,
            ),
            "package_driver_command": _command_payload(
                observation.driver.command,
                env_root=env_root,
                candidate_dir=candidate_dir,
                cache_root=cache,
                site_roots=site_roots,
            ),
            "wheel_install_command": _command_payload(
                observation.install,
                env_root=env_root,
                candidate_dir=candidate_dir,
                cache_root=cache,
                site_roots=site_roots,
            ),
        },
        "candidate_digest": candidate_digest,
        "candidate_distributions": [
            dict(entry)
            for entry in sorted(
                observation.candidate_distributions,
                key=lambda item: (item["name"], item["version"]),
            )
        ],
        "challenge": challenge,
        "companion_manifest": {
            "member_count": 7,
            "observed_size_bytes": observation.manifest_fetch.size_bytes,
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "semantic": "load-bearing-fields-match-authority",
            "sha256": observation.manifest_fetch.sha256,
            "target_key": target_key,
        },
        "core_lock_sha256": core_lock_sha256,
        "host": {
            "authority_target_key": target_key,
            "observed_arch": observation.host.arch,
            "observed_os": observation.host.os,
            "policy_arch": TARGET_POLICY[target][1],
            "policy_os": TARGET_POLICY[target][0],
        },
        "installed_authority": {
            "member": NVATTEST_AUTHORITY_MEMBER,
            "path": _normalize_receipt_path(
                observation.installed_authority_path,
                env_root=env_root,
                candidate_dir=candidate_dir,
                cache_root=cache,
                site_roots=site_roots,
            ),
            "sha256": hashlib.sha256(observation.installed_authority_bytes).hexdigest(),
            "size_bytes": len(observation.installed_authority_bytes),
        },
        "installed_package": _installed_package_payload(
            observation.driver.payload,
            env_root=env_root,
            candidate_dir=candidate_dir,
            cache_root=cache,
            site_roots=site_roots,
        ),
        "integrity": _integrity_payload(
            observation.integrity,
            env_root=env_root,
            candidate_dir=candidate_dir,
            cache_root=cache,
            site_roots=site_roots,
        ),
        "kind": NVATTEST_PROOF_KIND,
        "ledger_sha256": ledger_sha256,
        "manifest_fetch": {
            "sha256": observation.manifest_fetch.sha256,
            "size_bytes": observation.manifest_fetch.size_bytes,
            "url": observation.manifest_fetch.url,
        },
        "nvattest": {
            "artifact": {
                "name": authority_artifact.get("name"),
                "sha256": authority_artifact.get("sha256"),
                "size_bytes": authority_artifact.get("size_bytes"),
                "url": authority_artifact.get("url"),
            },
            "companion_manifest": {
                "name": authority_manifest.get("name"),
                "sha256": authority_manifest.get("sha256"),
                "url": authority_manifest.get("url"),
            },
            "source": {
                "fork_commit": authority_source.get("fork_commit"),
                "upstream_base": authority_source.get("upstream_base"),
                "version": authority_source.get("version"),
            },
            "target_key": target_key,
        },
        "recorded_at": _format_utc(recorded_at),
        "smoke": _command_payload(
            observation.smoke,
            env_root=env_root,
            candidate_dir=candidate_dir,
            cache_root=cache,
            site_roots=site_roots,
        ),
        "source_commit": source_commit,
        "support_distributions": [dict(entry) for entry in support_distributions],
        "target": target,
        "version": version,
    }
    if set(proof) != NVATTEST_TOP_LEVEL_KEYS:
        raise AssertionError("nvattest proof key set drifted")
    public_failures = validate_public_evidence_tree("nvattest_proof", proof)
    if public_failures:
        raise NvattestProofError(public_failures)
    validation_failures = validate_nvattest_proof(
        proof,
        expected_challenge=challenge,
        target=target,
        version=version,
        source_commit=source_commit,
        core_lock_sha256=core_lock_sha256,
        candidate_digest=candidate_digest,
        ledger_sha256=ledger_sha256,
        canonical_authority_payload=canonical_payload,
        canonical_authority_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        expected_support_distributions=support_distributions,
    )
    if validation_failures:
        raise NvattestProofError(validation_failures)
    return proof


def _validate_build_inputs(
    *,
    target: str,
    version: str,
    source_commit: str,
    core_lock_sha256: str,
    candidate_digest: str,
    ledger_sha256: str,
    challenge: str,
) -> list[Failure]:
    failures: list[Failure] = []
    if target not in PROOF_TARGETS:
        failures.append(
            _failure(
                "nvattest proof target is invalid",
                expected=", ".join(PROOF_TARGETS),
                actual=target,
            )
        )
    if not version:
        failures.append(
            _failure(
                "nvattest proof version is invalid",
                expected="non-empty version string",
                actual=repr(version),
            )
        )
    if not SOURCE_COMMIT_RE.fullmatch(source_commit):
        failures.append(
            _failure(
                "nvattest proof source commit is invalid",
                expected="40 or 64 lowercase hexadecimal characters",
                actual=source_commit,
            )
        )
    for label, value in (
        ("core_lock_sha256", core_lock_sha256),
        ("candidate_digest", candidate_digest),
        ("ledger_sha256", ledger_sha256),
    ):
        if not SHA256_RE.fullmatch(value):
            failures.append(
                _failure(
                    f"nvattest proof {label} is invalid",
                    expected="lowercase SHA-256",
                    actual=value,
                )
            )
    if not CHALLENGE_RE.fullmatch(challenge):
        failures.append(
            _failure(
                "nvattest proof challenge is invalid",
                expected="64 lowercase hexadecimal characters",
                actual=challenge,
            )
        )
    return failures


def _validate_observation(
    observation: NvattestProofObservation,
    *,
    target_key: str,
    candidate_paths: Sequence[Path],
    support_distributions: Sequence[Mapping[str, Any]],
    authority_target: Mapping[str, Any],
    canonical_authority_bytes: bytes,
) -> list[Failure]:
    failures: list[Failure] = []
    candidate_basenames = {
        str(entry["basename"]) for entry in candidate_file_entries(candidate_paths)
    }
    if len(candidate_basenames) != len(candidate_paths):
        failures.append(
            _failure(
                "nvattest candidate wheel set contains duplicate basenames",
                expected="unique target wheel basenames",
                actual=", ".join(sorted(candidate_basenames)),
            )
        )
    expected_candidate_distributions = {
        (entry["name"], entry["version"])
        for entry in expected_distribution_entries(candidate_paths)
    }
    observed_candidate_distributions = {
        (entry["name"], entry["version"])
        for entry in observation.candidate_distributions
    }
    if observed_candidate_distributions != expected_candidate_distributions:
        failures.append(
            _failure(
                "nvattest candidate distribution set is invalid",
                expected=repr(sorted(expected_candidate_distributions)),
                actual=repr(sorted(observed_candidate_distributions)),
            )
        )
    failures.extend(
        _validate_command_payload(
            "wheel install",
            _command_payload(
                observation.install,
                env_root=observation.env_root,
                candidate_dir=Path("/candidate"),
                cache_root=observation.cache_root,
                site_roots=(),
            ),
        )
    )
    installed_authority_sha = hashlib.sha256(
        observation.installed_authority_bytes
    ).hexdigest()
    canonical_sha = hashlib.sha256(canonical_authority_bytes).hexdigest()
    if installed_authority_sha != canonical_sha:
        failures.append(
            _failure(
                "nvattest installed authority bytes do not match canonical authority",
                expected=canonical_sha,
                actual=installed_authority_sha,
            )
        )
    artifact = _section(authority_target, "artifact")
    companion_manifest = _section(authority_target, "companion_manifest")
    if (
        observation.archive_fetch.url != artifact.get("url")
        or observation.archive_fetch.sha256 != artifact.get("sha256")
        or observation.archive_fetch.size_bytes != artifact.get("size_bytes")
    ):
        failures.append(
            _failure(
                "nvattest archive fetch does not match authority",
                expected=repr(dict(artifact)),
                actual=repr(
                    {
                        "sha256": observation.archive_fetch.sha256,
                        "size_bytes": observation.archive_fetch.size_bytes,
                        "url": observation.archive_fetch.url,
                    }
                ),
            )
        )
    manifest_bytes = observation.manifest_fetch.path.read_bytes()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if (
        observation.manifest_fetch.url != companion_manifest.get("url")
        or observation.manifest_fetch.sha256 != companion_manifest.get("sha256")
        or manifest_sha != companion_manifest.get("sha256")
    ):
        failures.append(
            _failure(
                "nvattest manifest fetch does not match authority",
                expected=repr(dict(companion_manifest)),
                actual=repr(
                    {
                        "computed_sha256": manifest_sha,
                        "sha256": observation.manifest_fetch.sha256,
                        "url": observation.manifest_fetch.url,
                    }
                ),
            )
        )
    failures.extend(
        validate_companion_manifest_bytes(
            manifest_bytes,
            target_key=target_key,
            authority_target=authority_target,
        )
    )
    failures.extend(
        _validate_support_declaration(
            list(support_distributions),
            expected_support_distributions=support_distributions,
        )
    )
    failures.extend(
        _validate_driver_payload(
            observation.driver.payload,
            target_key=target_key,
            env_root=observation.env_root,
            cache=observation.cache_root,
            canonical_authority_sha256=canonical_sha,
        )
    )
    failures.extend(
        _validate_integrity(
            observation.integrity,
            authority_target,
            target_key=target_key,
        )
    )
    if observation.smoke.exit_code != 0:
        failures.append(
            _failure(
                "nvattest smoke command failed",
                expected="exit code 0",
                actual=str(observation.smoke.exit_code),
            )
        )
    return failures


def _validate_driver_payload(
    payload: Mapping[str, Any],
    *,
    target_key: str,
    env_root: Path,
    cache: Path,
    canonical_authority_sha256: str,
) -> list[Failure]:
    failures: list[Failure] = []
    for key in ("module_origin", "module_file", "authority_path", "cache_root"):
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            failures.append(
                _failure(
                    f"nvattest driver {key} is invalid",
                    expected="non-empty string",
                    actual=repr(value),
                )
            )
    origin = Path(str(payload.get("module_origin", ""))).resolve()
    module_file = Path(str(payload.get("module_file", ""))).resolve()
    if not _is_under(origin, env_root.resolve()) or "site-packages" not in origin.parts:
        failures.append(
            _failure(
                "nvattest driver module origin is not under env site packages",
                expected=f"{ENVROOT}/{PYTHON_SITE}/solstone/...",
                actual=str(payload.get("module_origin")),
            )
        )
    if (
        not _is_under(module_file, env_root.resolve())
        or "site-packages" not in module_file.parts
    ):
        failures.append(
            _failure(
                "nvattest driver module file is not under env site packages",
                expected=f"{ENVROOT}/{PYTHON_SITE}/solstone/...",
                actual=str(payload.get("module_file")),
            )
        )
    if (
        payload.get("cache_root")
        and Path(str(payload["cache_root"])).resolve() != cache.resolve()
    ):
        failures.append(
            _failure(
                "nvattest driver cache root is invalid",
                expected=str(cache),
                actual=str(payload.get("cache_root")),
            )
        )
    if payload.get("authority_sha256") != canonical_authority_sha256:
        failures.append(
            _failure(
                "nvattest driver installed authority digest does not match canonical",
                expected=canonical_authority_sha256,
                actual=str(payload.get("authority_sha256")),
            )
        )
    if payload.get("spp_nvattest_dir_present") is not False:
        failures.append(
            _failure(
                f"nvattest driver inherited {SPP_NVATTEST_DIR_ENV}",
                expected=f"{SPP_NVATTEST_DIR_ENV} absent from scrubbed child env",
                actual=repr(payload.get("spp_nvattest_dir_present")),
            )
        )
    if payload.get("solstone_journal_present") is not False:
        failures.append(
            _failure(
                "nvattest driver inherited SOLSTONE_JOURNAL",
                expected="SOLSTONE_JOURNAL absent from child env",
                actual=repr(payload.get("solstone_journal_present")),
            )
        )
    sidecar = payload.get("sidecar")
    if isinstance(sidecar, Mapping) and sidecar.get("target_key") != target_key:
        failures.append(
            _failure(
                "nvattest driver sidecar target is invalid",
                expected=target_key,
                actual=str(sidecar.get("target_key")),
            )
        )
    return failures


def _validate_integrity(
    integrity: Mapping[str, Any],
    authority_target: Mapping[str, Any],
    *,
    target_key: str,
) -> list[Failure]:
    failures: list[Failure] = []
    sidecar = integrity.get("sidecar")
    if not isinstance(sidecar, Mapping):
        return [
            _failure(
                "nvattest integrity sidecar is invalid",
                expected="sidecar object",
                actual=type(sidecar).__name__,
            )
        ]
    sidecar_path = integrity.get("sidecar_path")
    if not isinstance(sidecar_path, str) or not sidecar_path.endswith(SIDECAR_NAME):
        failures.append(
            _failure(
                "nvattest integrity sidecar path is invalid",
                expected=f"sidecar path ending in {SIDECAR_NAME}",
                actual=repr(sidecar_path),
            )
        )
    source = _section(authority_target, "source")
    artifact = _section(authority_target, "artifact")
    if sidecar.get("schema_version") != SIDECAR_SCHEMA_VERSION:
        failures.append(
            _failure(
                "nvattest sidecar schema_version is invalid",
                expected=str(SIDECAR_SCHEMA_VERSION),
                actual=repr(sidecar.get("schema_version")),
            )
        )
    for label, actual, expected in (
        ("target_key", sidecar.get("target_key"), target_key),
        ("version", sidecar.get("version"), source.get("version")),
        ("artifact", sidecar.get("artifact"), dict(artifact)),
        (
            "tree_fingerprint_sha256",
            sidecar.get("tree_fingerprint_sha256"),
            integrity.get("tree_fingerprint_sha256"),
        ),
    ):
        if actual != expected:
            failures.append(
                _failure(
                    f"nvattest sidecar {label} is invalid",
                    expected=repr(expected),
                    actual=repr(actual),
                )
            )
    members = integrity.get("members")
    inventory = authority_target.get("inventory")
    if not isinstance(members, list) or not isinstance(inventory, list):
        failures.append(
            _failure(
                "nvattest installed member facts are invalid",
                expected="member fact list and authority inventory",
                actual=repr(members),
            )
        )
        return failures
    expected_members = {
        str(member.get("relpath")): member
        for member in inventory
        if isinstance(member, Mapping)
    }
    observed_members = {
        str(member.get("relpath")): member
        for member in members
        if isinstance(member, Mapping)
    }
    if set(observed_members) != set(expected_members) or len(observed_members) != 7:
        failures.append(
            _failure(
                "nvattest installed member set does not match authority",
                expected=", ".join(sorted(expected_members)),
                actual=", ".join(sorted(observed_members)) or "<empty>",
            )
        )
    for relpath, expected in expected_members.items():
        actual = observed_members.get(relpath)
        if actual is None:
            continue
        for key in ("kind", "symlink_target", "executable"):
            if actual.get(key) != expected.get(key):
                failures.append(
                    _failure(
                        f"nvattest installed member {relpath} {key} does not match authority",
                        expected=repr(expected.get(key)),
                        actual=repr(actual.get(key)),
                    )
                )
        if actual.get("kind") == "regular" and not (
            isinstance(actual.get("content_sha256"), str)
            and SHA256_RE.fullmatch(str(actual.get("content_sha256")))
        ):
            failures.append(
                _failure(
                    "nvattest installed regular member content hash is invalid",
                    expected="lowercase SHA-256",
                    actual=repr(actual.get("content_sha256")),
                )
            )
    return failures


def _driver_site_roots(payload: Mapping[str, Any]) -> tuple[Path, ...]:
    raw_roots = payload.get("site_packages")
    if not isinstance(raw_roots, list):
        return ()
    return tuple(
        Path(str(root)) for root in raw_roots if isinstance(root, str) and root
    )


def _installed_package_payload(
    driver_payload: Mapping[str, Any],
    *,
    env_root: Path,
    candidate_dir: Path,
    cache_root: Path,
    site_roots: Sequence[Path],
) -> Mapping[str, Any]:
    dist_info = driver_payload.get("dist_info")
    dist_entries = dist_info if isinstance(dist_info, list) else []
    return {
        "authority_module_file": _normalize_receipt_path(
            str(driver_payload.get("authority_module_file", "")),
            env_root=env_root,
            candidate_dir=candidate_dir,
            cache_root=cache_root,
            site_roots=site_roots,
        ),
        "authority_origin": _normalize_receipt_path(
            str(driver_payload.get("authority_origin", "")),
            env_root=env_root,
            candidate_dir=candidate_dir,
            cache_root=cache_root,
            site_roots=site_roots,
        ),
        "dist_info": [
            {
                "dist_info_path": _normalize_receipt_path(
                    str(entry.get("dist_info_path", "")),
                    env_root=env_root,
                    candidate_dir=candidate_dir,
                    cache_root=cache_root,
                    site_roots=site_roots,
                ),
                "name": entry.get("name"),
                "version": entry.get("version"),
            }
            for entry in dist_entries
            if isinstance(entry, Mapping)
        ],
        "module_file": _normalize_receipt_path(
            str(driver_payload.get("module_file", "")),
            env_root=env_root,
            candidate_dir=candidate_dir,
            cache_root=cache_root,
            site_roots=site_roots,
        ),
        "module_origin": _normalize_receipt_path(
            str(driver_payload.get("module_origin", "")),
            env_root=env_root,
            candidate_dir=candidate_dir,
            cache_root=cache_root,
            site_roots=site_roots,
        ),
    }


def _integrity_payload(
    integrity: Mapping[str, Any],
    *,
    env_root: Path,
    candidate_dir: Path,
    cache_root: Path,
    site_roots: Sequence[Path],
) -> Mapping[str, Any]:
    sidecar = integrity.get("sidecar")
    members = integrity.get("members")
    return {
        "members": sorted(
            [dict(member) for member in members if isinstance(member, Mapping)]
            if isinstance(members, list)
            else [],
            key=lambda item: item["relpath"],
        ),
        "sidecar": dict(sidecar) if isinstance(sidecar, Mapping) else {},
        "sidecar_path": _normalize_receipt_path(
            str(integrity.get("sidecar_path", "")),
            env_root=env_root,
            candidate_dir=candidate_dir,
            cache_root=cache_root,
            site_roots=site_roots,
        ),
        "sidecar_sha256": str(integrity.get("sidecar_sha256", "")),
        "sidecar_size_bytes": integrity.get("sidecar_size_bytes"),
        "tree_fingerprint_sha256": str(integrity.get("tree_fingerprint_sha256", "")),
    }


def _command_payload(
    result: CommandResult,
    *,
    env_root: Path,
    candidate_dir: Path,
    cache_root: Path,
    site_roots: Sequence[Path],
) -> dict[str, Any]:
    return {
        "argv": [
            _normalize_receipt_path(
                token,
                env_root=env_root,
                candidate_dir=candidate_dir,
                cache_root=cache_root,
                site_roots=site_roots,
            )
            for token in result.argv
        ],
        "env": dict(result.env),
        "exit_code": result.exit_code,
        "stderr": _normalize_receipt_text(
            result.stderr,
            env_root=env_root,
            candidate_dir=candidate_dir,
            cache_root=cache_root,
            site_roots=site_roots,
        ),
        "stdout": _normalize_receipt_text(
            result.stdout,
            env_root=env_root,
            candidate_dir=candidate_dir,
            cache_root=cache_root,
            site_roots=site_roots,
        ),
    }


def _normalize_receipt_text(
    value: str,
    *,
    env_root: Path,
    candidate_dir: Path,
    cache_root: Path,
    site_roots: Sequence[Path],
) -> str:
    prefixes = _normalization_prefixes(env_root, candidate_dir, cache_root, site_roots)
    prefix_spellings = dict(
        sorted(prefixes, key=lambda item: len(item[0]), reverse=True)
    )
    escaped_prefixes = "|".join(re.escape(raw) for raw in prefix_spellings)
    # The lookahead allowlist fails closed: unknown following characters leave the
    # absolute path in place so public-evidence validation rejects loudly.
    pattern = re.compile(f"(?:{escaped_prefixes})(?=[/\\s\"'),:;]|$)")
    return pattern.sub(lambda match: prefix_spellings[match.group(0)], value)


def _normalize_receipt_path(
    value: Path | str,
    *,
    env_root: Path,
    candidate_dir: Path,
    cache_root: Path,
    site_roots: Sequence[Path],
) -> str:
    text = str(value)
    if not text:
        return text
    prefixes = _normalization_prefixes(env_root, candidate_dir, cache_root, site_roots)
    for raw, sentinel in sorted(
        prefixes,
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if text == raw:
            return sentinel
        if text.startswith(f"{raw}/"):
            return f"{sentinel}/{text[len(raw) + 1 :]}"
    raw_path = Path(text)
    if text.endswith(".whl"):
        return f"{SUPPORT}/{raw_path.name}"
    if text.endswith(".py") and "solstone-nvattest-driver-" in text:
        return DRIVER
    return text


def _normalization_prefixes(
    env_root: Path,
    candidate_dir: Path,
    cache_root: Path,
    site_roots: Sequence[Path],
) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[Path, str]] = [
        (cache_root, NVATTEST_CACHE_ROOT),
        (candidate_dir, CANDIDATE),
        (env_root, ENVROOT),
    ]
    entries.extend((root, f"{ENVROOT}/{PYTHON_SITE}") for root in site_roots)
    raw: set[tuple[str, str]] = set()
    for path, sentinel in entries:
        for variant in {path, path.resolve()}:
            raw.add((variant.as_posix(), sentinel))
    return tuple(raw)


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
                f"nvattest proof {label} command payload is invalid",
                expected="argv, env, exit_code, stdout, stderr",
                actual=repr(value),
            )
        ]
    argv = value.get("argv")
    if not isinstance(argv, list) or not all(isinstance(token, str) for token in argv):
        failures.append(
            _failure(
                f"nvattest proof {label} argv is invalid",
                expected="list of normalized string tokens",
                actual=repr(argv),
            )
        )
    else:
        forbidden = _forbidden_command_tokens(argv)
        if forbidden:
            failures.append(
                _failure(
                    f"nvattest proof {label} command contains forbidden resolver option",
                    expected=", ".join(sorted(FORBIDDEN_INSTALL_TOKENS)),
                    actual=", ".join(forbidden),
                )
            )
    env = value.get("env")
    if not isinstance(env, Mapping):
        failures.append(
            _failure(
                f"nvattest proof {label} env is invalid",
                expected="scrubbed environment object",
                actual=type(env).__name__,
            )
        )
    else:
        failures.extend(_env_failures(f"nvattest proof {label}", env))
    if not isinstance(value.get("exit_code"), int):
        failures.append(
            _failure(
                f"nvattest proof {label} exit code is invalid",
                expected="integer",
                actual=repr(value.get("exit_code")),
            )
        )
    for stream in ("stdout", "stderr"):
        if not isinstance(value.get(stream), str):
            failures.append(
                _failure(
                    f"nvattest proof {label} {stream} is invalid",
                    expected="string",
                    actual=repr(value.get(stream)),
                )
            )
    return failures


def validate_nvattest_proof_bytes(
    data: bytes,
    *,
    expected_challenge: str,
    target: str,
    version: str,
    source_commit: str,
    core_lock_sha256: str,
    candidate_digest: str,
    ledger_sha256: str,
    canonical_authority_bytes: bytes,
    expected_support_distributions: Sequence[Mapping[str, Any]],
) -> list[Failure]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [
            _failure(
                "nvattest proof is not valid JSON",
                expected="canonical JSON object",
                actual=str(exc),
            )
        ]
    if not isinstance(payload, Mapping):
        return [
            _failure(
                "nvattest proof is not an object",
                expected="JSON object",
                actual=type(payload).__name__,
            )
        ]
    failures: list[Failure] = []
    if data != canonical_json_bytes(payload):
        failures.append(
            _failure(
                "nvattest proof bytes are not canonical",
                expected="canonical sorted-key JSON bytes",
                actual="non-canonical JSON",
            )
        )
    try:
        canonical_payload = _load_canonical_authority(canonical_authority_bytes)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return [
            _failure(
                "nvattest canonical authority bytes are invalid",
                expected="canonical nvattest authority JSON",
                actual=str(exc),
            )
        ]
    failures.extend(
        validate_nvattest_proof(
            payload,
            expected_challenge=expected_challenge,
            target=target,
            version=version,
            source_commit=source_commit,
            core_lock_sha256=core_lock_sha256,
            candidate_digest=candidate_digest,
            ledger_sha256=ledger_sha256,
            canonical_authority_payload=canonical_payload,
            canonical_authority_sha256=hashlib.sha256(
                canonical_authority_bytes
            ).hexdigest(),
            expected_support_distributions=expected_support_distributions,
        )
    )
    return failures


def validate_nvattest_proof(
    proof: Mapping[str, Any],
    *,
    expected_challenge: str,
    target: str,
    version: str,
    source_commit: str,
    core_lock_sha256: str,
    candidate_digest: str,
    ledger_sha256: str,
    canonical_authority_payload: Mapping[str, Any],
    canonical_authority_sha256: str,
    expected_support_distributions: Sequence[Mapping[str, Any]],
) -> list[Failure]:
    failures: list[Failure] = []
    if set(proof) != NVATTEST_TOP_LEVEL_KEYS:
        failures.append(
            _failure(
                "nvattest proof key set is invalid",
                expected=", ".join(sorted(NVATTEST_TOP_LEVEL_KEYS)),
                actual=", ".join(sorted(str(key) for key in proof)),
            )
        )
    if proof.get("kind") != NVATTEST_PROOF_KIND:
        failures.append(
            _failure(
                "nvattest proof kind is invalid",
                expected=NVATTEST_PROOF_KIND,
                actual=str(proof.get("kind")),
            )
        )
    for key, expected in (
        ("challenge", expected_challenge),
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
                    f"nvattest proof {key} is not bound to expected input",
                    expected=str(expected),
                    actual=str(proof.get(key)),
                )
            )
    failures.extend(
        _validate_build_inputs(
            target=str(proof.get("target", "")),
            version=str(proof.get("version", "")),
            source_commit=str(proof.get("source_commit", "")),
            core_lock_sha256=str(proof.get("core_lock_sha256", "")),
            candidate_digest=str(proof.get("candidate_digest", "")),
            ledger_sha256=str(proof.get("ledger_sha256", "")),
            challenge=str(proof.get("challenge", "")),
        )
    )
    recorded_at = proof.get("recorded_at")
    if not isinstance(recorded_at, str) or not recorded_at.endswith("Z"):
        failures.append(
            _failure(
                "nvattest proof recorded_at is invalid",
                expected="UTC RFC3339 timestamp",
                actual=repr(recorded_at),
            )
        )
    host = proof.get("host")
    target_key = _target_key_from_proof_host(host, target, failures)
    if target_key is not None:
        authority_target = _authority_target(canonical_authority_payload, target_key)
        failures.extend(
            _validate_authority_bound_sections(
                proof,
                target_key=target_key,
                authority_target=authority_target,
                canonical_authority_sha256=canonical_authority_sha256,
            )
        )
    failures.extend(
        _validate_support_declaration(
            proof.get("support_distributions"),
            expected_support_distributions=expected_support_distributions,
        )
    )
    failures.extend(
        _validate_candidate_distributions(proof.get("candidate_distributions"))
    )
    failures.extend(_validate_installed_package(proof.get("installed_package")))
    cache_install = proof.get("cache_install")
    if not isinstance(cache_install, Mapping) or set(cache_install) != {
        "cache_root",
        "journal_path",
        "package_driver_command",
        "wheel_install_command",
    }:
        failures.append(
            _failure(
                "nvattest proof cache install section is invalid",
                expected=(
                    "cache_root, journal_path, package_driver_command, "
                    "wheel_install_command"
                ),
                actual=repr(cache_install),
            )
        )
        cache_install = {}
    else:
        if cache_install.get("cache_root") != NVATTEST_CACHE_ROOT:
            failures.append(
                _failure(
                    "nvattest proof cache root sentinel is invalid",
                    expected=NVATTEST_CACHE_ROOT,
                    actual=repr(cache_install.get("cache_root")),
                )
            )
        journal_path = cache_install.get("journal_path")
        if not (
            isinstance(journal_path, str) and journal_path.startswith(f"{ENVROOT}/")
        ):
            failures.append(
                _failure(
                    "nvattest proof journal path is invalid",
                    expected="normalized ENVROOT journal path",
                    actual=repr(journal_path),
                )
            )
        failures.extend(
            _validate_command_payload(
                "package driver",
                cache_install.get("package_driver_command"),
            )
        )
        failures.extend(
            _validate_command_payload(
                "wheel install",
                cache_install.get("wheel_install_command"),
            )
        )
        failures.extend(
            _validate_cache_install_policy(
                cache_install,
                expected_support_distributions=expected_support_distributions,
            )
        )
    failures.extend(_validate_command_payload("smoke", proof.get("smoke")))
    failures.extend(validate_public_evidence_tree("nvattest_proof", proof))
    return failures


def _validate_candidate_distributions(value: Any) -> list[Failure]:
    failures: list[Failure] = []
    if not isinstance(value, list):
        return [
            _failure(
                "nvattest proof candidate distributions are invalid",
                expected="list of Solstone distribution entries",
                actual=type(value).__name__,
            )
        ]
    seen: set[tuple[str, str]] = set()
    valid_entries: list[Mapping[str, Any]] = []
    for entry in value:
        if not isinstance(entry, Mapping) or set(entry) != {"name", "version"}:
            failures.append(
                _failure(
                    "nvattest proof candidate distribution entry is invalid",
                    expected="name, version",
                    actual=repr(entry),
                )
            )
            continue
        valid_entries.append(entry)
        name = entry.get("name")
        version = entry.get("version")
        if (
            not isinstance(name, str)
            or not name.lower().replace("_", "-").startswith("solstone")
            or not isinstance(version, str)
            or not version
        ):
            failures.append(
                _failure(
                    "nvattest proof candidate distribution scalar is invalid",
                    expected="Solstone-prefixed name and non-empty version",
                    actual=repr(entry),
                )
            )
            continue
        pair = (name, version)
        if pair in seen:
            failures.append(
                _failure(
                    "nvattest proof candidate distribution is duplicated",
                    expected="unique Solstone distribution entries",
                    actual=f"{name}=={version}",
                )
            )
        seen.add(pair)
    canonical = sorted(valid_entries, key=lambda item: (item["name"], item["version"]))
    if len(valid_entries) == len(value) and value != canonical:
        failures.append(
            _failure(
                "nvattest proof candidate distributions are not canonical",
                expected="sorted by name and version",
                actual=repr(value),
            )
        )
    return failures


def _validate_installed_package(value: Any) -> list[Failure]:
    failures: list[Failure] = []
    required = {
        "authority_module_file",
        "authority_origin",
        "dist_info",
        "module_file",
        "module_origin",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        return [
            _failure(
                "nvattest proof installed package section is invalid",
                expected=", ".join(sorted(required)),
                actual=repr(value),
            )
        ]
    for key in (
        "authority_module_file",
        "authority_origin",
        "module_file",
        "module_origin",
    ):
        path = value.get(key)
        if not (
            isinstance(path, str)
            and path.startswith(f"{ENVROOT}/{PYTHON_SITE}/solstone/")
            and "site-packages" not in path
        ):
            failures.append(
                _failure(
                    f"nvattest proof installed package {key} is invalid",
                    expected=f"{ENVROOT}/{PYTHON_SITE}/solstone/... without site-packages",
                    actual=repr(path),
                )
            )
    dist_info = value.get("dist_info")
    if not isinstance(dist_info, list):
        failures.append(
            _failure(
                "nvattest proof installed package dist-info is invalid",
                expected="list of Solstone dist-info observations",
                actual=type(dist_info).__name__,
            )
        )
        return failures
    for entry in dist_info:
        if not isinstance(entry, Mapping) or set(entry) != {
            "dist_info_path",
            "name",
            "version",
        }:
            failures.append(
                _failure(
                    "nvattest proof installed package dist-info entry is invalid",
                    expected="dist_info_path, name, version",
                    actual=repr(entry),
                )
            )
            continue
        path = entry.get("dist_info_path")
        if not (
            isinstance(path, str)
            and path.startswith(f"{ENVROOT}/{PYTHON_SITE}/")
            and "site-packages" not in path
        ):
            failures.append(
                _failure(
                    "nvattest proof installed package dist-info path is invalid",
                    expected=f"{ENVROOT}/{PYTHON_SITE}/... without site-packages",
                    actual=repr(path),
                )
            )
    return failures


def _validate_cache_install_policy(
    cache_install: Mapping[str, Any],
    *,
    expected_support_distributions: Sequence[Mapping[str, Any]],
) -> list[Failure]:
    failures: list[Failure] = []
    wheel_command = cache_install.get("wheel_install_command")
    wheel_argv = (
        wheel_command.get("argv") if isinstance(wheel_command, Mapping) else None
    )
    if isinstance(wheel_argv, list):
        expected_support_tokens = [
            f"{SUPPORT}/{entry['filename']}" for entry in expected_support_distributions
        ]
        if wheel_argv[:5] != [
            f"{ENVROOT}/bin/python",
            "-m",
            "pip",
            "install",
            "--no-index",
        ]:
            failures.append(
                _failure(
                    "nvattest proof wheel install command prefix is invalid",
                    expected="ENVROOT python -m pip install --no-index",
                    actual=repr(wheel_argv),
                )
            )
        if "--no-deps" not in wheel_argv:
            failures.append(
                _failure(
                    "nvattest proof wheel install command allows dependency resolution",
                    expected="--no-deps",
                    actual=repr(wheel_argv),
                )
            )
        if wheel_argv[-len(expected_support_tokens) :] != expected_support_tokens:
            failures.append(
                _failure(
                    "nvattest proof support wheel argv order is invalid",
                    expected=repr(expected_support_tokens),
                    actual=repr(wheel_argv),
                )
            )
    package_command = cache_install.get("package_driver_command")
    package_argv = (
        package_command.get("argv") if isinstance(package_command, Mapping) else None
    )
    if isinstance(package_argv, list) and (
        package_argv[:2] != [f"{ENVROOT}/bin/python", DRIVER] or "-c" in package_argv
    ):
        failures.append(
            _failure(
                "nvattest proof package driver command is invalid",
                expected="ENVROOT python executing temp driver script, never -c",
                actual=repr(package_argv),
            )
        )
    for label, command in (
        ("wheel install", wheel_command),
        ("package driver", package_command),
    ):
        env = command.get("env") if isinstance(command, Mapping) else None
        if isinstance(env, Mapping) and dict(env) != dict(SCRUBBED_COMMAND_ENV):
            failures.append(
                _failure(
                    f"nvattest proof {label} env is not scrubbed",
                    expected=repr(dict(SCRUBBED_COMMAND_ENV)),
                    actual=repr(dict(env)),
                )
            )
    return failures


def _target_key_from_proof_host(
    host: Any,
    target: str,
    failures: list[Failure],
) -> str | None:
    if not isinstance(host, Mapping) or set(host) != {
        "authority_target_key",
        "observed_arch",
        "observed_os",
        "policy_arch",
        "policy_os",
    }:
        failures.append(
            _failure(
                "nvattest proof host section is invalid",
                expected="authority_target_key, observed_arch, observed_os, policy_arch, policy_os",
                actual=repr(host),
            )
        )
        return None
    if target not in TARGET_POLICY:
        return None
    policy_os, policy_arch = TARGET_POLICY[target]
    expected_key = nvattest_target_key(os_name=policy_os.lower(), arch=policy_arch)
    observed_key = nvattest_target_key(
        os_name=str(host.get("observed_os", "")).lower(),
        arch=str(host.get("observed_arch", "")),
    )
    if (
        host.get("policy_os") != policy_os
        or host.get("policy_arch") != policy_arch
        or host.get("authority_target_key") != expected_key
        or observed_key != expected_key
    ):
        failures.append(
            _failure(
                "nvattest proof host binding is invalid",
                expected=f"{policy_os}/{policy_arch}/{expected_key}",
                actual=repr(dict(host)),
            )
        )
        return None
    return cast(str, expected_key)


def _validate_authority_bound_sections(
    proof: Mapping[str, Any],
    *,
    target_key: str,
    authority_target: Mapping[str, Any],
    canonical_authority_sha256: str,
) -> list[Failure]:
    failures: list[Failure] = []
    source = _section(authority_target, "source")
    artifact = _section(authority_target, "artifact")
    companion_manifest = _section(authority_target, "companion_manifest")
    nvattest = proof.get("nvattest")
    expected_nvattest = {
        "artifact": {
            "name": artifact.get("name"),
            "sha256": artifact.get("sha256"),
            "size_bytes": artifact.get("size_bytes"),
            "url": artifact.get("url"),
        },
        "companion_manifest": {
            "name": companion_manifest.get("name"),
            "sha256": companion_manifest.get("sha256"),
            "url": companion_manifest.get("url"),
        },
        "source": {
            "fork_commit": source.get("fork_commit"),
            "upstream_base": source.get("upstream_base"),
            "version": source.get("version"),
        },
        "target_key": target_key,
    }
    if nvattest != expected_nvattest:
        failures.append(
            _failure(
                "nvattest proof authority identity section is invalid",
                expected=repr(expected_nvattest),
                actual=repr(nvattest),
            )
        )
    archive_fetch = proof.get("archive_fetch")
    if archive_fetch != {
        "sha256": artifact.get("sha256"),
        "size_bytes": artifact.get("size_bytes"),
        "url": artifact.get("url"),
    }:
        failures.append(
            _failure(
                "nvattest proof archive fetch is not bound to authority",
                expected=repr(artifact),
                actual=repr(archive_fetch),
            )
        )
    manifest_fetch = proof.get("manifest_fetch")
    if not isinstance(manifest_fetch, Mapping) or (
        manifest_fetch.get("sha256") != companion_manifest.get("sha256")
        or manifest_fetch.get("url") != companion_manifest.get("url")
    ):
        failures.append(
            _failure(
                "nvattest proof manifest fetch is not bound to authority",
                expected=repr(companion_manifest),
                actual=repr(manifest_fetch),
            )
        )
    installed_authority = proof.get("installed_authority")
    if not isinstance(installed_authority, Mapping) or (
        installed_authority.get("member") != NVATTEST_AUTHORITY_MEMBER
        or installed_authority.get("sha256") != canonical_authority_sha256
    ):
        failures.append(
            _failure(
                "nvattest proof installed authority is not canonical",
                expected=f"{NVATTEST_AUTHORITY_MEMBER} {canonical_authority_sha256}",
                actual=repr(installed_authority),
            )
        )
    companion = proof.get("companion_manifest")
    if not isinstance(companion, Mapping) or companion != {
        "member_count": 7,
        "observed_size_bytes": (
            manifest_fetch.get("size_bytes")
            if isinstance(manifest_fetch, Mapping)
            else None
        ),
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "semantic": "load-bearing-fields-match-authority",
        "sha256": companion_manifest.get("sha256"),
        "target_key": target_key,
    }:
        failures.append(
            _failure(
                "nvattest proof companion manifest summary is invalid",
                expected="schema v2 load-bearing summary bound to authority",
                actual=repr(companion),
            )
        )
    smoke = proof.get("smoke")
    expected_smoke_argv = [
        f"{NVATTEST_CACHE_ROOT}/{_nvattest_bin_relpath(authority_target).as_posix()}",
        "--help",
    ]
    if not isinstance(smoke, Mapping) or (
        smoke.get("argv") != expected_smoke_argv or smoke.get("exit_code") != 0
    ):
        failures.append(
            _failure(
                "nvattest proof smoke command is invalid",
                expected=repr({"argv": expected_smoke_argv, "exit_code": 0}),
                actual=repr(smoke),
            )
        )
    failures.extend(
        _validate_integrity(
            proof.get("integrity", {}),
            authority_target,
            target_key=target_key,
        )
    )
    return failures


def write_nvattest_proof(
    path: Path,
    proof: Mapping[str, Any],
    *,
    expected_challenge: str,
    target: str,
    version: str,
    source_commit: str,
    core_lock_sha256: str,
    candidate_digest: str,
    ledger_sha256: str,
    canonical_authority_bytes: bytes,
    expected_support_distributions: Sequence[Mapping[str, Any]],
) -> Path:
    failures = validate_nvattest_proof_bytes(
        canonical_json_bytes(proof),
        expected_challenge=expected_challenge,
        target=target,
        version=version,
        source_commit=source_commit,
        core_lock_sha256=core_lock_sha256,
        candidate_digest=candidate_digest,
        ledger_sha256=ledger_sha256,
        canonical_authority_bytes=canonical_authority_bytes,
        expected_support_distributions=expected_support_distributions,
    )
    if failures:
        raise NvattestProofError(failures)
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
    readback_failures = validate_nvattest_proof_bytes(
        path.read_bytes(),
        expected_challenge=expected_challenge,
        target=target,
        version=version,
        source_commit=source_commit,
        core_lock_sha256=core_lock_sha256,
        candidate_digest=candidate_digest,
        ledger_sha256=ledger_sha256,
        canonical_authority_bytes=canonical_authority_bytes,
        expected_support_distributions=expected_support_distributions,
    )
    if readback_failures:
        raise NvattestProofError(readback_failures)
    return path


def run_nvattest_proof(
    *,
    target: str,
    version: str,
    source_commit: str,
    core_lock_sha256: str,
    candidate_digest: str,
    ledger_sha256: str,
    challenge: str,
    candidate_dir: Path,
    candidate_paths: Sequence[Path],
    support_wheel_paths: Sequence[Path],
    output_path: Path,
    services: NvattestProofServices | None = None,
    canonical_authority_bytes: bytes | None = None,
) -> Path:
    resolved = services or default_services()
    canonical_bytes = canonical_authority_bytes or _canonical_authority_bytes()
    host = resolved.observe_host()
    target_key = _target_key_from_policy(target, host)
    support_distributions = support_distribution_entries(support_wheel_paths)
    canonical_payload = _load_canonical_authority(canonical_bytes)
    authority_target = _authority_target(canonical_payload, target_key)
    artifact = _section(authority_target, "artifact")
    companion_manifest = _section(authority_target, "companion_manifest")
    env_root: Path | None = None
    driver_path: Path | None = None
    primary_failure: NvattestProofError | None = None
    try:
        env_root = _call_stage(
            "environment",
            lambda: resolved.create_environment(target),
        )
        env_python = _env_python(env_root)
        journal_path = env_root / "journal"
        cache = cache_root(journal_path)
        install_result = _call_stage(
            "wheel-install",
            lambda: resolved.install_wheels(
                env_python,
                candidate_paths,
                support_wheel_paths,
            ),
        )
        candidate_distributions = _solstone_distributions(env_python)
        authority_path, authority_bytes = _call_stage(
            "wheel-install",
            lambda: _read_installed_authority(env_root),
        )
        proof_root = env_root / "proof-fetches"
        archive_fetch = _call_stage(
            "archive-fetch",
            lambda: resolved.fetch(
                "archive",
                str(artifact["url"]),
                proof_root / str(artifact["name"]),
            ),
        )
        manifest_fetch = _call_stage(
            "manifest-fetch",
            lambda: resolved.fetch(
                "manifest",
                str(companion_manifest["url"]),
                proof_root / str(companion_manifest["name"]),
            ),
        )
        driver_dir = _call_stage(
            "cache-install",
            lambda: Path(tempfile.mkdtemp(prefix="solstone-nvattest-driver-")),
        )
        driver_path = _call_stage("cache-install", lambda: _write_driver(driver_dir))
        driver = _call_stage(
            "cache-install",
            lambda: resolved.run_package_install(
                env_python,
                driver_path,
                target_key,
                journal_path,
            ),
        )
        integrity = _call_stage(
            "integrity",
            lambda: resolved.integrity_recheck(
                journal_path,
                target_key,
                (archive_fetch, manifest_fetch),
                driver,
            ),
        )
        nvattest_bin = cache / _nvattest_bin_relpath(authority_target)
        smoke = _call_stage("smoke", lambda: resolved.run_smoke(nvattest_bin))
        observation = NvattestProofObservation(
            env_root=env_root,
            journal_path=journal_path,
            cache_root=cache,
            host=host,
            install=install_result,
            candidate_distributions=candidate_distributions,
            archive_fetch=archive_fetch,
            manifest_fetch=manifest_fetch,
            installed_authority_path=authority_path,
            installed_authority_bytes=authority_bytes,
            driver=driver,
            integrity=integrity,
            smoke=smoke,
        )
        proof = build_nvattest_proof(
            target=target,
            version=version,
            source_commit=source_commit,
            core_lock_sha256=core_lock_sha256,
            candidate_digest=candidate_digest,
            ledger_sha256=ledger_sha256,
            challenge=challenge,
            candidate_dir=candidate_dir,
            candidate_paths=candidate_paths,
            support_distributions=support_distributions,
            observation=observation,
            recorded_at=resolved.clock(),
            canonical_authority_bytes=canonical_bytes,
        )
        return _call_stage(
            "receipt-write",
            lambda: write_nvattest_proof(
                output_path,
                proof,
                expected_challenge=challenge,
                target=target,
                version=version,
                source_commit=source_commit,
                core_lock_sha256=core_lock_sha256,
                candidate_digest=candidate_digest,
                ledger_sha256=ledger_sha256,
                canonical_authority_bytes=canonical_bytes,
                expected_support_distributions=support_distributions,
            ),
        )
    except NvattestProofError as exc:
        primary_failure = exc
        raise
    finally:
        cleanup_failures: list[Failure] = []
        if driver_path is not None:
            try:
                shutil.rmtree(driver_path.parent)
            except Exception as exc:
                cleanup_failures.append(
                    _failure(
                        "cleanup",
                        expected="temporary driver directory removed",
                        actual=str(exc),
                    )
                )
        if env_root is not None:
            try:
                resolved.cleanup(env_root)
            except Exception as exc:
                cleanup_failures.append(
                    _failure(
                        "cleanup",
                        expected="proof environment removed",
                        actual=str(exc),
                    )
                )
        if cleanup_failures and primary_failure is None:
            raise NvattestProofError(cleanup_failures)
        if cleanup_failures and primary_failure is not None:
            raise NvattestProofError((*primary_failure.failures, *cleanup_failures))


def _nvattest_bin_relpath(authority_target: Mapping[str, Any]) -> Path:
    inventory = authority_target.get("inventory")
    if not isinstance(inventory, list):
        raise NvattestProofError(
            [
                _failure(
                    "nvattest authority inventory is invalid",
                    expected="inventory list",
                    actual=type(inventory).__name__,
                )
            ]
        )
    candidates = [
        str(member["relpath"])
        for member in inventory
        if isinstance(member, Mapping)
        and member.get("kind") == "regular"
        and member.get("executable") is True
    ]
    if len(candidates) != 1:
        raise NvattestProofError(
            [
                _failure(
                    "nvattest executable inventory member is invalid",
                    expected="one executable regular authority member",
                    actual=", ".join(candidates) or "<missing>",
                )
            ]
        )
    return Path(candidates[0])


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, choices=PROOF_TARGETS)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--core-lock-sha256", required=True)
    parser.add_argument("--candidate-digest", required=True)
    parser.add_argument("--ledger-sha256", required=True)
    parser.add_argument("--challenge", required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--candidate-wheel", type=Path, action="append", required=True)
    parser.add_argument("--support-wheel", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        run_nvattest_proof(
            target=args.target,
            version=args.version,
            source_commit=args.source_commit,
            core_lock_sha256=args.core_lock_sha256,
            candidate_digest=args.candidate_digest,
            ledger_sha256=args.ledger_sha256,
            challenge=args.challenge,
            candidate_dir=args.candidate_dir,
            candidate_paths=args.candidate_wheel,
            support_wheel_paths=args.support_wheel,
            output_path=args.output,
        )
    except NvattestProofError as exc:
        for failure in exc.failures:
            print(
                f"{failure.error}: expected {failure.expected}; actual {failure.actual}",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
