#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Locked support-wheel helpers for nvattest release evidence."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from scripts.check_rust_release_manifest import SHA256_RE, Failure
from scripts.release_nvattest_proof import (
    SUPPORT_DISTRIBUTION_NAMES,
    NvattestProofError,
    support_distribution_entries,
)

REPAIR = "bash scripts/release.sh --candidate"
SUPPORT_ENTRY_KEYS = frozenset(("bytes", "filename", "name", "sha256", "version"))


@dataclass(frozen=True)
class SupportLockEntry:
    name: str
    version: str
    filename: str
    bytes: int
    sha256: str
    url: str

    def declaration(self) -> dict[str, object]:
        return {
            "bytes": self.bytes,
            "filename": self.filename,
            "name": self.name,
            "sha256": self.sha256,
            "version": self.version,
        }


class SupportLockError(RuntimeError):
    def __init__(self, failures: Sequence[Failure]) -> None:
        self.failures = tuple(failures)
        super().__init__("; ".join(failure.error for failure in self.failures))


def _failure(
    error: str,
    *,
    expected: str,
    actual: str,
    repair: str = REPAIR,
) -> Failure:
    return Failure(error=error, expected=expected, actual=actual, repair=repair)


def _normalize_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _safe_basename(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and Path(value).name == value
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
    )


def _url_basename(url: object) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    parsed = urlparse(url)
    basename = Path(unquote(parsed.path)).name
    return basename if _safe_basename(basename) else None


def _read_lock_payload(lock_path: Path) -> Mapping[str, Any]:
    try:
        with lock_path.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SupportLockError(
            [
                _failure(
                    "nvattest support lock could not be read",
                    expected="valid uv.lock TOML",
                    actual=f"{type(exc).__name__}: {exc}",
                )
            ]
        ) from None
    if not isinstance(payload, Mapping):
        raise SupportLockError(
            [
                _failure(
                    "nvattest support lock could not be read",
                    expected="valid uv.lock TOML object",
                    actual=type(payload).__name__,
                )
            ]
        )
    return payload


def read_support_lock_entries(lock_path: Path) -> tuple[SupportLockEntry, ...]:
    payload = _read_lock_payload(lock_path)
    packages = payload.get("package")
    if not isinstance(packages, list):
        raise SupportLockError(
            [
                _failure(
                    "nvattest support lock package is not exact",
                    expected="uv.lock package list",
                    actual=type(packages).__name__,
                )
            ]
        )

    failures: list[Failure] = []
    entries: list[SupportLockEntry] = []
    for name in sorted(SUPPORT_DISTRIBUTION_NAMES):
        matches = [
            package
            for package in packages
            if isinstance(package, Mapping)
            and _normalize_distribution_name(str(package.get("name", ""))) == name
        ]
        if len(matches) != 1:
            failures.append(
                _failure(
                    "nvattest support lock package is not exact",
                    expected=f"exactly one [[package]] for {name}",
                    actual=str(len(matches)),
                )
            )
            continue

        package = matches[0]
        version = package.get("version")
        wheels = package.get("wheels")
        if not isinstance(version, str) or not version:
            failures.append(
                _failure(
                    "nvattest support lock wheel metadata is invalid",
                    expected=f"{name} non-empty version",
                    actual=repr(version),
                )
            )
            continue
        if not isinstance(wheels, list):
            failures.append(
                _failure(
                    "nvattest support lock wheel entry is not exact",
                    expected=f"exactly one py3-none-any wheel for {name}",
                    actual=type(wheels).__name__,
                )
            )
            continue

        wheel_matches = [
            wheel
            for wheel in wheels
            if isinstance(wheel, Mapping)
            and (_url_basename(wheel.get("url")) or "").endswith("-py3-none-any.whl")
        ]
        if len(wheel_matches) != 1:
            actual = [
                _url_basename(wheel.get("url")) or repr(wheel.get("url"))
                for wheel in wheels
                if isinstance(wheel, Mapping)
            ]
            failures.append(
                _failure(
                    "nvattest support lock wheel entry is not exact",
                    expected=f"exactly one py3-none-any wheel for {name}",
                    actual=", ".join(sorted(actual)) or "<empty>",
                )
            )
            continue

        wheel = wheel_matches[0]
        filename = _url_basename(wheel.get("url"))
        raw_hash = wheel.get("hash")
        size = wheel.get("size")
        if (
            filename is None
            or not filename.endswith(".whl")
            or not isinstance(raw_hash, str)
            or not raw_hash.startswith("sha256:")
            or not SHA256_RE.fullmatch(raw_hash.removeprefix("sha256:"))
            or not isinstance(size, int)
            or size <= 0
        ):
            failures.append(
                _failure(
                    "nvattest support lock wheel metadata is invalid",
                    expected="url basename, sha256:<hex>, positive size, version",
                    actual=repr(wheel),
                )
            )
            continue
        entries.append(
            SupportLockEntry(
                name=name,
                version=version,
                filename=filename,
                bytes=size,
                sha256=raw_hash.removeprefix("sha256:"),
                url=str(wheel["url"]),
            )
        )

    if failures:
        raise SupportLockError(failures)
    return tuple(sorted(entries, key=lambda entry: (entry.name, entry.version)))


