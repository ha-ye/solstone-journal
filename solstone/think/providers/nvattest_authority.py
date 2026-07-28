# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Canonical nvattest release authority and platform resolution."""

from __future__ import annotations

import platform
import re
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping, cast

SCHEMA_VERSION = 1
NVATTEST_VERSION = "1.2.2-sol.2"
NVATTEST_FORK_COMMIT = "1c95b1f5ae72b5f4282f7b6b96722f7c8d69f744"
NVATTEST_UPSTREAM_BASE = "73c032ebff680ca6d2ba06f4006b511491b71ce9"
NVATTEST_URL_PREFIX = "https://updates.solstone.app/providers/nvattest/"

NvattestTargetKey = Literal["linux-x86_64", "linux-aarch64", "macos-arm64"]
NvattestMemberKind = Literal["regular", "symlink"]

TARGET_KEYS: tuple[NvattestTargetKey, ...] = (
    "linux-x86_64",
    "linux-aarch64",
    "macos-arm64",
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class NvattestSourceIdentity:
    version: str
    fork_commit: str
    upstream_base: str
    url_prefix: str

    def to_payload(self) -> dict[str, str]:
        return {
            "fork_commit": self.fork_commit,
            "upstream_base": self.upstream_base,
            "url_prefix": self.url_prefix,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class NvattestArtifactIdentity:
    name: str
    url: str
    size_bytes: int
    sha256: str

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "url": self.url,
        }


@dataclass(frozen=True, slots=True)
class NvattestManifestIdentity:
    name: str
    url: str
    sha256: str

    def to_payload(self) -> dict[str, str]:
        return {
            "name": self.name,
            "sha256": self.sha256,
            "url": self.url,
        }


@dataclass(frozen=True, slots=True)
class NvattestInventoryMember:
    kind: NvattestMemberKind
    relpath: str
    symlink_target: str | None
    executable: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "executable": self.executable,
            "kind": self.kind,
            "relpath": self.relpath,
            "symlink_target": self.symlink_target,
        }


@dataclass(frozen=True, slots=True)
class NvattestTargetEntry:
    key: NvattestTargetKey
    source: NvattestSourceIdentity
    artifact: NvattestArtifactIdentity
    companion_manifest: NvattestManifestIdentity
    inventory: tuple[NvattestInventoryMember, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "artifact": self.artifact.to_payload(),
            "companion_manifest": self.companion_manifest.to_payload(),
            "inventory": [member.to_payload() for member in self.inventory],
            "source": self.source.to_payload(),
        }


SOURCE_IDENTITY = NvattestSourceIdentity(
    version=NVATTEST_VERSION,
    fork_commit=NVATTEST_FORK_COMMIT,
    upstream_base=NVATTEST_UPSTREAM_BASE,
    url_prefix=NVATTEST_URL_PREFIX,
)


def _artifact(name: str, *, size_bytes: int, sha256: str) -> NvattestArtifactIdentity:
    return NvattestArtifactIdentity(
        name=name,
        url=f"{NVATTEST_URL_PREFIX}{name}",
        size_bytes=size_bytes,
        sha256=sha256,
    )


def _manifest(name: str, *, sha256: str) -> NvattestManifestIdentity:
    return NvattestManifestIdentity(
        name=name,
        url=f"{NVATTEST_URL_PREFIX}{name}",
        sha256=sha256,
    )


def _regular(relpath: str, *, executable: bool) -> NvattestInventoryMember:
    return NvattestInventoryMember(
        kind="regular",
        relpath=relpath,
        symlink_target=None,
        executable=executable,
    )


def _symlink(relpath: str, target: str) -> NvattestInventoryMember:
    return NvattestInventoryMember(
        kind="symlink",
        relpath=relpath,
        symlink_target=target,
        executable=False,
    )


def _linux_inventory() -> tuple[NvattestInventoryMember, ...]:
    return (
        _regular("bin/nvattest", executable=True),
        _regular("lib/libnvat.so.1.2.2", executable=True),
        _symlink("lib/libnvat.so.1", "libnvat.so.1.2.2"),
        _symlink("lib/libnvat.so", "libnvat.so.1"),
        _regular("LICENSE", executable=False),
        _regular("share/ca/ca-bundle.pem", executable=False),
        _regular("share/THIRD_PARTY_NOTICES.md", executable=False),
    )


MACOS_INVENTORY: tuple[NvattestInventoryMember, ...] = (
    _regular("bin/nvattest", executable=True),
    _regular("lib/libnvat.1.2.2.dylib", executable=True),
    _symlink("lib/libnvat.1.dylib", "libnvat.1.2.2.dylib"),
    _symlink("lib/libnvat.dylib", "libnvat.1.dylib"),
    _regular("LICENSE", executable=False),
    _regular("share/ca/ca-bundle.pem", executable=False),
    _regular("share/THIRD_PARTY_NOTICES.md", executable=False),
)

LINUX_INVENTORY = _linux_inventory()

