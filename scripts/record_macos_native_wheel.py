#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Write normalized macOS native wheel records for release evidence."""

from __future__ import annotations

import argparse
import json
import os
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from scripts.check_rust_release_manifest import (
    SHA256_RE,
    SOURCE_COMMIT_RE,
    Failure,
    _format_failures,
    canonical_json_bytes,
)
from scripts.check_wheel_contents import (
    PARAKEET_HELPER_MEMBER,
    core_wheel_script_members,
)
from scripts.release_digest import file_sha256_size
from scripts.release_public_evidence import validate_public_evidence_tree
from scripts.release_tool_pins import (
    MACOS_CODESIGN_PUBLIC_PIN,
    MACOS_NOTARYTOOL_PIN,
    MACOS_SIGNING_MODE,
    MACOS_SWIFT_PIN,
    MACOS_XCODE_PIN,
    PYTHON_MACOS_VERSION,
)

NativeRole = Literal["root", "core"]

KIND = "macos-native-record/v1"
TARGET = {
    "triple": "aarch64-apple-darwin",
    "profile": "release",
    "features": [],
}
TOP_LEVEL_KEYS = frozenset(
    (
        "schema_version",
        "kind",
        "source_commit",
        "core_lock_sha256",
        "role",
        "target",
        "wheel",
        "member",
        "tools",
        "signing_mode",
        "signing",
        "notarization_status",
    )
)
SIGNING_KEYS = frozenset(
    ("signer_pinned", "team_pinned", "hardened_runtime", "trusted_timestamp")
)
FACT_KEYS = SIGNING_KEYS | frozenset(
    ("signed_binary_sha256", "notarization_status", "tools")
)
FACT_TOOL_KEYS = frozenset(("xcode", "swift", "codesign", "notarytool"))


class NativeRecordError(RuntimeError):
    def __init__(self, failures: Sequence[Failure]) -> None:
        self.failures = tuple(failures)
        super().__init__("; ".join(failure.error for failure in self.failures))


def _failure(error: str, *, expected: str, actual: str, repair: str) -> Failure:
    return Failure(error=error, expected=expected, actual=actual, repair=repair)


def _member_for_role(
    wheel: zipfile.ZipFile, role: NativeRole
) -> zipfile.ZipInfo | None:
    if role == "root":
        helpers = [
            info for info in wheel.infolist() if info.filename == PARAKEET_HELPER_MEMBER
        ]
        return helpers[0] if len(helpers) == 1 else None
    scripts = core_wheel_script_members(wheel)
    return scripts[0] if len(scripts) == 1 else None


def _expected_member_path(role: NativeRole) -> str:
    if role == "root":
        return PARAKEET_HELPER_MEMBER
    return ".data/scripts/solstone-core"


def _role_matches_wheel(role: NativeRole, wheel_name: str) -> bool:
    if role == "root":
        return wheel_name.startswith("solstone-") and wheel_name.endswith(".whl")
    return wheel_name.startswith("solstone_core-") and wheel_name.endswith(".whl")


