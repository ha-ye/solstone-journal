#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Keep a Maturin workspace sdist's lock aligned with its pruned workspace."""

from __future__ import annotations

import copy
import gzip
import io
import os
import re
import stat
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath


class SdistLockError(RuntimeError):
    """The built sdist cannot be normalized without weakening its lock."""


PACKAGE_BLOCK_RE = re.compile(r"(?ms)^\[\[package\]\]\n.*?(?=^\[\[package\]\]\n|\Z)")
DEPENDENCY_RE = re.compile(
    r"^(?P<name>\S+)(?: (?P<version>\S+)(?: \((?P<source>.+)\))?)?$"
)


def _workspace_members(manifest: bytes, *, label: str) -> tuple[str, ...]:
    try:
        data = tomllib.loads(manifest.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SdistLockError(f"{label} Cargo.toml is invalid: {exc}") from None
    members = data.get("workspace", {}).get("members")
    if not isinstance(members, list) or not members:
        raise SdistLockError(f"{label} Cargo.toml has no workspace members")
    if any(not isinstance(member, str) or not member for member in members):
        raise SdistLockError(f"{label} Cargo.toml has invalid workspace members")
    return tuple(members)


def _package_name(manifest: Path, *, member: str) -> str:
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SdistLockError(
            f"source workspace member {member} Cargo.toml is invalid: {exc}"
        ) from None
    name = data.get("package", {}).get("name")
    if not isinstance(name, str) or not name:
        raise SdistLockError(f"source workspace member {member} has no package name")
    return name


def _retain_reachable_lock_packages(
    lock_bytes: bytes,
    *,
    retained_names: frozenset[str],
    pruned_names: frozenset[str],
) -> bytes:
    try:
        text = lock_bytes.decode("utf-8")
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SdistLockError(f"sdist Cargo.lock is invalid: {exc}") from None

    packages = parsed.get("package")
    if not isinstance(packages, list) or not packages:
        raise SdistLockError("sdist Cargo.lock has no package records")
    matches = list(PACKAGE_BLOCK_RE.finditer(text))
    if len(matches) != len(packages):
        raise SdistLockError("sdist Cargo.lock package records cannot be isolated")

    identities: list[tuple[str, str, str | None]] = []
    by_name: dict[str, list[int]] = {}
    for index, package in enumerate(packages):
        if not isinstance(package, dict):
            raise SdistLockError("sdist Cargo.lock has an invalid package record")
        name = package.get("name")
        version = package.get("version")
        source = package.get("source")
        dependencies = package.get("dependencies", [])
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(version, str)
            or not version
            or (source is not None and not isinstance(source, str))
            or not isinstance(dependencies, list)
            or any(not isinstance(dependency, str) for dependency in dependencies)
        ):
            raise SdistLockError("sdist Cargo.lock has an invalid package record")
        identity = (name, version, source)
        if identity in identities:
            raise SdistLockError(
                f"sdist Cargo.lock repeats package identity {name} {version}"
            )
        identities.append(identity)
        by_name.setdefault(name, []).append(index)

    roots: list[int] = []
    for name in sorted(retained_names):
        candidates = [
            index for index in by_name.get(name, []) if identities[index][2] is None
        ]
        if len(candidates) != 1:
            raise SdistLockError(
                f"sdist Cargo.lock does not identify retained workspace package {name}"
            )
        roots.append(candidates[0])

    for name in sorted(pruned_names):
        candidates = by_name.get(name, [])
        if candidates and not any(identities[index][2] is None for index in candidates):
            raise SdistLockError(
                f"sdist Cargo.lock pruned package {name} is not a workspace package"
            )

    def dependency_index(dependency: str, *, parent: str) -> int:
        match = DEPENDENCY_RE.fullmatch(dependency)
        if match is None:
            raise SdistLockError(
                f"sdist Cargo.lock dependency {dependency!r} from {parent} is invalid"
            )
        candidates = list(by_name.get(match.group("name"), []))
        version = match.group("version")
        source = match.group("source")
        if version is not None:
            candidates = [
                index for index in candidates if identities[index][1] == version
            ]
        if source is not None:
            candidates = [
                index for index in candidates if identities[index][2] == source
            ]
        if len(candidates) != 1:
            raise SdistLockError(
                f"sdist Cargo.lock dependency {dependency!r} from {parent} "
                "does not resolve uniquely"
            )
        return candidates[0]

    reachable: set[int] = set()
    pending = roots[:]
    while pending:
        index = pending.pop()
        if index in reachable:
            continue
        reachable.add(index)
        package = packages[index]
        parent = f"{identities[index][0]} {identities[index][1]}"
        pending.extend(
            dependency_index(dependency, parent=parent)
            for dependency in package.get("dependencies", [])
        )

    if len(reachable) == len(packages):
        return lock_bytes

    pieces: list[str] = []
    cursor = 0
    for index, match in enumerate(matches):
        pieces.append(text[cursor : match.start()])
        if index in reachable:
            pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(text[cursor:])
    rewritten = "".join(pieces)

    try:
        normalized = tomllib.loads(rewritten)
    except tomllib.TOMLDecodeError as exc:
        raise SdistLockError(f"normalized sdist Cargo.lock is invalid: {exc}") from None
    remaining = normalized.get("package", [])
    if len(remaining) != len(reachable):
        raise SdistLockError("normalized sdist Cargo.lock package count changed")
    return rewritten.encode("utf-8")