def support_declarations_from_lock(
    entries: Sequence[SupportLockEntry],
) -> tuple[dict[str, object], ...]:
    declarations = tuple(entry.declaration() for entry in entries)
    failures = validate_support_declarations(declarations, repair=REPAIR)
    if failures:
        raise SupportLockError(failures)
    return declarations


def validate_support_declarations(
    value: Any,
    *,
    expected: Sequence[Mapping[str, Any]] | None = None,
    repair: str,
) -> list[Failure]:
    failures: list[Failure] = []
    if not isinstance(value, list | tuple):
        return [
            _failure(
                "nvattest support declaration is invalid",
                expected="canonical support distribution list",
                actual=type(value).__name__,
                repair=repair,
            )
        ]
    actual = [dict(entry) for entry in value if isinstance(entry, Mapping)]
    if len(actual) != len(value):
        failures.append(
            _failure(
                "nvattest support declaration contains non-object entries",
                expected="support distribution objects",
                actual=repr(value),
                repair=repair,
            )
        )
    names: list[str] = []
    for entry in actual:
        if set(entry) != SUPPORT_ENTRY_KEYS:
            failures.append(
                _failure(
                    "nvattest support distribution entry is invalid",
                    expected=", ".join(sorted(SUPPORT_ENTRY_KEYS)),
                    actual=", ".join(sorted(str(key) for key in entry)) or "<empty>",
                    repair=repair,
                )
            )
            continue
        name = entry["name"]
        names.append(str(name))
        if (
            not isinstance(name, str)
            or _normalize_distribution_name(name) != name
            or name not in SUPPORT_DISTRIBUTION_NAMES
        ):
            failures.append(
                _failure(
                    "nvattest support distribution name is invalid",
                    expected=", ".join(sorted(SUPPORT_DISTRIBUTION_NAMES)),
                    actual=repr(name),
                    repair=repair,
                )
            )
        if not _safe_basename(entry["filename"]) or not str(entry["filename"]).endswith(
            ".whl"
        ):
            failures.append(
                _failure(
                    "nvattest support wheel filename is invalid",
                    expected="safe wheel basename",
                    actual=repr(entry["filename"]),
                    repair=repair,
                )
            )
        if not isinstance(entry["version"], str) or not entry["version"]:
            failures.append(
                _failure(
                    "nvattest support version is invalid",
                    expected="non-empty version string",
                    actual=repr(entry["version"]),
                    repair=repair,
                )
            )
        if not isinstance(entry["bytes"], int) or entry["bytes"] <= 0:
            failures.append(
                _failure(
                    "nvattest support wheel byte count is invalid",
                    expected="positive integer",
                    actual=repr(entry["bytes"]),
                    repair=repair,
                )
            )
        if not isinstance(entry["sha256"], str) or not SHA256_RE.fullmatch(
            entry["sha256"]
        ):
            failures.append(
                _failure(
                    "nvattest support wheel sha256 is invalid",
                    expected="lowercase SHA-256",
                    actual=repr(entry["sha256"]),
                    repair=repair,
                )
            )
    if len(names) != len(set(names)):
        failures.append(
            _failure(
                "nvattest support distribution is duplicated",
                expected="unique support distribution names",
                actual=", ".join(names),
                repair=repair,
            )
        )
    if set(names) != SUPPORT_DISTRIBUTION_NAMES:
        failures.append(
            _failure(
                "nvattest support distribution set is not exact",
                expected=", ".join(sorted(SUPPORT_DISTRIBUTION_NAMES)),
                actual=", ".join(sorted(set(names))) or "<empty>",
                repair=repair,
            )
        )
    canonical = sorted(actual, key=lambda item: (item["name"], item["version"]))
    if actual != canonical:
        failures.append(
            _failure(
                "nvattest support declaration is not canonical",
                expected="support distributions sorted by normalized name and version",
                actual=repr(actual),
                repair=repair,
            )
        )
    if expected is not None and actual != [dict(entry) for entry in expected]:
        failures.append(
            _failure(
                "nvattest support declaration is not bound to expected wheels",
                expected=repr([dict(entry) for entry in expected]),
                actual=repr(actual),
                repair=repair,
            )
        )
    return failures