NVATTEST_AUTHORITY: Mapping[NvattestTargetKey, NvattestTargetEntry] = {
    "linux-x86_64": NvattestTargetEntry(
        key="linux-x86_64",
        source=SOURCE_IDENTITY,
        artifact=_artifact(
            "libnvat-linux-x86_64-1.2.2-sol.2-archive.tar.xz",
            size_bytes=7655628,
            sha256="3e2d207a3bb6eab9c47fc9cf65d7990f0d11541ff8691dce18e786e2ce9b26c7",
        ),
        companion_manifest=_manifest(
            "libnvat-linux-x86_64-1.2.2-sol.2-archive.manifest.json",
            sha256="a013d18a74d89f3ee99a0a3648c582df08446a30f5b8f5bbb48929a08fad4a8f",
        ),
        inventory=LINUX_INVENTORY,
    ),
    "linux-aarch64": NvattestTargetEntry(
        key="linux-aarch64",
        source=SOURCE_IDENTITY,
        artifact=_artifact(
            "libnvat-linux-aarch64-1.2.2-sol.2-archive.tar.xz",
            size_bytes=7423268,
            sha256="7a13a15192f5005a700dbe154da28cbc2a2b5f3f387113c8cd89a36083a2bd80",
        ),
        companion_manifest=_manifest(
            "libnvat-linux-aarch64-1.2.2-sol.2-archive.manifest.json",
            sha256="0d46ffdf21a9c5a61370fbf77aed0c275dae93f3609511f4573515a7260ddc39",
        ),
        inventory=LINUX_INVENTORY,
    ),
    "macos-arm64": NvattestTargetEntry(
        key="macos-arm64",
        source=SOURCE_IDENTITY,
        artifact=_artifact(
            "libnvat-macos-arm64-1.2.2-sol.2-archive.tar.xz",
            size_bytes=5848264,
            sha256="f2b01f60ee52f9c38c9e5e0f3cea6e8078a1efa165fad1f5c59994563120e365",
        ),
        companion_manifest=_manifest(
            "libnvat-macos-arm64-1.2.2-sol.2-archive.manifest.json",
            sha256="dca5342f87bba1244f8ee78848207410ae5ee8976df42fe10d804f1c99167094",
        ),
        inventory=MACOS_INVENTORY,
    ),
}


def nvattest_target_key(
    os_name: str | None = None,
    arch: str | None = None,
) -> NvattestTargetKey | None:
    if os_name is None:
        os_name = "linux" if sys.platform.startswith("linux") else sys.platform
    if arch is None:
        arch = platform.machine()
    normalized_arch = arch.lower()
    if os_name == "linux" and normalized_arch in {"x86_64", "amd64", "x64"}:
        return "linux-x86_64"
    if os_name == "linux" and normalized_arch in {"aarch64", "arm64"}:
        return "linux-aarch64"
    if os_name == "darwin" and normalized_arch == "arm64":
        return "macos-arm64"
    return None


def authority_entry(target_key: NvattestTargetKey) -> NvattestTargetEntry:
    return NVATTEST_AUTHORITY[target_key]


def authority_payload() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "targets": {key: NVATTEST_AUTHORITY[key].to_payload() for key in TARGET_KEYS},
    }


def validate_authority_payload(payload: Mapping[str, Any]) -> None:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("nvattest authority schema_version must be 1")
    targets = payload.get("targets")
    if not isinstance(targets, Mapping):
        raise ValueError("nvattest authority targets must be an object")
    target_keys = set(targets)
    expected_keys = set(TARGET_KEYS)
    if target_keys != expected_keys:
        raise ValueError("nvattest authority target set is not exact")

    artifact_names: set[str] = set()
    artifact_urls: set[str] = set()
    artifact_hashes: set[str] = set()
    manifest_names: set[str] = set()
    manifest_urls: set[str] = set()
    manifest_hashes: set[str] = set()
    sources: set[tuple[str, str, str, str]] = set()
    for target_key in TARGET_KEYS:
        raw_entry = targets[target_key]
        if not isinstance(raw_entry, Mapping):
            raise ValueError("nvattest authority target entry must be an object")
        source = _mapping(raw_entry.get("source"), "source")
        artifact = _mapping(raw_entry.get("artifact"), "artifact")
        companion_manifest = _mapping(
            raw_entry.get("companion_manifest"),
            "companion_manifest",
        )
        inventory = raw_entry.get("inventory")
        if not isinstance(inventory, list):
            raise ValueError("nvattest authority inventory must be a list")

        source_tuple = _validate_source(source)
        sources.add(source_tuple)
        _validate_artifact(target_key, source, artifact)
        _validate_manifest(target_key, source, companion_manifest)
        _validate_inventory(target_key, inventory)

        _add_unique(artifact_names, str(artifact["name"]), "artifact name")
        _add_unique(artifact_urls, str(artifact["url"]), "artifact url")
        _add_unique(artifact_hashes, str(artifact["sha256"]), "artifact sha256")
        _add_unique(manifest_names, str(companion_manifest["name"]), "manifest name")
        _add_unique(manifest_urls, str(companion_manifest["url"]), "manifest url")
        _add_unique(
            manifest_hashes,
            str(companion_manifest["sha256"]),
            "manifest sha256",
        )
    if len(sources) != 1:
        raise ValueError("nvattest authority source identity drifts across targets")


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"nvattest authority {label} must be an object")
    return value