def _read_archive(
    archive: Path,
) -> tuple[list[tuple[tarfile.TarInfo, bytes | None]], str, bytes, bytes]:
    try:
        with tarfile.open(archive, mode="r:gz") as source:
            members = source.getmembers()
            entries: list[tuple[tarfile.TarInfo, bytes | None]] = []
            names: set[str] = set()
            roots: set[str] = set()
            cargo_manifest: bytes | None = None
            cargo_lock: bytes | None = None
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
                    raise SdistLockError(
                        f"sdist contains unsafe member path {member.name!r}"
                    )
                if member.name in names:
                    raise SdistLockError(f"sdist repeats member path {member.name!r}")
                names.add(member.name)
                roots.add(path.parts[0])
                if not (member.isfile() or member.isdir()):
                    raise SdistLockError(
                        f"sdist contains unsupported member type {member.name!r}"
                    )
                data = None
                if member.isfile():
                    stream = source.extractfile(member)
                    if stream is None:
                        raise SdistLockError(
                            f"sdist regular file is unreadable {member.name!r}"
                        )
                    data = stream.read()
                entries.append((copy.copy(member), data))
            if len(roots) != 1:
                raise SdistLockError("sdist does not have exactly one archive root")
            root = next(iter(roots))
            manifest_name = f"{root}/core/Cargo.toml"
            lock_name = f"{root}/core/Cargo.lock"
            for member, data in entries:
                if member.name == manifest_name:
                    cargo_manifest = data
                elif member.name == lock_name:
                    cargo_lock = data
            if cargo_manifest is None or cargo_lock is None:
                raise SdistLockError(
                    "sdist is missing core/Cargo.toml or core/Cargo.lock"
                )
            return entries, root, cargo_manifest, cargo_lock
    except (OSError, tarfile.TarError) as exc:
        raise SdistLockError(f"sdist archive is unreadable: {exc}") from None


def _replace_archive_lock(
    archive: Path,
    *,
    entries: list[tuple[tarfile.TarInfo, bytes | None]],
    lock_name: str,
    lock_bytes: bytes,
) -> None:
    archive_stat = archive.stat()
    descriptor, temporary_name = tempfile.mkstemp(
        dir=archive.parent, prefix=f".{archive.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(
                filename="", mode="wb", compresslevel=9, fileobj=raw, mtime=0
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
                ) as target:
                    for member, original_data in entries:
                        data = lock_bytes if member.name == lock_name else original_data
                        if member.isfile():
                            assert data is not None
                            member.size = len(data)
                            target.addfile(member, io.BytesIO(data))
                        else:
                            target.addfile(member)
            raw.flush()
            os.fsync(raw.fileno())
        os.chmod(temporary, stat.S_IMODE(archive_stat.st_mode))
        os.replace(temporary, archive)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def normalize_core_sdist_workspace_lock(root: Path, archive: Path) -> tuple[str, ...]:
    """Retain the exact locked graph reachable from Maturin's sdist workspace."""

    root = root.resolve()
    if archive.is_symlink():
        raise SdistLockError("core sdist must be a regular file directly under dist")
    archive = archive.resolve()
    expected_parent = (root / "dist").resolve()
    if archive.parent != expected_parent:
        raise SdistLockError("core sdist must be a regular file directly under dist")
    if not archive.is_file():
        raise SdistLockError("core sdist is missing or is not a regular file")

    entries, archive_root, sdist_manifest, lock_bytes = _read_archive(archive)
    source_manifest = (root / "core" / "Cargo.toml").read_bytes()
    source_members = frozenset(
        _workspace_members(source_manifest, label="source workspace")
    )
    sdist_members = frozenset(
        _workspace_members(sdist_manifest, label="sdist workspace")
    )
    if not sdist_members <= source_members:
        raise SdistLockError(
            "sdist workspace members are not a source-workspace subset"
        )
    pruned_members = source_members - sdist_members
    pruned_names = frozenset(
        _package_name(root / "core" / member / "Cargo.toml", member=member)
        for member in pruned_members
    )
    retained_names = frozenset(
        _package_name(root / "core" / member / "Cargo.toml", member=member)
        for member in sdist_members
    )
    if len(pruned_names) != len(pruned_members):
        raise SdistLockError("pruned source workspace package names are not unique")
    if len(retained_names) != len(sdist_members):
        raise SdistLockError("retained source workspace package names are not unique")
    if retained_names & pruned_names:
        raise SdistLockError("source workspace package names are not unique")
    if not pruned_names:
        return ()

    rewritten = _retain_reachable_lock_packages(
        lock_bytes,
        retained_names=retained_names,
        pruned_names=pruned_names,
    )
    if rewritten == lock_bytes:
        return ()
    _replace_archive_lock(
        archive,
        entries=entries,
        lock_name=f"{archive_root}/core/Cargo.lock",
        lock_bytes=rewritten,
    )
    return tuple(sorted(pruned_names))