def _read_member(
    wheel_path: Path, role: NativeRole
) -> tuple[str, bytes] | list[Failure]:
    if wheel_path.is_symlink():
        return [
            _failure(
                "macOS native wheel is a symlink",
                expected="regular wheel file",
                actual=wheel_path.name,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        ]
    if not _role_matches_wheel(role, wheel_path.name):
        return [
            _failure(
                "macOS native record role does not match wheel name",
                expected=f"{role} wheel name",
                actual=wheel_path.name,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        ]
    with zipfile.ZipFile(wheel_path) as wheel:
        member = _member_for_role(wheel, role)
        if member is None:
            return [
                _failure(
                    "macOS native wheel member count is wrong",
                    expected=f"exactly one {_expected_member_path(role)}",
                    actual="missing or duplicate",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        return member.filename, wheel.read(member)


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _load_facts(path: Path) -> Mapping[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, Mapping):
        raise NativeRecordError(
            [
                _failure(
                    "macOS signing facts are not an object",
                    expected="JSON object",
                    actual=type(data).__name__,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        )
    return data


def _validate_facts(facts: Mapping[str, Any]) -> list[Failure]:
    failures: list[Failure] = []
    if set(facts) != FACT_KEYS:
        failures.append(
            _failure(
                "macOS signing facts key set is wrong",
                expected=", ".join(sorted(FACT_KEYS)),
                actual=", ".join(sorted(str(key) for key in facts)),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    signed = facts.get("signed_binary_sha256")
    if not isinstance(signed, str) or not SHA256_RE.fullmatch(signed):
        failures.append(
            _failure(
                "macOS signed binary hash is invalid",
                expected="lowercase SHA-256",
                actual=str(signed),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    for key in SIGNING_KEYS:
        if facts.get(key) is not True:
            failures.append(
                _failure(
                    f"macOS signing fact {key} is not pinned true",
                    expected="true",
                    actual=repr(facts.get(key)),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    if facts.get("notarization_status") != "accepted":
        failures.append(
            _failure(
                "macOS notarization status is not accepted",
                expected="accepted",
                actual=str(facts.get("notarization_status")),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    tools = facts.get("tools")
    if not isinstance(tools, Mapping) or set(tools) != FACT_TOOL_KEYS:
        failures.append(
            _failure(
                "macOS signing tool facts key set is wrong",
                expected=", ".join(sorted(FACT_TOOL_KEYS)),
                actual=(
                    ", ".join(sorted(str(key) for key in tools))
                    if isinstance(tools, Mapping)
                    else type(tools).__name__
                ),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    else:
        expected_tools = {
            "xcode": MACOS_XCODE_PIN,
            "swift": MACOS_SWIFT_PIN,
            "codesign": MACOS_CODESIGN_PUBLIC_PIN,
            "notarytool": MACOS_NOTARYTOOL_PIN,
        }
        for key, expected in expected_tools.items():
            if tools.get(key) != expected:
                failures.append(
                    _failure(
                        f"macOS {key} tool evidence is not pinned",
                        expected=expected,
                        actual=str(tools.get(key)),
                        repair="python3 scripts/check_rust_release_manifest.py",
                    )
                )
    failures.extend(validate_public_evidence_tree("macos_signing_facts", facts))
    return failures


def build_macos_native_record(
    *,
    role: NativeRole,
    wheel_path: Path,
    signing_facts: Mapping[str, Any],
    source_commit: str,
    core_lock_sha256: str,
    python_version: str = PYTHON_MACOS_VERSION,
) -> dict[str, Any]:
    failures = _validate_facts(signing_facts)
    if not SOURCE_COMMIT_RE.fullmatch(source_commit):
        failures.append(
            _failure(
                "macOS native record source commit is invalid",
                expected="full lowercase commit",
                actual=source_commit,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if not SHA256_RE.fullmatch(core_lock_sha256):
        failures.append(
            _failure(
                "macOS native record core lock hash is invalid",
                expected="lowercase SHA-256",
                actual=core_lock_sha256,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if python_version != PYTHON_MACOS_VERSION:
        failures.append(
            _failure(
                "macOS Python evidence is not pinned",
                expected=PYTHON_MACOS_VERSION,
                actual=python_version,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    member = _read_member(wheel_path, role)
    if isinstance(member, list):
        failures.extend(member)
        member_path = _expected_member_path(role)
        member_bytes = b""
    else:
        member_path, member_bytes = member
    member_sha256 = _sha256_bytes(member_bytes)
    signed_sha256 = signing_facts.get("signed_binary_sha256")
    if isinstance(signed_sha256, str) and member_sha256 != signed_sha256:
        failures.append(
            _failure(
                "macOS signed binary hash does not match final wheel member",
                expected=signed_sha256,
                actual=member_sha256,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if failures:
        raise NativeRecordError(failures)

    wheel_sha256, wheel_bytes = file_sha256_size(wheel_path)
    tools = signing_facts["tools"]
    record: dict[str, Any] = {
        "schema_version": 1,
        "kind": KIND,
        "source_commit": source_commit,
        "core_lock_sha256": core_lock_sha256,
        "role": role,
        "target": dict(TARGET),
        "wheel": {
            "name": wheel_path.name,
            "sha256": wheel_sha256,
            "bytes": wheel_bytes,
        },
        "member": {
            "path": member_path,
            "sha256": member_sha256,
            "bytes": len(member_bytes),
        },
        "tools": {
            "python": python_version,
            "xcode": tools["xcode"],
            "swift": tools["swift"],
            "codesign": tools["codesign"],
            "notarytool": tools["notarytool"],
        },
        "signing_mode": MACOS_SIGNING_MODE,
        "signing": {key: signing_facts[key] for key in sorted(SIGNING_KEYS)},
        "notarization_status": signing_facts["notarization_status"],
    }
    record_failures = validate_macos_native_record(
        record,
        role=role,
        wheel_path=wheel_path,
        source_commit=source_commit,
        core_lock_sha256=core_lock_sha256,
    )
    if record_failures:
        raise NativeRecordError(record_failures)
    return record


def validate_macos_native_record(
    record: Mapping[str, Any],
    *,
    role: NativeRole,
    wheel_path: Path,
    source_commit: str,
    core_lock_sha256: str,
) -> list[Failure]:
    failures: list[Failure] = []
    if set(record) != TOP_LEVEL_KEYS:
        failures.append(
            _failure(
                "macOS native record key set is wrong",
                expected=", ".join(sorted(TOP_LEVEL_KEYS)),
                actual=", ".join(sorted(str(key) for key in record)),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    expected_scalars = {
        "schema_version": 1,
        "kind": KIND,
        "source_commit": source_commit,
        "core_lock_sha256": core_lock_sha256,
        "role": role,
        "target": TARGET,
        "signing_mode": MACOS_SIGNING_MODE,
        "notarization_status": "accepted",
    }
    for key, expected in expected_scalars.items():
        if record.get(key) != expected:
            failures.append(
                _failure(
                    f"macOS native record {key} is wrong",
                    expected=repr(expected),
                    actual=repr(record.get(key)),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    signing = record.get("signing")
    if not isinstance(signing, Mapping) or set(signing) != SIGNING_KEYS:
        failures.append(
            _failure(
                "macOS native record signing key set is wrong",
                expected=", ".join(sorted(SIGNING_KEYS)),
                actual=(
                    ", ".join(sorted(str(key) for key in signing))
                    if isinstance(signing, Mapping)
                    else type(signing).__name__
                ),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    else:
        for key in SIGNING_KEYS:
            if signing.get(key) is not True:
                failures.append(
                    _failure(
                        f"macOS native record signing {key} is not true",
                        expected="true",
                        actual=repr(signing.get(key)),
                        repair="python3 scripts/check_rust_release_manifest.py",
                    )
                )
    tools = record.get("tools")
    expected_tools = {
        "python": PYTHON_MACOS_VERSION,
        "xcode": MACOS_XCODE_PIN,
        "swift": MACOS_SWIFT_PIN,
        "codesign": MACOS_CODESIGN_PUBLIC_PIN,
        "notarytool": MACOS_NOTARYTOOL_PIN,
    }
    if not isinstance(tools, Mapping) or set(tools) != set(expected_tools):
        failures.append(
            _failure(
                "macOS native record tools key set is wrong",
                expected=", ".join(sorted(expected_tools)),
                actual=(
                    ", ".join(sorted(str(key) for key in tools))
                    if isinstance(tools, Mapping)
                    else type(tools).__name__
                ),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    else:
        for key, expected in expected_tools.items():
            if tools.get(key) != expected:
                failures.append(
                    _failure(
                        f"macOS native record tool {key} is not pinned",
                        expected=expected,
                        actual=str(tools.get(key)),
                        repair="python3 scripts/check_rust_release_manifest.py",
                    )
                )

    member = _read_member(wheel_path, role)
    if isinstance(member, list):
        failures.extend(member)
    else:
        member_path, member_bytes = member
        expected_member = {
            "path": member_path,
            "sha256": _sha256_bytes(member_bytes),
            "bytes": len(member_bytes),
        }
        if record.get("member") != expected_member:
            failures.append(
                _failure(
                    "macOS native record member does not match wheel",
                    expected=repr(expected_member),
                    actual=repr(record.get("member")),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
    expected_wheel_sha256, expected_wheel_bytes = file_sha256_size(wheel_path)
    expected_wheel = {
        "name": wheel_path.name,
        "sha256": expected_wheel_sha256,
        "bytes": expected_wheel_bytes,
    }
    if record.get("wheel") != expected_wheel:
        failures.append(
            _failure(
                "macOS native record wheel does not match final wheel",
                expected=repr(expected_wheel),
                actual=repr(record.get("wheel")),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    failures.extend(validate_public_evidence_tree("macos_native_record", record))
    return failures


def write_macos_native_record(
    *,
    role: NativeRole,
    wheel_path: Path,
    signing_facts_path: Path,
    output_path: Path,
    source_commit: str,
    core_lock_sha256: str,
    python_version: str = PYTHON_MACOS_VERSION,
) -> Path:
    record = build_macos_native_record(
        role=role,
        wheel_path=wheel_path,
        signing_facts=_load_facts(signing_facts_path),
        source_commit=source_commit,
        core_lock_sha256=core_lock_sha256,
        python_version=python_version,
    )
    payload = canonical_json_bytes(record)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.name}.tmp")
    try:
        with temp_path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)
    readback = json.loads(output_path.read_text(encoding="utf-8"))
    failures = validate_macos_native_record(
        readback,
        role=role,
        wheel_path=wheel_path,
        source_commit=source_commit,
        core_lock_sha256=core_lock_sha256,
    )
    if failures:
        raise NativeRecordError(failures)
    return output_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("root", "core"), required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--signing-facts", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--core-lock-sha256", required=True)
    parser.add_argument("--python-version", default=PYTHON_MACOS_VERSION)
    args = parser.parse_args(argv)
    try:
        write_macos_native_record(
            role=args.role,
            wheel_path=args.wheel,
            signing_facts_path=args.signing_facts,
            output_path=args.out,
            source_commit=args.source_commit,
            core_lock_sha256=args.core_lock_sha256,
            python_version=args.python_version,
        )
    except NativeRecordError as exc:
        _format_failures(exc.failures)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