def _validate_source(source: Mapping[str, Any]) -> tuple[str, str, str, str]:
    version = source.get("version")
    fork_commit = source.get("fork_commit")
    upstream_base = source.get("upstream_base")
    url_prefix = source.get("url_prefix")
    expected = (
        NVATTEST_VERSION,
        NVATTEST_FORK_COMMIT,
        NVATTEST_UPSTREAM_BASE,
        NVATTEST_URL_PREFIX,
    )
    observed = (version, fork_commit, upstream_base, url_prefix)
    if observed != expected:
        raise ValueError("nvattest authority source identity drifted")
    return cast(tuple[str, str, str, str], observed)


def _validate_artifact(
    target_key: NvattestTargetKey,
    source: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> None:
    name = artifact.get("name")
    url = artifact.get("url")
    size_bytes = artifact.get("size_bytes")
    sha256 = artifact.get("sha256")
    if name != f"libnvat-{target_key}-{source['version']}-archive.tar.xz":
        raise ValueError("nvattest artifact name does not match target/version")
    if url != f"{source['url_prefix']}{name}" or not str(url).startswith("https://"):
        raise ValueError("nvattest artifact url is not the pinned HTTPS url")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes <= 0
    ):
        raise ValueError("nvattest artifact size_bytes must be positive")
    _validate_hash(sha256, "artifact sha256")


def _validate_manifest(
    target_key: NvattestTargetKey,
    source: Mapping[str, Any],
    companion_manifest: Mapping[str, Any],
) -> None:
    name = companion_manifest.get("name")
    url = companion_manifest.get("url")
    sha256 = companion_manifest.get("sha256")
    if name != f"libnvat-{target_key}-{source['version']}-archive.manifest.json":
        raise ValueError("nvattest manifest name does not match target/version")
    if url != f"{source['url_prefix']}{name}" or not str(url).startswith("https://"):
        raise ValueError("nvattest manifest url is not the pinned HTTPS url")
    _validate_hash(sha256, "manifest sha256")


def _validate_hash(value: object, label: str) -> None:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"nvattest {label} must be lowercase 64 hex")


def _validate_inventory(target_key: NvattestTargetKey, inventory: list[object]) -> None:
    expected = _expected_inventory_payload(target_key)
    observed: dict[str, Mapping[str, Any]] = {}
    for raw_member in inventory:
        member = _mapping(raw_member, "inventory member")
        relpath = member.get("relpath")
        kind = member.get("kind")
        symlink_target = member.get("symlink_target")
        executable = member.get("executable")
        if kind not in {"regular", "symlink"}:
            raise ValueError("nvattest inventory kind must be regular or symlink")
        if not isinstance(relpath, str):
            raise ValueError("nvattest inventory relpath must be a string")
        _validate_relative_posix_path(relpath, "inventory relpath")
        if kind == "regular":
            if symlink_target is not None:
                raise ValueError("nvattest regular member symlink_target must be null")
        else:
            if not isinstance(symlink_target, str):
                raise ValueError("nvattest symlink member target must be a string")
            _validate_relative_link_target(symlink_target)
            if executable is not False:
                raise ValueError("nvattest symlink members must not be executable")
        if not isinstance(executable, bool):
            raise ValueError("nvattest inventory executable must be a boolean")
        if relpath in observed:
            raise ValueError("nvattest inventory contains duplicate relpath")
        observed[relpath] = member
    if set(observed) != set(expected):
        raise ValueError("nvattest inventory member set is not exact")
    for relpath, expected_member in expected.items():
        actual = observed[relpath]
        expected_payload = expected_member.to_payload()
        if dict(actual) != expected_payload:
            raise ValueError("nvattest inventory member does not match target platform")


def _expected_inventory_payload(
    target_key: NvattestTargetKey,
) -> dict[str, NvattestInventoryMember]:
    inventory = MACOS_INVENTORY if target_key == "macos-arm64" else LINUX_INVENTORY
    return {member.relpath: member for member in inventory}


def _validate_relative_posix_path(value: str, label: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise ValueError(f"nvattest {label} must be a safe relative POSIX path")


def _validate_relative_link_target(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise ValueError("nvattest symlink target must stay inside the tree")


def _add_unique(seen: set[str], value: str, label: str) -> None:
    if value in seen:
        raise ValueError(f"nvattest authority duplicate {label}")
    seen.add(value)


validate_authority_payload(authority_payload())

__all__ = [
    "NVATTEST_AUTHORITY",
    "NVATTEST_FORK_COMMIT",
    "NVATTEST_UPSTREAM_BASE",
    "NVATTEST_URL_PREFIX",
    "NVATTEST_VERSION",
    "NvattestArtifactIdentity",
    "NvattestInventoryMember",
    "NvattestManifestIdentity",
    "NvattestMemberKind",
    "NvattestSourceIdentity",
    "NvattestTargetEntry",
    "NvattestTargetKey",
    "TARGET_KEYS",
    "authority_entry",
    "authority_payload",
    "nvattest_target_key",
    "validate_authority_payload",
]