def verify_support_wheels_against_lock(
    paths: Sequence[Path],
    entries: Sequence[SupportLockEntry],
) -> tuple[dict[str, object], ...]:
    expected = support_declarations_from_lock(entries)
    failures: list[Failure] = []
    for raw_path in paths:
        path = Path(raw_path)
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or path.suffix != ".whl"
        ):
            failures.append(
                _failure(
                    "nvattest support wheel path is invalid",
                    expected="absolute regular local wheel file path",
                    actual=str(raw_path),
                )
            )
    if failures:
        raise SupportLockError(failures)
    try:
        observed = tuple(support_distribution_entries(paths))
    except NvattestProofError as exc:
        raise SupportLockError(
            [
                _failure(
                    failure.error,
                    expected=failure.expected,
                    actual=failure.actual,
                    repair=REPAIR,
                )
                for failure in exc.failures
            ]
        ) from None
    by_filename = {str(entry["filename"]): entry for entry in observed}
    expected_by_filename = {entry.filename: entry for entry in entries}
    if set(by_filename) != set(expected_by_filename):
        failures.append(
            _failure(
                "nvattest support inventory is not exact",
                expected=", ".join(sorted(expected_by_filename)),
                actual=", ".join(sorted(by_filename)) or "<empty>",
            )
        )
    for filename in sorted(set(by_filename) & set(expected_by_filename)):
        lock_entry = expected_by_filename[filename]
        observed_entry = by_filename[filename]
        if (
            observed_entry.get("sha256") != lock_entry.sha256
            or observed_entry.get("bytes") != lock_entry.bytes
        ):
            failures.append(
                _failure(
                    "nvattest support wheel does not match uv.lock",
                    expected=f"{filename} {lock_entry.sha256}/{lock_entry.bytes}",
                    actual=(
                        f"{observed_entry.get('sha256')}/{observed_entry.get('bytes')}"
                    ),
                )
            )
        if (
            observed_entry.get("name") != lock_entry.name
            or observed_entry.get("version") != lock_entry.version
        ):
            failures.append(
                _failure(
                    "nvattest support wheel METADATA does not match uv.lock",
                    expected=f"{lock_entry.name}=={lock_entry.version}",
                    actual=(
                        f"{observed_entry.get('name')}=={observed_entry.get('version')}"
                    ),
                )
            )
    if failures:
        raise SupportLockError(failures)
    return expected
