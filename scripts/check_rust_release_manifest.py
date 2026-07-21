#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Build and validate offline Rust release manifests for solstone-core."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
for _path in (str(ROOT), str(_SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from check_wheel_contents import release_artifacts  # noqa: E402

from scripts.release_tool_pins import (  # noqa: E402
    CARGO_DENY_PIN,
    CARGO_RELEASE_PIN,
    CARGO_VERSION_PIN,
    RUSTC_BINARY_PIN,
    RUSTC_COMMIT_DATE_PIN,
    RUSTC_COMMIT_HASH_PIN,
    RUSTC_LLVM_PIN,
    RUSTC_RELEASE_PIN,
    RUSTC_VERSION_BANNER,
    fixture_native_tools,
)
from solstone.think.probe import (  # noqa: E402
    SOLSTONE_CORE_COVERED_PLATFORMS,
    SOLSTONE_CORE_PLATFORM_TAGS,
    CorePlatform,
)

SCHEMA_PATH = ROOT / "schemas" / "rust-release-manifest" / "v1.json"
SCHEMA_SHA256 = "d4eabf52bcc68b56945912d351f818e5444fe8c6461cb5c48b096f87b17a875c"
SCHEMA_ID = "https://solpbc.org/schemas/rust-release-manifest/v1.json"
SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
PRODUCT = "solstone-core"
SOURCE_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RFC3339_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$"
)
RAW_ENV_RE = re.compile(r"(?i)\b[A-Z_][A-Z0-9_]{2,}=")
SECRET_RE = re.compile(
    r"(?i)\b(secret|token|password|passwd|pwd|api[_-]?key|private[_-]?key|bearer|session|credential)\b"
    r"|sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}"
)
PRIVATE_HOST_RE = re.compile(r"(?i)\b(?:localhost|[A-Za-z0-9-]+\.local)\b")
ABSOLUTE_PATH_RE = re.compile(r"(^|\s)(?:/[^ \t\r\n]+|~[^ \t\r\n]*|[A-Za-z]:[\\/])")
EMAIL_RE = re.compile(r"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}")
SIGNER_RE = re.compile(
    r"(?i)\b(Developer ID|Apple Development|Team ID|TeamIdentifier|Authority=|Signed by|Apple ID|account|issuer|CN=|OU=)\b"
)
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
IP_CANDIDATE_RE = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-f:]*:[0-9a-f:]+\b",
    re.IGNORECASE,
)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

LaneName = Literal[
    "source",
    "linux-x86_64-musl",
    "linux-aarch64-musl",
    "macos-arm64",
]
ReleaseMode = Literal["fixtures", "manifest", "release-dir"]

LANES: tuple[LaneName, ...] = (
    "source",
    "linux-x86_64-musl",
    "linux-aarch64-musl",
    "macos-arm64",
)
PLATFORM_TRIPLES: dict[CorePlatform, str] = {
    ("linux", "x86_64"): "x86_64-unknown-linux-musl",
    ("linux", "aarch64"): "aarch64-unknown-linux-musl",
    ("darwin", "arm64"): "aarch64-apple-darwin",
}
PLATFORM_LANES: dict[CorePlatform, LaneName] = {
    ("linux", "x86_64"): "linux-x86_64-musl",
    ("linux", "aarch64"): "linux-aarch64-musl",
    ("darwin", "arm64"): "macos-arm64",
}
LANE_HOSTS: dict[LaneName, str] = {
    "source": "x86_64-unknown-linux-gnu",
    "linux-x86_64-musl": "x86_64-unknown-linux-gnu",
    "linux-aarch64-musl": "x86_64-unknown-linux-gnu",
    "macos-arm64": "aarch64-apple-darwin",
}
NATIVE_TOOL_KEYS: dict[LaneName, frozenset[str]] = {
    "source": frozenset(("uv", "maturin")),
    "linux-x86_64-musl": frozenset(("uv", "maturin", "zig")),
    "linux-aarch64-musl": frozenset(("uv", "maturin", "zig")),
    "macos-arm64": frozenset(
        ("uv", "maturin", "xcode", "swift", "codesign", "notarytool", "signing_mode")
    ),
}
ORDER_INDEPENDENT_LIST_KEYS = frozenset(("features", "active_exceptions"))


@dataclass(frozen=True)
class Failure:
    error: str
    expected: str
    actual: str
    repair: str


@dataclass(frozen=True)
class LaneEvidence:
    rustc_verbose: str
    cargo_version: str
    native_tools: Mapping[str, str]
    cargo_deny_version: str
    advisory_checked_at: str


@dataclass(frozen=True)
class CohortInputs:
    product: str
    version: str
    source_commit: str
    source_dirty: bool
    active_exceptions: tuple[str, ...]
    deterministic_gate: str = "pass"


@dataclass(frozen=True)
class GeneratedManifest:
    artifact_name: str
    manifest_name: str
    payload: dict[str, Any]
    bytes: bytes


@dataclass(frozen=True)
class RustcVerbose:
    first_line: str
    release: str
    commit_hash: str
    commit_date: str
    host: str
    labels: dict[str, str]


@dataclass(frozen=True)
class ManifestRecord:
    path: Path
    payload: dict[str, Any]
    artifact_name: str
    lane: LaneName
    rustc: RustcVerbose
    cargo_release: str


@dataclass(frozen=True)
class ReleaseInventory:
    package_names: tuple[str, ...]
    manifest_records: tuple[ManifestRecord, ...]
    include_models: bool


def _failure(error: str, *, expected: str, actual: str, repair: str) -> Failure:
    return Failure(error=error, expected=expected, actual=actual, repair=repair)


def _format_failures(failures: Sequence[Failure]) -> None:
    for failure in failures:
        print(f"ERROR: {failure.error}", file=sys.stderr)
        print(f"  expected: {failure.expected}", file=sys.stderr)
        print(f"  actual: {failure.actual}", file=sys.stderr)
        print(f"  repair command: {failure.repair}", file=sys.stderr)


def _jsonschema() -> tuple[Any, Any]:
    from jsonschema import Draft202012Validator, FormatChecker

    return Draft202012Validator, FormatChecker


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SCHEMA_SHA256:
        raise ValueError(
            f"{path}: schema SHA-256 mismatch; expected {SCHEMA_SHA256}, actual {digest}"
        )
    if not raw.endswith(b"\n"):
        raise ValueError(f"{path}: schema must end with a trailing newline")
    schema = json.loads(raw)
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: schema must be a JSON object")
    if schema.get("$id") != SCHEMA_ID:
        raise ValueError(f"{path}: unexpected $id {schema.get('$id')!r}")
    if schema.get("$schema") != SCHEMA_DRAFT:
        raise ValueError(f"{path}: unexpected $schema {schema.get('$schema')!r}")
    Draft202012Validator, _FormatChecker = _jsonschema()
    Draft202012Validator.check_schema(schema)
    return schema


def _schema_load_failure(exc: Exception) -> Failure:
    return _failure(
        "schema file does not match canonical Rust release manifest schema",
        expected=f"{SCHEMA_PATH} SHA-256 {SCHEMA_SHA256}",
        actual=str(exc),
        repair="python3 scripts/check_rust_release_manifest.py",
    )


def expected_package_names(*, include_models: bool) -> tuple[str, ...]:
    decision = "publish" if include_models else "skip"
    return tuple(
        path.name
        for path in release_artifacts(
            Path("__solstone_release_dist__"),
            release_scope="all-hosts",
            models_decision=decision,
        )
    )


def _current_version() -> str:
    for name in expected_package_names(include_models=False):
        match = re.fullmatch(r"solstone-(?P<version>.+)\.tar\.gz", name)
        if match:
            return match.group("version")
    raise RuntimeError("release_artifacts did not include the root sdist")


def _models_expected_names() -> frozenset[str]:
    skip = set(expected_package_names(include_models=False))
    publish = set(expected_package_names(include_models=True))
    return frozenset(publish - skip)


def rust_artifact_targets() -> dict[str, tuple[LaneName, dict[str, Any]]]:
    names = expected_package_names(include_models=False)
    targets: dict[str, tuple[LaneName, dict[str, Any]]] = {}
    source_names = [
        name
        for name in names
        if name.startswith("solstone_core-") and name.endswith(".tar.gz")
    ]
    if len(source_names) != 1:
        raise RuntimeError("release_artifacts did not include exactly one core sdist")
    targets[source_names[0]] = ("source", {"kind": "source"})
    for platform_tuple in SOLSTONE_CORE_COVERED_PLATFORMS:
        tag = SOLSTONE_CORE_PLATFORM_TAGS[platform_tuple]
        lane = PLATFORM_LANES[platform_tuple]
        matching = [
            name
            for name in names
            if name.startswith("solstone_core-")
            and name.endswith(f"-py3-none-{tag}.whl")
        ]
        if len(matching) != 1:
            raise RuntimeError(
                f"release_artifacts did not include exactly one core wheel for {tag}"
            )
        targets[matching[0]] = (
            lane,
            {
                "kind": "compiled",
                "triple": PLATFORM_TRIPLES[platform_tuple],
                "profile": "release",
                "features": [],
            },
        )
    return targets


def _rust_artifact_names() -> frozenset[str]:
    return frozenset(rust_artifact_targets())


def _safe_posix_basename(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return False
    if value in {".", ".."}:
        return False
    if "/" in value or "\\" in value:
        return False
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return False
    return True


def _validate_regular_file(path: Path, *, label: str) -> list[Failure]:
    if path.is_symlink():
        return [
            _failure(
                "release file is not a regular non-symlink file",
                expected=f"{label} regular file, not a symlink",
                actual=str(path),
                repair="replace the symlink with the final release artifact file",
            )
        ]
    if not path.exists():
        return [
            _failure(
                "manifest artifact is missing",
                expected=f"{label} exists",
                actual=str(path),
                repair="restore the missing release artifact",
            )
        ]
    if not path.is_file():
        return [
            _failure(
                "release file is not a regular non-symlink file",
                expected=f"{label} regular file",
                actual=str(path),
                repair="remove directories and special files from the release payload",
            )
        ]
    return []


def _file_digest(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _load_manifest_json(path: Path) -> tuple[dict[str, Any] | None, list[Failure]]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw, parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        return None, [
            _failure(
                "manifest JSON is invalid or non-finite",
                expected="strict JSON object without NaN or Infinity",
                actual=str(exc),
                repair="regenerate the Rust release manifest",
            )
        ]
    if not isinstance(payload, dict):
        return None, [
            _failure(
                "manifest JSON is invalid or non-finite",
                expected="JSON object",
                actual=type(payload).__name__,
                repair="regenerate the Rust release manifest",
            )
        ]
    return payload, []


def _validate_payload_schema(
    payload: Mapping[str, Any], schema: Mapping[str, Any]
) -> list[Failure]:
    Draft202012Validator, FormatChecker = _jsonschema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    failures: list[Failure] = []
    for error in sorted(
        validator.iter_errors(payload), key=lambda item: list(item.path)
    ):
        path = ".".join(str(part) for part in error.path) or "<root>"
        failures.append(
            _failure(
                "manifest does not match Rust release manifest schema",
                expected=f"schema-valid field at {path}",
                actual=error.message,
                repair="regenerate the Rust release manifest from validated inputs",
            )
        )
    return failures


def _utc_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not RFC3339_UTC_RE.fullmatch(value):
        return None
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != datetime.min.replace(tzinfo=UTC).utcoffset()
    ):
        return None
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_dependency_policy(payload: Mapping[str, Any]) -> list[Failure]:
    policy = payload.get("dependency_policy")
    if not isinstance(policy, Mapping):
        return []
    failures: list[Failure] = []
    cargo_deny_version = policy.get("cargo_deny_version")
    if isinstance(cargo_deny_version, str):
        failures.extend(
            validate_public_evidence_text("cargo_deny_version", cargo_deny_version)
        )
    if cargo_deny_version != CARGO_DENY_PIN:
        failures.append(
            _failure(
                "cargo_deny_version is not pinned",
                expected=f'cargo_deny_version == "{CARGO_DENY_PIN}"',
                actual="redacted",
                repair="supply the pinned cargo-deny version used for dependency policy",
            )
        )
    if policy.get("deterministic_gate") != "pass":
        failures.append(
            _failure(
                "dependency policy deterministic gate did not pass",
                expected='dependency_policy.deterministic_gate == "pass"',
                actual=repr(policy.get("deterministic_gate")),
                repair="rerun the deterministic dependency gate before generation",
            )
        )
    if _utc_timestamp(policy.get("advisory_checked_at")) is None:
        failures.append(
            _failure(
                "advisory timestamp is not RFC3339 UTC",
                expected='RFC3339 timestamp ending "Z" or "+00:00"',
                actual=repr(policy.get("advisory_checked_at")),
                repair="supply a real UTC advisory_checked_at timestamp",
            )
        )
    return failures


def _validate_source_commit(
    payload: Mapping[str, Any], expected_source_commit: str | None
) -> list[Failure]:
    source_commit = payload.get("source_commit")
    failures: list[Failure] = []
    if not isinstance(source_commit, str) or not SOURCE_COMMIT_RE.fullmatch(
        source_commit
    ):
        failures.append(
            _failure(
                "source_commit is not a full lowercase commit",
                expected="40 or 64 lowercase hexadecimal characters",
                actual=repr(source_commit),
                repair="supply the full source commit",
            )
        )
    if expected_source_commit is not None:
        if not SOURCE_COMMIT_RE.fullmatch(expected_source_commit):
            failures.append(
                _failure(
                    "SOURCE_COMMIT is not a full lowercase commit",
                    expected="40 or 64 lowercase hexadecimal characters",
                    actual=expected_source_commit,
                    repair="set SOURCE_COMMIT to the full public release commit",
                )
            )
        elif source_commit != expected_source_commit:
            failures.append(
                _failure(
                    "source_commit does not match SOURCE_COMMIT",
                    expected=expected_source_commit,
                    actual=repr(source_commit),
                    repair="validate the candidate against the matching public release commit",
                )
            )
    return failures


def _validate_cohort_fields(payload: Mapping[str, Any]) -> list[Failure]:
    failures: list[Failure] = []
    if payload.get("product") != PRODUCT:
        failures.append(
            _failure(
                "product is not solstone-core",
                expected=PRODUCT,
                actual=repr(payload.get("product")),
                repair="regenerate manifests for solstone-core only",
            )
        )
    current_version = _current_version()
    if payload.get("version") != current_version:
        failures.append(
            _failure(
                "version does not match current release metadata",
                expected=current_version,
                actual=repr(payload.get("version")),
                repair="regenerate manifests after rendering packaging metadata",
            )
        )
    if payload.get("source_dirty") is not False:
        failures.append(
            _failure(
                "source_dirty must be false",
                expected="false",
                actual=repr(payload.get("source_dirty")),
                repair="generate manifests only from a clean source tree",
            )
        )
    if payload.get("active_exceptions") != []:
        failures.append(
            _failure(
                "active_exceptions must be empty",
                expected="[]",
                actual=repr(payload.get("active_exceptions")),
                repair="clear dependency-policy exceptions before release generation",
            )
        )
    return failures


def _validate_hash_fields(payload: Mapping[str, Any]) -> list[Failure]:
    failures: list[Failure] = []
    cargo_lock_sha256 = payload.get("cargo_lock_sha256")
    if not isinstance(cargo_lock_sha256, str) or not SHA256_RE.fullmatch(
        cargo_lock_sha256
    ):
        failures.append(
            _failure(
                "sha256 is not lowercase hex",
                expected="cargo_lock_sha256 lowercase 64-character SHA-256",
                actual=repr(cargo_lock_sha256),
                repair="regenerate the manifest with a recomputed SHA-256 digest",
            )
        )
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                continue
            artifact_sha = artifact.get("sha256")
            if not isinstance(artifact_sha, str) or not SHA256_RE.fullmatch(
                artifact_sha
            ):
                failures.append(
                    _failure(
                        "sha256 is not lowercase hex",
                        expected="artifact lowercase 64-character SHA-256",
                        actual=repr(artifact_sha),
                        repair="regenerate the manifest with a recomputed artifact digest",
                    )
                )
    return failures


def _private_ip_present(value: str) -> bool:
    for match in IP_CANDIDATE_RE.finditer(value):
        text = match.group(0)
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
        ):
            return True
    return False


def validate_public_evidence_text(field: str, value: str) -> list[Failure]:
    failures: list[Failure] = []

    def add_failure() -> None:
        failures.append(
            _failure(
                f"{field} contains disallowed content",
                expected=f"{field} public pinned evidence",
                actual="redacted",
                repair="remove non-public evidence from Rust release evidence",
            )
        )

    if CONTROL_RE.search(value.replace("\n", "")):
        add_failure()
    if RAW_ENV_RE.search(value):
        add_failure()
    if SECRET_RE.search(value):
        add_failure()
    if (
        PRIVATE_HOST_RE.search(value)
        or ABSOLUTE_PATH_RE.search(value)
        or _private_ip_present(value)
    ):
        add_failure()
    if EMAIL_RE.search(value):
        add_failure()
    if SIGNER_RE.search(value):
        add_failure()
    if UUID_RE.search(value):
        add_failure()
    return failures


def validate_native_tools(lane: LaneName, tools: Mapping[str, Any]) -> list[Failure]:
    failures: list[Failure] = []
    allowed = NATIVE_TOOL_KEYS[lane]
    keys = set(tools)
    if keys != set(allowed):
        failures.append(
            _failure(
                "native_tools keys do not match lane allowlist",
                expected=", ".join(sorted(allowed)),
                actual=", ".join(sorted(keys)),
                repair="supply exactly the public native tool keys for this lane",
            )
        )
    if lane == "macos-arm64" and tools.get("signing_mode") != "signed-verified":
        failures.append(
            _failure(
                "macOS signing_mode is not signed-verified",
                expected="signed-verified",
                actual=repr(tools.get("signing_mode")),
                repair="supply signing_mode only after signed verification has completed",
            )
        )
    for key, raw_value in sorted(tools.items()):
        if not isinstance(raw_value, str):
            failures.append(
                _failure(
                    "native_tools value is not a normalized public single-line string",
                    expected=f"{key} string value",
                    actual=type(raw_value).__name__,
                    repair="supply public version/status strings only",
                )
            )
            continue
        value = raw_value
        if value != value.strip() or not value or CONTROL_RE.search(value):
            failures.append(
                _failure(
                    "native_tools value is not a normalized public single-line string",
                    expected=f"{key} stripped single-line value without control characters",
                    actual=repr(value),
                    repair="supply normalized public version/status strings only",
                )
            )
            continue
        if RAW_ENV_RE.search(value):
            failures.append(
                _failure(
                    "native_tools value is not a normalized public single-line string",
                    expected=f"{key} public version/status string",
                    actual=value,
                    repair="do not paste raw environment output into native_tools",
                )
            )
        if SECRET_RE.search(value):
            failures.append(
                _failure(
                    "native_tools value contains secret/token canary",
                    expected=f"{key} public version/status string",
                    actual=value,
                    repair="remove secrets and tokens from native_tools evidence",
                )
            )
        if (
            PRIVATE_HOST_RE.search(value)
            or ABSOLUTE_PATH_RE.search(value)
            or _private_ip_present(value)
        ):
            failures.append(
                _failure(
                    "native_tools value contains private host, IP, or path",
                    expected=f"{key} public version/status string",
                    actual=value,
                    repair="remove private machine and filesystem details from native_tools",
                )
            )
        if EMAIL_RE.search(value):
            failures.append(
                _failure(
                    "native_tools value contains email address",
                    expected=f"{key} public version/status string",
                    actual=value,
                    repair="remove account email addresses from native_tools",
                )
            )
        if SIGNER_RE.search(value):
            failures.append(
                _failure(
                    "native_tools value contains signing identity",
                    expected=f"{key} public version/status string",
                    actual=value,
                    repair="record only public signing status, not identities",
                )
            )
        if UUID_RE.search(value):
            failures.append(
                _failure(
                    "native_tools value contains notarization submission ID",
                    expected=f"{key} public version/status string",
                    actual=value,
                    repair="remove notarization submission IDs from native_tools",
                )
            )
    return failures


def _normalize_native_tools(
    lane: LaneName, tools: Mapping[str, str]
) -> tuple[dict[str, str] | None, list[Failure]]:
    normalized = {
        key: value.strip() if isinstance(value, str) else value
        for key, value in tools.items()
    }
    failures = validate_native_tools(lane, normalized)
    if failures:
        return None, failures
    return dict(normalized), []


def parse_rustc_verbose(text: Any) -> tuple[RustcVerbose | None, list[Failure]]:
    def malformed(expected: str) -> tuple[None, list[Failure]]:
        return None, [
            _failure(
                "rustc_verbose is malformed",
                expected=expected,
                actual="redacted",
                repair="supply the pinned rustc -Vv output for the lane",
            )
        ]

    if not isinstance(text, str) or not text:
        return malformed("pinned rustc -Vv output")
    if text != text.strip() or "\r" in text or "\t" in text or "\x00" in text:
        return malformed(
            "pinned rustc -Vv output without surrounding or control whitespace"
        )
    lines = text.split("\n")
    if len(lines) != 7 or any(not line or CONTROL_RE.search(line) for line in lines):
        return malformed("exactly 7 pinned rustc -Vv lines")
    if lines[0] != RUSTC_VERSION_BANNER:
        return malformed(RUSTC_VERSION_BANNER)

    labels: dict[str, str] = {}
    expected_labels: tuple[tuple[str, str | None], ...] = (
        ("binary", RUSTC_BINARY_PIN),
        ("commit-hash", RUSTC_COMMIT_HASH_PIN),
        ("commit-date", RUSTC_COMMIT_DATE_PIN),
        ("host", None),
        ("release", RUSTC_RELEASE_PIN),
        ("LLVM version", RUSTC_LLVM_PIN),
    )
    host: str | None = None
    for line, (expected_label, pinned_value) in zip(
        lines[1:], expected_labels, strict=True
    ):
        if pinned_value is None:
            if not line.startswith("host: "):
                return malformed("host: build host")
            value = line[len("host: ") :]
            if not value or value != value.strip():
                return malformed("host: build host")
            labels[expected_label] = value
            host = value
            continue

        if line != f"{expected_label}: {pinned_value}":
            return malformed(f"{expected_label}: pinned value")
        labels[expected_label] = pinned_value
    if host is None:
        return malformed("host: build host")
    return (
        RustcVerbose(
            first_line=RUSTC_VERSION_BANNER,
            release=RUSTC_RELEASE_PIN,
            commit_hash=RUSTC_COMMIT_HASH_PIN,
            commit_date=RUSTC_COMMIT_DATE_PIN,
            host=host,
            labels=labels,
        ),
        [],
    )


def _cargo_release(value: Any) -> tuple[str | None, list[Failure]]:
    if not isinstance(value, str):
        return None, [
            _failure(
                "cargo_version is malformed",
                expected=CARGO_VERSION_PIN,
                actual="redacted",
                repair="supply complete cargo --version output",
            )
        ]
    if value != CARGO_VERSION_PIN:
        return None, [
            _failure(
                "cargo_version is malformed",
                expected=CARGO_VERSION_PIN,
                actual="redacted",
                repair="supply complete cargo --version output",
            )
        ]
    return CARGO_RELEASE_PIN, []


def _validate_rust_for_lane(
    lane: LaneName, rust: Mapping[str, Any]
) -> tuple[RustcVerbose | None, str | None, list[Failure]]:
    rustc_value = rust.get("rustc_verbose")
    cargo_value = rust.get("cargo_version")
    failures: list[Failure] = []
    if isinstance(rustc_value, str):
        failures.extend(validate_public_evidence_text("rustc_verbose", rustc_value))
    if isinstance(cargo_value, str):
        failures.extend(validate_public_evidence_text("cargo_version", cargo_value))
    rustc, rustc_failures = parse_rustc_verbose(rustc_value)
    cargo, cargo_failures = _cargo_release(cargo_value)
    failures.extend(rustc_failures)
    failures.extend(cargo_failures)
    if rustc is not None and rustc.host != LANE_HOSTS[lane]:
        failures.append(
            _failure(
                "rustc host is not an allowed build host",
                expected=f"{lane} host {LANE_HOSTS[lane]}",
                actual="redacted",
                repair="supply the rustc -Vv output from the correct build lane",
            )
        )
    return rustc, cargo, failures


def _validate_rust_evidence_cohort(records: Sequence[ManifestRecord]) -> list[Failure]:
    failures: list[Failure] = []
    if not records:
        return failures
    first = records[0]
    first_labels = {
        key: value for key, value in first.rustc.labels.items() if key != "host"
    }
    cargo_release = first.cargo_release
    for record in records[1:]:
        labels = {
            key: value for key, value in record.rustc.labels.items() if key != "host"
        }
        if labels != first_labels:
            failures.append(
                _failure(
                    "Rust evidence differs outside permitted host field",
                    expected=str(first_labels),
                    actual=str(labels),
                    repair="regenerate all four manifests from the same pinned Rust toolchain",
                )
            )
        if record.cargo_release != cargo_release:
            failures.append(
                _failure(
                    "Cargo release differs across lanes",
                    expected=cargo_release,
                    actual=record.cargo_release,
                    repair="regenerate all four manifests from the same pinned Cargo release",
                )
            )
    return failures


def _validate_target_for_artifact(
    artifact_name: str, target: Mapping[str, Any]
) -> tuple[LaneName | None, list[Failure]]:
    if not isinstance(target, Mapping):
        return None, [
            _failure(
                "artifact target does not match release lane",
                expected="target object",
                actual=type(target).__name__,
                repair="regenerate the manifest with the authoritative artifact target mapping",
            )
        ]
    targets = rust_artifact_targets()
    expected = targets.get(artifact_name)
    if expected is None:
        if artifact_name in expected_package_names(include_models=True):
            return None, [
                _failure(
                    "manifest covers a non-Rust release artifact",
                    expected="manifest artifact is one of the four solstone_core artifacts",
                    actual=artifact_name,
                    repair="remove manifests for pure/root/journal/CUDA/models artifacts",
                )
            ]
        return None, [
            _failure(
                "manifest artifact is not a recognized Rust-bearing artifact",
                expected=", ".join(sorted(targets)),
                actual=artifact_name,
                repair="regenerate the companion manifest beside a solstone_core artifact",
            )
        ]
    lane, expected_target = expected
    if dict(target) != expected_target:
        return lane, [
            _failure(
                "artifact target does not match release lane",
                expected=json.dumps(expected_target, sort_keys=True),
                actual=json.dumps(target, sort_keys=True),
                repair="regenerate the manifest with the authoritative artifact target mapping",
            )
        ]
    return lane, []


def _validate_artifact_record(
    manifest_path: Path, payload: Mapping[str, Any]
) -> tuple[str | None, list[Failure]]:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        return None, [
            _failure(
                "manifest must contain exactly one artifact",
                expected="one artifact entry",
                actual=repr(artifacts),
                repair="regenerate one companion manifest per Rust artifact",
            )
        ]
    artifact = artifacts[0]
    if not isinstance(artifact, Mapping):
        return None, [
            _failure(
                "manifest must contain exactly one artifact",
                expected="artifact object",
                actual=type(artifact).__name__,
                repair="regenerate one companion manifest per Rust artifact",
            )
        ]
    path_value = artifact.get("path")
    if not _safe_posix_basename(path_value):
        return None, [
            _failure(
                "artifact path is not a safe relative basename",
                expected="POSIX relative basename without slash, backslash, drive, or traversal",
                actual=repr(path_value),
                repair="regenerate manifests with artifact basenames only",
            )
        ]
    artifact_name = str(path_value)
    expected_manifest_name = f"{artifact_name}.rust-release-manifest.json"
    failures: list[Failure] = []
    if manifest_path.name != expected_manifest_name:
        failures.append(
            _failure(
                "manifest filename is not the artifact companion name",
                expected=expected_manifest_name,
                actual=manifest_path.name,
                repair="rename the manifest to the artifact basename plus .rust-release-manifest.json",
            )
        )
    artifact_path = manifest_path.parent / artifact_name
    regular_failures = _validate_regular_file(artifact_path, label=artifact_name)
    failures.extend(regular_failures)
    if regular_failures:
        return artifact_name, failures
    digest, byte_count = _file_digest(artifact_path)
    if byte_count < 1:
        failures.append(
            _failure(
                "manifest artifact is empty",
                expected="artifact byte count greater than zero",
                actual="0",
                repair="replace the empty artifact with final release bytes",
            )
        )
    if artifact.get("bytes") != byte_count:
        failures.append(
            _failure(
                "artifact byte count does not match manifest",
                expected=str(byte_count),
                actual=repr(artifact.get("bytes")),
                repair="regenerate the manifest after final artifact bytes are present",
            )
        )
    if artifact.get("sha256") != digest:
        failures.append(
            _failure(
                "artifact sha256 does not match manifest",
                expected=digest,
                actual=repr(artifact.get("sha256")),
                repair="regenerate the manifest after final artifact bytes are present",
            )
        )
    return artifact_name, failures


def _validate_manifest_record(
    manifest_path: Path,
    *,
    expected_source_commit: str | None,
    schema: Mapping[str, Any],
) -> tuple[ManifestRecord | None, list[Failure]]:
    failures = _validate_regular_file(manifest_path, label="manifest")
    if failures:
        return None, failures
    payload, load_failures = _load_manifest_json(manifest_path)
    if payload is None:
        return None, load_failures
    failures.extend(_validate_payload_schema(payload, schema))
    failures.extend(_validate_dependency_policy(payload))
    failures.extend(_validate_source_commit(payload, expected_source_commit))
    failures.extend(_validate_cohort_fields(payload))
    failures.extend(_validate_hash_fields(payload))
    artifact_name, artifact_failures = _validate_artifact_record(manifest_path, payload)
    failures.extend(artifact_failures)
    lane: LaneName | None = None
    if artifact_name is not None:
        lane, target_failures = _validate_target_for_artifact(
            artifact_name, payload.get("target", {})
        )
        failures.extend(target_failures)
    rustc: RustcVerbose | None = None
    cargo_release: str | None = None
    if lane is not None and isinstance(payload.get("rust"), Mapping):
        rustc, cargo_release, rust_failures = _validate_rust_for_lane(
            lane, payload["rust"]
        )
        failures.extend(rust_failures)
    if lane is not None and isinstance(payload.get("native_tools"), Mapping):
        failures.extend(validate_native_tools(lane, payload["native_tools"]))
    if artifact_name is None or lane is None or rustc is None or cargo_release is None:
        return None, failures
    record = ManifestRecord(
        path=manifest_path,
        payload=payload,
        artifact_name=artifact_name,
        lane=lane,
        rustc=rustc,
        cargo_release=cargo_release,
    )
    return record, failures


def cohort_key(manifest: Mapping[str, Any]) -> tuple[Any, ...]:
    policy = manifest.get("dependency_policy")
    if not isinstance(policy, Mapping):
        policy = {}
    active = manifest.get("active_exceptions")
    if not isinstance(active, list):
        active = []
    return (
        manifest.get("product"),
        manifest.get("version"),
        manifest.get("source_commit"),
        manifest.get("source_dirty"),
        manifest.get("cargo_lock_sha256"),
        policy.get("cargo_deny_version"),
        policy.get("deterministic_gate"),
        policy.get("advisory_checked_at"),
        tuple(active),
    )


def _validate_manifest_inventory(records: Sequence[ManifestRecord]) -> list[Failure]:
    failures: list[Failure] = []
    coverage: dict[str, list[Path]] = {}
    for record in records:
        coverage.setdefault(record.artifact_name, []).append(record.path)
    for artifact_name, manifests in sorted(coverage.items()):
        if len(manifests) > 1:
            failures.append(
                _failure(
                    "Rust artifact is covered by multiple manifests",
                    expected=f"one manifest for {artifact_name}",
                    actual=", ".join(path.name for path in manifests),
                    repair="remove duplicate companion manifests",
                )
            )
    expected = _rust_artifact_names()
    actual = frozenset(coverage)
    for missing in sorted(expected - actual):
        failures.append(
            _failure(
                "Rust artifact is not covered by any manifest",
                expected=missing,
                actual="missing",
                repair="generate a companion manifest for every solstone_core artifact",
            )
        )
    for extra in sorted(actual - expected):
        failures.append(
            _failure(
                "manifest artifact is not a recognized Rust-bearing artifact",
                expected=", ".join(sorted(expected)),
                actual=extra,
                repair="remove manifests for non-release Rust artifacts",
            )
        )
    if records:
        first = cohort_key(records[0].payload)
        for record in records[1:]:
            key = cohort_key(record.payload)
            if key != first:
                failures.append(
                    _failure(
                        "Rust release manifests do not agree on cohort fields",
                        expected=str(first),
                        actual=str(key),
                        repair="regenerate all four manifests from the same release inputs",
                    )
                )
    failures.extend(_validate_rust_evidence_cohort(records))
    return failures


def validate_manifest_file(
    manifest_path: Path,
    *,
    expected_source_commit: str | None = None,
    schema_path: Path = SCHEMA_PATH,
) -> list[Failure]:
    try:
        schema = load_schema(schema_path)
    except Exception as exc:
        return [_schema_load_failure(exc)]
    _record, failures = _validate_manifest_record(
        manifest_path,
        expected_source_commit=expected_source_commit,
        schema=schema,
    )
    return failures


def _case_collision_failures(paths: Sequence[Path]) -> list[Failure]:
    buckets: dict[str, list[str]] = {}
    for path in paths:
        buckets.setdefault(path.name.casefold(), []).append(path.name)
    failures: list[Failure] = []
    for names in buckets.values():
        if len(names) > 1:
            failures.append(
                _failure(
                    "release directory contains case-colliding filenames",
                    expected="unique filenames under casefold()",
                    actual=", ".join(sorted(names)),
                    repair="remove case-colliding files from the release payload",
                )
            )
    return failures


def _model_name_failures(package_names: set[str], expected_count: int) -> list[Failure]:
    model_like = {
        name for name in package_names if name.startswith("solstone_journal_models-")
    }
    expected_models = _models_expected_names()
    failures: list[Failure] = []
    if len(model_like) == 1:
        failures.append(
            _failure(
                "release directory contains exactly one models archive",
                expected="zero models archives or the exact sdist+wheel pair",
                actual=", ".join(sorted(model_like)),
                repair="remove the leftover model archive or include the complete pair",
            )
        )
    if model_like and model_like != expected_models:
        failures.append(
            _failure(
                "models archive names do not match current models version pair",
                expected=", ".join(sorted(expected_models)),
                actual=", ".join(sorted(model_like)),
                repair="use the models version derived from package metadata",
            )
        )
    if expected_count == 15 and model_like:
        failures.append(
            _failure(
                "15-file candidate contains models archive leftover",
                expected="no solstone_journal_models archives in a 15-file candidate",
                actual=", ".join(sorted(model_like)),
                repair="remove skipped models artifacts from the release candidate",
            )
        )
    return failures


def classify_release_dir(
    release_dir: Path,
    *,
    expected_source_commit: str,
    schema_path: Path = SCHEMA_PATH,
) -> tuple[ReleaseInventory | None, list[Failure]]:
    failures: list[Failure] = []
    if not SOURCE_COMMIT_RE.fullmatch(expected_source_commit):
        failures.append(
            _failure(
                "SOURCE_COMMIT is not a full lowercase commit",
                expected="40 or 64 lowercase hexadecimal characters",
                actual=expected_source_commit,
                repair="set SOURCE_COMMIT to the full public release commit",
            )
        )
    if not release_dir.exists() or not release_dir.is_dir():
        failures.append(
            _failure(
                "release directory is missing",
                expected="existing release payload directory",
                actual=str(release_dir),
                repair="set RELEASE_DIR to the exact package-and-manifest payload directory",
            )
        )
        return None, failures
    entries = sorted(release_dir.iterdir(), key=lambda path: path.name)
    failures.extend(_case_collision_failures(entries))
    for entry in entries:
        failures.extend(_validate_regular_file(entry, label=entry.name))
    if len(entries) not in {15, 17}:
        failures.append(
            _failure(
                "release directory must contain exactly 15 or 17 files",
                expected="15 files without models or 17 files with models",
                actual=str(len(entries)),
                repair="validate the exact release candidate payload directory",
            )
        )
    manifest_paths = [
        path for path in entries if path.name.endswith(".rust-release-manifest.json")
    ]
    package_names = {path.name for path in entries if path not in manifest_paths}
    if len(manifest_paths) != 4:
        failures.append(
            _failure(
                "release directory must contain exactly four Rust manifests",
                expected="4 companion .rust-release-manifest.json files",
                actual=str(len(manifest_paths)),
                repair="generate one companion manifest for each solstone_core artifact",
            )
        )
    expected_without_models = set(expected_package_names(include_models=False))
    expected_with_models = set(expected_package_names(include_models=True))
    include_models = len(entries) == 17
    expected_packages = (
        expected_with_models if include_models else expected_without_models
    )
    failures.extend(_model_name_failures(package_names, len(entries)))
    unknown = package_names - expected_with_models
    if unknown:
        failures.append(
            _failure(
                "release directory contains unknown asset",
                expected="only current release package archives and Rust manifests",
                actual=", ".join(sorted(unknown)),
                repair="remove unknown assets from RELEASE_DIR",
            )
        )
    missing = expected_packages - package_names
    if missing:
        failures.append(
            _failure(
                "release directory is missing required assets",
                expected=", ".join(sorted(missing)),
                actual="missing",
                repair="copy the complete current-only all-hosts release package set",
            )
        )
    extra = package_names - expected_packages - unknown
    if extra:
        failures.append(
            _failure(
                "release directory contains extra assets",
                expected=", ".join(sorted(expected_packages)),
                actual=", ".join(sorted(extra)),
                repair="remove assets outside the exact 15/17-file release payload",
            )
        )
    try:
        schema = load_schema(schema_path)
    except Exception as exc:
        failures.append(_schema_load_failure(exc))
        schema = None
    records: list[ManifestRecord] = []
    if schema is not None:
        for manifest_path in manifest_paths:
            record, record_failures = _validate_manifest_record(
                manifest_path,
                expected_source_commit=expected_source_commit,
                schema=schema,
            )
            failures.extend(record_failures)
            if record is not None:
                records.append(record)
    failures.extend(_validate_manifest_inventory(records))
    if failures:
        return None, failures
    return (
        ReleaseInventory(
            package_names=tuple(sorted(package_names)),
            manifest_records=tuple(
                sorted(records, key=lambda record: record.path.name)
            ),
            include_models=include_models,
        ),
        [],
    )


def validate_release_dir(
    release_dir: Path,
    *,
    expected_source_commit: str,
    schema_path: Path = SCHEMA_PATH,
) -> list[Failure]:
    _inventory, failures = classify_release_dir(
        release_dir,
        expected_source_commit=expected_source_commit,
        schema_path=schema_path,
    )
    return failures


def _canonicalize(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return {
            str(item_key): _canonicalize(item_value, key=str(item_key))
            for item_key, item_value in sorted(
                value.items(), key=lambda item: str(item[0])
            )
        }
    if isinstance(value, list):
        items = [_canonicalize(item) for item in value]
        if key in ORDER_INDEPENDENT_LIST_KEYS:
            return sorted(items)
        return items
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("manifest JSON contains non-finite number")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            _canonicalize(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except ValueError as exc:
        raise ValueError("manifest JSON contains non-finite number") from exc
    return text.encode("utf-8") + b"\n"


def _cohort_failures(cohort: CohortInputs) -> list[Failure]:
    failures: list[Failure] = []
    if cohort.product != PRODUCT:
        failures.append(
            _failure(
                "product is not solstone-core",
                expected=PRODUCT,
                actual=cohort.product,
                repair="generate manifests only for solstone-core",
            )
        )
    if cohort.version != _current_version():
        failures.append(
            _failure(
                "version does not match current release metadata",
                expected=_current_version(),
                actual=cohort.version,
                repair="derive the version from package metadata",
            )
        )
    if not SOURCE_COMMIT_RE.fullmatch(cohort.source_commit):
        failures.append(
            _failure(
                "source_commit is not a full lowercase commit",
                expected="40 or 64 lowercase hexadecimal characters",
                actual=cohort.source_commit,
                repair="supply the full source commit",
            )
        )
    if cohort.source_dirty is not False:
        failures.append(
            _failure(
                "source_dirty must be false",
                expected="false",
                actual=repr(cohort.source_dirty),
                repair="generate manifests only from clean release inputs",
            )
        )
    if cohort.active_exceptions:
        failures.append(
            _failure(
                "active_exceptions must be empty",
                expected="()",
                actual=repr(cohort.active_exceptions),
                repair="clear dependency-policy exceptions before generation",
            )
        )
    if cohort.deterministic_gate != "pass":
        failures.append(
            _failure(
                "dependency policy deterministic gate did not pass",
                expected="pass",
                actual=cohort.deterministic_gate,
                repair="rerun the deterministic dependency gate",
            )
        )
    return failures


def generate_manifest(
    artifact_path: Path,
    *,
    lane: LaneName,
    evidence: LaneEvidence,
    cohort: CohortInputs,
    cargo_lock_path: Path = ROOT / "core" / "Cargo.lock",
    schema_path: Path = SCHEMA_PATH,
) -> tuple[GeneratedManifest | None, list[Failure]]:
    failures = _cohort_failures(cohort)
    targets = rust_artifact_targets()
    artifact_name = artifact_path.name
    expected = targets.get(artifact_name)
    if expected is None:
        failures.append(
            _failure(
                "manifest artifact is not a recognized Rust-bearing artifact",
                expected=", ".join(sorted(targets)),
                actual=artifact_name,
                repair="generate manifests only for the four solstone_core release artifacts",
            )
        )
    elif expected[0] != lane:
        failures.append(
            _failure(
                "artifact target does not match release lane",
                expected=expected[0],
                actual=lane,
                repair="call generate_manifest with the artifact's matching lane",
            )
        )
    failures.extend(_validate_regular_file(artifact_path, label=artifact_name))
    if not cargo_lock_path.is_file() or cargo_lock_path.is_symlink():
        failures.append(
            _failure(
                "Cargo.lock is not a regular file",
                expected=str(cargo_lock_path),
                actual="missing or non-regular",
                repair="supply the core/Cargo.lock path for this source tree",
            )
        )
    normalized_tools, tool_failures = _normalize_native_tools(
        lane, evidence.native_tools
    )
    failures.extend(tool_failures)
    rustc, cargo_release, rust_failures = _validate_rust_for_lane(
        lane,
        {
            "rustc_verbose": evidence.rustc_verbose,
            "cargo_version": evidence.cargo_version,
        },
    )
    failures.extend(rust_failures)
    advisory = _utc_timestamp(evidence.advisory_checked_at)
    if advisory is None:
        failures.append(
            _failure(
                "advisory timestamp is not RFC3339 UTC",
                expected='RFC3339 timestamp ending "Z" or "+00:00"',
                actual=evidence.advisory_checked_at,
                repair="supply a real UTC advisory_checked_at timestamp",
            )
        )
    if isinstance(evidence.cargo_deny_version, str):
        failures.extend(
            validate_public_evidence_text(
                "cargo_deny_version", evidence.cargo_deny_version
            )
        )
    if (
        not isinstance(evidence.cargo_deny_version, str)
        or evidence.cargo_deny_version != CARGO_DENY_PIN
    ):
        failures.append(
            _failure(
                "cargo_deny_version is not pinned",
                expected=f'cargo_deny_version == "{CARGO_DENY_PIN}"',
                actual="redacted",
                repair="supply the pinned cargo-deny version used for dependency policy",
            )
        )
    try:
        schema = load_schema(schema_path)
    except Exception as exc:
        failures.append(_schema_load_failure(exc))
        schema = None
    if (
        failures
        or expected is None
        or normalized_tools is None
        or rustc is None
        or cargo_release is None
        or advisory is None
        or schema is None
    ):
        return None, failures
    artifact_sha256, artifact_size = _file_digest(artifact_path)
    cargo_lock_sha256 = hashlib.sha256(cargo_lock_path.read_bytes()).hexdigest()
    target = expected[1]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "product": cohort.product,
        "version": cohort.version,
        "source_commit": cohort.source_commit,
        "source_dirty": cohort.source_dirty,
        "cargo_lock_sha256": cargo_lock_sha256,
        "rust": {
            "rustc_verbose": evidence.rustc_verbose,
            "cargo_version": evidence.cargo_version,
        },
        "target": target,
        "native_tools": normalized_tools,
        "dependency_policy": {
            "cargo_deny_version": evidence.cargo_deny_version,
            "deterministic_gate": cohort.deterministic_gate,
            "advisory_checked_at": advisory,
        },
        "active_exceptions": list(cohort.active_exceptions),
        "artifacts": [
            {
                "path": artifact_name,
                "sha256": artifact_sha256,
                "bytes": artifact_size,
            }
        ],
    }
    schema_failures = _validate_payload_schema(payload, schema)
    if schema_failures:
        return None, schema_failures
    try:
        manifest_bytes = canonical_json_bytes(payload)
    except ValueError as exc:
        return None, [
            _failure(
                "manifest JSON contains non-finite number",
                expected="finite JSON values only",
                actual=str(exc),
                repair="remove non-finite values before generation",
            )
        ]
    return (
        GeneratedManifest(
            artifact_name=artifact_name,
            manifest_name=f"{artifact_name}.rust-release-manifest.json",
            payload=payload,
            bytes=manifest_bytes,
        ),
        [],
    )


def _copy_selected_packages(
    source_dist_dir: Path, staging: Path, *, include_models: bool
) -> list[Failure]:
    failures: list[Failure] = []
    for name in expected_package_names(include_models=include_models):
        source = source_dist_dir / name
        regular_failures = _validate_regular_file(source, label=name)
        failures.extend(regular_failures)
        if regular_failures:
            continue
        _digest, byte_count = _file_digest(source)
        if byte_count < 1:
            failures.append(
                _failure(
                    "manifest artifact is empty",
                    expected=f"{name} byte count greater than zero",
                    actual="0",
                    repair="replace the empty package artifact with final release bytes",
                )
            )
            continue
        shutil.copy2(source, staging / name)
    return failures


def _default_cohort(source_commit: str) -> CohortInputs:
    return CohortInputs(
        product=PRODUCT,
        version=_current_version(),
        source_commit=source_commit,
        source_dirty=False,
        active_exceptions=(),
    )


def _final_validate_release_dir(
    release_dir: Path,
    *,
    expected_source_commit: str | None,
    schema_path: Path = SCHEMA_PATH,
) -> list[Failure]:
    return validate_release_dir(
        release_dir,
        expected_source_commit=expected_source_commit,
        schema_path=schema_path,
    )


def _quarantine_and_remove(ready_path: Path, quarantine: Path) -> Failure | None:
    os.rename(ready_path, quarantine)
    try:
        shutil.rmtree(quarantine)
    except Exception:
        return _failure(
            "release candidate quarantine could not be removed",
            expected="quarantine removed after failed promotion",
            actual="quarantine remains",
            repair="inspect and remove the quarantine directory before retrying",
        )
    return None


def build_and_promote_candidate(
    source_dist_dir: Path,
    ready_path: Path,
    *,
    source_commit: str,
    evidence_by_lane: Mapping[LaneName, LaneEvidence],
    include_models: bool,
    cargo_lock_path: Path = ROOT / "core" / "Cargo.lock",
    schema_path: Path = SCHEMA_PATH,
    _post_promote_hook: Callable[[Path], None] | None = None,
) -> list[Failure]:
    failures: list[Failure] = []
    if set(evidence_by_lane) != set(LANES):
        failures.append(
            _failure(
                "lane evidence keys do not match required lanes",
                expected=", ".join(LANES),
                actual=", ".join(sorted(evidence_by_lane)),
                repair="supply injected evidence for all four release lanes",
            )
        )
    if not SOURCE_COMMIT_RE.fullmatch(source_commit):
        failures.append(
            _failure(
                "SOURCE_COMMIT is not a full lowercase commit",
                expected="40 or 64 lowercase hexadecimal characters",
                actual=source_commit,
                repair="set SOURCE_COMMIT to the full public release commit",
            )
        )
    parent = ready_path.parent
    if not parent.is_dir():
        failures.append(
            _failure(
                "release root is not a directory",
                expected=f"existing parent directory {parent}",
                actual=str(parent),
                repair="create the release root before promotion",
            )
        )
    if ready_path.exists() or ready_path.is_symlink():
        failures.append(
            _failure(
                "ready path already exists",
                expected=f"absent ready path {ready_path}",
                actual="present",
                repair="choose an absent ready path or remove the stale candidate manually",
            )
        )
    staging = parent / f"{ready_path.name}.staging"
    if staging.exists() or staging.is_symlink():
        failures.append(
            _failure(
                "staging directory already exists",
                expected=f"absent staging directory {staging}",
                actual="present",
                repair="remove the stale staging directory after inspecting it",
            )
        )
    quarantine = parent / f"{ready_path.name}.quarantine"
    if quarantine.exists() or quarantine.is_symlink():
        failures.append(
            _failure(
                "quarantine directory already exists",
                expected=f"absent quarantine directory {quarantine}",
                actual="present",
                repair="remove the stale quarantine directory after inspecting it",
            )
        )
    if failures:
        return failures
    lock_path = parent / ".rust-release-candidate.lock"
    lock_file = lock_path.open("a+")
    promoted = False
    succeeded = False
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return [
                _failure(
                    "release candidate lock is already held",
                    expected=f"exclusive lock {lock_path}",
                    actual="contended",
                    repair="wait for the active release-candidate operation to finish",
                )
            ]
        if ready_path.exists() or ready_path.is_symlink():
            return [
                _failure(
                    "ready path already exists",
                    expected=f"absent ready path {ready_path}",
                    actual="present",
                    repair="choose an absent ready path or remove the stale candidate manually",
                )
            ]
        if staging.exists() or staging.is_symlink():
            return [
                _failure(
                    "staging directory already exists",
                    expected=f"absent staging directory {staging}",
                    actual="present",
                    repair="remove the stale staging directory after inspecting it",
                )
            ]
        if quarantine.exists() or quarantine.is_symlink():
            return [
                _failure(
                    "quarantine directory already exists",
                    expected=f"absent quarantine directory {quarantine}",
                    actual="present",
                    repair="remove the stale quarantine directory after inspecting it",
                )
            ]
        staging.mkdir()
        failures = _copy_selected_packages(
            source_dist_dir, staging, include_models=include_models
        )
        if not failures:
            cohort = _default_cohort(source_commit)
            for artifact_name, (lane, _target) in sorted(
                rust_artifact_targets().items()
            ):
                generated, manifest_failures = generate_manifest(
                    staging / artifact_name,
                    lane=lane,
                    evidence=evidence_by_lane[lane],
                    cohort=cohort,
                    cargo_lock_path=cargo_lock_path,
                    schema_path=schema_path,
                )
                failures.extend(manifest_failures)
                if generated is not None:
                    (staging / generated.manifest_name).write_bytes(generated.bytes)
        if not failures:
            failures = validate_release_dir(
                staging,
                expected_source_commit=source_commit,
                schema_path=schema_path,
            )
        if failures:
            shutil.rmtree(staging, ignore_errors=True)
            return failures
        os.rename(staging, ready_path)
        promoted = True
        try:
            if _post_promote_hook is not None:
                _post_promote_hook(ready_path)
            failures = _final_validate_release_dir(
                ready_path,
                expected_source_commit=source_commit,
                schema_path=schema_path,
            )
        except BaseException as exc:
            try:
                residual = _quarantine_and_remove(ready_path, quarantine)
            except BaseException as cleanup_exc:
                raise cleanup_exc from exc
            if residual is not None:
                raise RuntimeError(
                    "release candidate quarantine could not be removed"
                ) from exc
            raise
        if failures:
            residual = _quarantine_and_remove(ready_path, quarantine)
            if residual is not None:
                failures = [*failures, residual]
            return failures
        succeeded = True
        return []
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()
        if not succeeded and not promoted and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _cargo_lock_path_for_fixture(root: Path) -> Path:
    lock = root / "Cargo.lock"
    lock.write_text("fixture cargo lock\n", encoding="utf-8")
    return lock


def fixture_source_commit() -> str:
    return "a" * 40


def fixture_rustc_verbose(host: str) -> str:
    return "\n".join(
        [
            RUSTC_VERSION_BANNER,
            f"binary: {RUSTC_BINARY_PIN}",
            f"commit-hash: {RUSTC_COMMIT_HASH_PIN}",
            f"commit-date: {RUSTC_COMMIT_DATE_PIN}",
            f"host: {host}",
            f"release: {RUSTC_RELEASE_PIN}",
            f"LLVM version: {RUSTC_LLVM_PIN}",
        ]
    )


def fixture_evidence_by_lane() -> dict[LaneName, LaneEvidence]:
    return {
        "source": LaneEvidence(
            rustc_verbose=fixture_rustc_verbose("x86_64-unknown-linux-gnu"),
            cargo_version=CARGO_VERSION_PIN,
            native_tools=fixture_native_tools("source"),
            cargo_deny_version=CARGO_DENY_PIN,
            advisory_checked_at="2026-07-20T00:00:00Z",
        ),
        "linux-x86_64-musl": LaneEvidence(
            rustc_verbose=fixture_rustc_verbose("x86_64-unknown-linux-gnu"),
            cargo_version=CARGO_VERSION_PIN,
            native_tools=fixture_native_tools("linux-x86_64-musl"),
            cargo_deny_version=CARGO_DENY_PIN,
            advisory_checked_at="2026-07-20T00:00:00Z",
        ),
        "linux-aarch64-musl": LaneEvidence(
            rustc_verbose=fixture_rustc_verbose("x86_64-unknown-linux-gnu"),
            cargo_version=CARGO_VERSION_PIN,
            native_tools=fixture_native_tools("linux-aarch64-musl"),
            cargo_deny_version=CARGO_DENY_PIN,
            advisory_checked_at="2026-07-20T00:00:00Z",
        ),
        "macos-arm64": LaneEvidence(
            rustc_verbose=fixture_rustc_verbose("aarch64-apple-darwin"),
            cargo_version=CARGO_VERSION_PIN,
            native_tools=fixture_native_tools("macos-arm64"),
            cargo_deny_version=CARGO_DENY_PIN,
            advisory_checked_at="2026-07-20T00:00:00Z",
        ),
    }


def write_inert_packages(dist_dir: Path, *, include_models: bool) -> None:
    dist_dir.mkdir(parents=True, exist_ok=True)
    core_wheel_names = {
        name
        for name, (lane, _target) in rust_artifact_targets().items()
        if lane != "source"
    }

    def metadata_member(name: str) -> tuple[str, str]:
        parts = name.removesuffix(".whl").split("-")
        distribution = parts[0]
        version = parts[1]
        dist_info = f"{distribution}-{version}.dist-info/METADATA"
        metadata = f"Name: {distribution.replace('_', '-')}\nVersion: {version}\n"
        return dist_info, metadata

    for name in expected_package_names(include_models=include_models):
        path = dist_dir / name
        if name in core_wheel_names:
            info = zipfile.ZipInfo(
                f"{name.removesuffix('.whl')}.data/scripts/solstone-core"
            )
            info.create_system = 3
            info.external_attr = 0o755 << 16
            with zipfile.ZipFile(path, "w") as wheel:
                meta_name, metadata = metadata_member(name)
                wheel.writestr(meta_name, metadata)
                wheel.writestr(info, f"inert solstone-core for {name}\n")
            continue
        if (
            name.startswith("solstone-")
            and name.endswith(".whl")
            and "macosx_14_0_arm64" in name
        ):
            info = zipfile.ZipInfo(
                "solstone/observe/transcribe/parakeet_helper/_bin/parakeet-helper"
            )
            info.create_system = 3
            info.external_attr = 0o755 << 16
            with zipfile.ZipFile(path, "w") as wheel:
                meta_name, metadata = metadata_member(name)
                wheel.writestr(meta_name, metadata)
                wheel.writestr(info, f"inert parakeet-helper for {name}\n")
            continue
        if name.endswith(".whl"):
            with zipfile.ZipFile(path, "w") as wheel:
                meta_name, metadata = metadata_member(name)
                wheel.writestr(meta_name, metadata)
            continue
        path.write_bytes(f"inert bytes for {name}\n".encode("utf-8"))


def write_inert_candidate(
    release_dir: Path,
    *,
    include_models: bool,
    source_commit: str = fixture_source_commit(),
    evidence_by_lane: Mapping[LaneName, LaneEvidence] | None = None,
    cargo_lock_path: Path | None = None,
) -> list[Failure]:
    release_dir.mkdir(parents=True, exist_ok=True)
    write_inert_packages(release_dir, include_models=include_models)
    evidence = evidence_by_lane or fixture_evidence_by_lane()
    lock_path = cargo_lock_path or ROOT / "core" / "Cargo.lock"
    cohort = _default_cohort(source_commit)
    failures: list[Failure] = []
    for artifact_name, (lane, _target) in sorted(rust_artifact_targets().items()):
        generated, manifest_failures = generate_manifest(
            release_dir / artifact_name,
            lane=lane,
            evidence=evidence[lane],
            cohort=cohort,
            cargo_lock_path=lock_path,
        )
        failures.extend(manifest_failures)
        if generated is not None:
            (release_dir / generated.manifest_name).write_bytes(generated.bytes)
    return failures


def _assert_failure(failures: Sequence[Failure], error: str) -> list[Failure]:
    if any(failure.error == error for failure in failures):
        return []
    return [
        _failure(
            "fixtures mode did not observe expected failure",
            expected=error,
            actual=", ".join(failure.error for failure in failures) or "no failures",
            repair="fix the Rust release manifest fixtures",
        )
    ]


def run_fixtures_mode() -> list[Failure]:
    with tempfile.TemporaryDirectory(prefix="solstone-rust-manifest-") as tmp:
        root = Path(tmp)
        source_commit = fixture_source_commit()
        cargo_lock_path = _cargo_lock_path_for_fixture(root)
        source_dist = root / "dist"
        write_inert_packages(source_dist, include_models=False)
        ready = root / "ready"
        failures = build_and_promote_candidate(
            source_dist,
            ready,
            source_commit=source_commit,
            evidence_by_lane=fixture_evidence_by_lane(),
            include_models=False,
            cargo_lock_path=cargo_lock_path,
        )
        if failures:
            return failures
        failures = validate_release_dir(ready, expected_source_commit=source_commit)
        if failures:
            return failures
        generated, failures = generate_manifest(
            ready / next(iter(sorted(_rust_artifact_names()))),
            lane=rust_artifact_targets()[next(iter(sorted(_rust_artifact_names())))][0],
            evidence=fixture_evidence_by_lane()[
                rust_artifact_targets()[next(iter(sorted(_rust_artifact_names())))][0]
            ],
            cohort=_default_cohort(source_commit),
            cargo_lock_path=ROOT / "core" / "Cargo.lock",
        )
        if failures or generated is None:
            return failures
        generated_again, failures = generate_manifest(
            ready / generated.artifact_name,
            lane=rust_artifact_targets()[generated.artifact_name][0],
            evidence=fixture_evidence_by_lane()[
                rust_artifact_targets()[generated.artifact_name][0]
            ],
            cohort=_default_cohort(source_commit),
        )
        if failures or generated_again is None:
            return failures
        if generated.bytes != generated_again.bytes or not generated.bytes.endswith(
            b"\n"
        ):
            return [
                _failure(
                    "manifest generation is not byte deterministic",
                    expected="identical bytes with trailing newline",
                    actual="bytes differed",
                    repair="fix canonical_json_bytes ordering and formatting",
                )
            ]
        semantic_dir = root / "semantic"
        semantic_failures = write_inert_candidate(
            semantic_dir, include_models=False, source_commit=source_commit
        )
        if semantic_failures:
            return semantic_failures
        first_manifest = sorted(semantic_dir.glob("*.rust-release-manifest.json"))[0]
        payload = json.loads(first_manifest.read_text(encoding="utf-8"))
        payload["source_commit"] = "b" * 40
        first_manifest.write_bytes(canonical_json_bytes(payload))
        failures = validate_release_dir(
            semantic_dir, expected_source_commit=source_commit
        )
        expected_failure = _assert_failure(
            failures, "source_commit does not match SOURCE_COMMIT"
        )
        if expected_failure:
            return expected_failure
        one_model_dir = root / "one-model"
        write_inert_packages(one_model_dir, include_models=False)
        model_name = sorted(_models_expected_names())[0]
        (one_model_dir / model_name).write_bytes(b"model leftover\n")
        failures = validate_release_dir(
            one_model_dir, expected_source_commit=source_commit
        )
        expected_failure = _assert_failure(
            failures, "release directory contains exactly one models archive"
        )
        if expected_failure:
            return expected_failure
        pre_source = root / "pre-dist"
        write_inert_packages(pre_source, include_models=False)
        (pre_source / expected_package_names(include_models=False)[0]).unlink()
        pre_ready = root / "pre-ready"
        failures = build_and_promote_candidate(
            pre_source,
            pre_ready,
            source_commit=source_commit,
            evidence_by_lane=fixture_evidence_by_lane(),
            include_models=False,
            cargo_lock_path=cargo_lock_path,
        )
        expected_failure = _assert_failure(failures, "manifest artifact is missing")
        if (
            expected_failure
            or pre_ready.exists()
            or (root / "pre-ready.staging").exists()
        ):
            return expected_failure or [
                _failure(
                    "pre-promotion rollback failed",
                    expected="no ready or staging directory",
                    actual="leftover path present",
                    repair="remove staging on pre-promotion failures",
                )
            ]
        post_source = root / "post-dist"
        write_inert_packages(post_source, include_models=False)
        post_ready = root / "post-ready"

        def mutate_after_promote(path: Path) -> None:
            rust_artifact = next(iter(sorted(_rust_artifact_names())))
            (path / rust_artifact).write_bytes(b"mutated\n")

        failures = build_and_promote_candidate(
            post_source,
            post_ready,
            source_commit=source_commit,
            evidence_by_lane=fixture_evidence_by_lane(),
            include_models=False,
            cargo_lock_path=cargo_lock_path,
            _post_promote_hook=mutate_after_promote,
        )
        expected_failure = _assert_failure(
            failures, "artifact sha256 does not match manifest"
        )
        if expected_failure or post_ready.exists():
            return expected_failure or [
                _failure(
                    "post-promotion rollback failed",
                    expected="ready directory removed",
                    actual="ready path still exists",
                    repair="remove ready on post-promotion validation failure",
                )
            ]
        lock_source = root / "lock-dist"
        write_inert_packages(lock_source, include_models=False)
        lock_ready = root / "lock-ready"
        lock_path = root / ".rust-release-candidate.lock"
        lock_file = lock_path.open("a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            failures = build_and_promote_candidate(
                lock_source,
                lock_ready,
                source_commit=source_commit,
                evidence_by_lane=fixture_evidence_by_lane(),
                include_models=False,
                cargo_lock_path=cargo_lock_path,
            )
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        expected_failure = _assert_failure(
            failures, "release candidate lock is already held"
        )
        if (
            expected_failure
            or lock_ready.exists()
            or (root / "lock-ready.staging").exists()
        ):
            return expected_failure or [
                _failure(
                    "lock contention rollback failed",
                    expected="no ready or staging directory",
                    actual="leftover path present",
                    repair="do not stage while the lock is contended",
                )
            ]
        try:
            from scripts.check_release_preflight import expected_lane_tool_evidence
            from scripts.release_advisory_policy import PolicyRun
            from scripts.release_digest import candidate_digest, file_sha256_size
            from scripts.release_install_smoke import (
                PROOF_TARGETS,
                SCRUBBED_COMMAND_ENV,
                CommandResult,
                InstallObservation,
                build_install_proof,
                expected_distribution_entries,
                proof_targets_match_lanes,
                target_install_paths_from_ledger,
                write_install_proof,
            )
            from scripts.release_ledger import read_retained_ledger, write_ledger
        except ImportError as exc:
            return [
                _failure(
                    "fixtures mode could not import release evidence helpers",
                    expected="release evidence helper modules import cleanly",
                    actual=str(exc),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        if not proof_targets_match_lanes():
            return [
                _failure(
                    "proof target fixture relationship drifted",
                    expected="PROOF_TARGETS plus source equals LANES",
                    actual=", ".join(PROOF_TARGETS),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        policy_run = PolicyRun(
            advisory_source_id="fixture-advisories",
            db_snapshot_basename="advisory-db-fixture00000000",
            db_commit="a" * 40,
            db_archive_sha256="b" * 64,
            advisory_count=1,
            advisory_acquired_at="2026-07-20T11:00:00Z",
            db_commit_timestamp="2026-07-19T12:00:00Z",
            policy_checked_at="2026-07-20T12:00:00Z",
            result="pass",
        )
        fixture_root_wheel = next(
            name
            for name in expected_package_names(include_models=False)
            if name.startswith("solstone-") and "macosx_14_0_arm64" in name
        )
        fixture_core_wheel = next(
            name
            for name in expected_package_names(include_models=False)
            if name.startswith("solstone_core-") and "macosx_14_0_arm64" in name
        )
        native_records = [
            {
                "role": "root",
                "wheel": {
                    "name": fixture_root_wheel,
                    "sha256": "c" * 64,
                    "bytes": 12,
                },
                "member": {
                    "path": "solstone/observe/transcribe/parakeet_helper/_bin/parakeet-helper",
                    "sha256": "d" * 64,
                    "bytes": 6,
                },
                "tools": fixture_native_tools("macos-arm64"),
                "signing_mode": "signed-verified",
                "signing": {
                    "signer_pinned": True,
                    "team_pinned": True,
                    "hardened_runtime": True,
                    "trusted_timestamp": True,
                },
                "notarization_status": "accepted",
            },
            {
                "role": "core",
                "wheel": {
                    "name": fixture_core_wheel,
                    "sha256": "e" * 64,
                    "bytes": 12,
                },
                "member": {
                    "path": "solstone_core-1.0.0.data/scripts/solstone-core",
                    "sha256": "f" * 64,
                    "bytes": 6,
                },
                "tools": fixture_native_tools("macos-arm64"),
                "signing_mode": "signed-verified",
                "signing": {
                    "signer_pinned": True,
                    "team_pinned": True,
                    "hardened_runtime": True,
                    "trusted_timestamp": True,
                },
                "notarization_status": "accepted",
            },
        ]
        tool_evidence = {lane: expected_lane_tool_evidence(lane) for lane in LANES}
        evidence_root = root / "target" / "release-evidence"
        try:
            ledger_path = write_ledger(
                evidence_root=evidence_root,
                version=_current_version(),
                source_commit=source_commit,
                release_dir=ready,
                core_lock_path=cargo_lock_path,
                tool_evidence=tool_evidence,
                policy_run=policy_run,
                native_records=native_records,
                models={
                    "decision": "exclude",
                    "package_version": next(
                        name.removeprefix("solstone_journal_models-").removesuffix(
                            ".tar.gz"
                        )
                        for name in _models_expected_names()
                        if name.endswith(".tar.gz")
                    ),
                },
            )
            ledger_payload = read_retained_ledger(ledger_path)
            ledger_sha256 = file_sha256_size(ledger_path)[0]
            digest = candidate_digest(ready)
            candidate_paths = sorted(path for path in ready.iterdir() if path.is_file())
            env_root = root / "fixture-env"
            (env_root / "bin").mkdir(parents=True, exist_ok=True)
            (env_root / "bin" / "solstone-core").write_bytes(b"fixture-core")
            (env_root / "bin" / "parakeet-helper").write_bytes(b"fixture-helper")
            proofs_dir = evidence_root / _current_version() / "proofs"
            for target in PROOF_TARGETS:
                install_paths = target_install_paths_from_ledger(
                    ledger_payload,
                    target=target,
                    candidate_dir=ready,
                )
                retained_members = ledger_payload["native_members"][target]
                installed_members = [
                    {
                        "name": "solstone-core",
                        "path": env_root / "bin" / "solstone-core",
                        "sha256": retained_members["solstone-core"]["sha256"],
                        "symlink": False,
                    }
                ]
                if target == "macos-arm64":
                    installed_members.append(
                        {
                            "name": "parakeet-helper",
                            "path": env_root / "bin" / "parakeet-helper",
                            "sha256": retained_members["parakeet-helper"]["sha256"],
                            "symlink": False,
                        }
                    )
                proof = build_install_proof(
                    target=target,
                    version=_current_version(),
                    source_commit=source_commit,
                    core_lock_sha256=file_sha256_size(cargo_lock_path)[0],
                    candidate_digest=digest,
                    ledger_sha256=ledger_sha256,
                    candidate_dir=ready,
                    candidate_paths=candidate_paths,
                    ledger_payload=ledger_payload,
                    observation=InstallObservation(
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
                        installed_distributions=expected_distribution_entries(
                            install_paths
                        ),
                        installed_members=tuple(installed_members),
                        smoke={
                            "solstone-core": CommandResult(
                                argv=(
                                    str(env_root / "bin" / "solstone-core"),
                                    "--version",
                                ),
                                exit_code=0,
                                stdout=f"solstone-core {_current_version()}",
                                env=SCRUBBED_COMMAND_ENV,
                            )
                        },
                    ),
                    recorded_at=datetime(2026, 7, 20, 12, 30, tzinfo=UTC),
                )
                write_install_proof(proofs_dir / f"{target}.json", proof)
        except Exception as exc:
            return [
                _failure(
                    "fixtures mode release evidence validation failed",
                    expected="inert ledger and proofs validate",
                    actual=type(exc).__name__,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        return []


def _select_mode(env: Mapping[str, str]) -> tuple[ReleaseMode | None, list[Failure]]:
    has_manifest = "MANIFEST" in env
    has_release_dir = "RELEASE_DIR" in env
    has_source_commit = "SOURCE_COMMIT" in env
    if has_manifest and has_release_dir:
        return None, [
            _failure(
                "mode conflict",
                expected="set only one of MANIFEST or RELEASE_DIR",
                actual="both MANIFEST and RELEASE_DIR are set",
                repair="unset one mode variable and rerun",
            )
        ]
    if has_manifest and has_source_commit:
        return None, [
            _failure(
                "unexpected SOURCE_COMMIT",
                expected="SOURCE_COMMIT only with RELEASE_DIR mode",
                actual="SOURCE_COMMIT set with MANIFEST",
                repair="unset SOURCE_COMMIT for single-manifest validation",
            )
        ]
    if has_manifest:
        if not env.get("MANIFEST"):
            return None, [
                _failure(
                    "MANIFEST is empty",
                    expected="path to one manifest",
                    actual="empty",
                    repair="set MANIFEST to a companion manifest path",
                )
            ]
        return "manifest", []
    if has_release_dir:
        if not env.get("RELEASE_DIR"):
            return None, [
                _failure(
                    "RELEASE_DIR is empty",
                    expected="path to candidate payload directory",
                    actual="empty",
                    repair="set RELEASE_DIR to the exact candidate payload directory",
                )
            ]
        if not has_source_commit or not env.get("SOURCE_COMMIT"):
            return None, [
                _failure(
                    "missing SOURCE_COMMIT",
                    expected="full source commit with RELEASE_DIR",
                    actual="missing",
                    repair="set SOURCE_COMMIT to the full public release commit",
                )
            ]
        if not SOURCE_COMMIT_RE.fullmatch(env["SOURCE_COMMIT"]):
            return None, [
                _failure(
                    "SOURCE_COMMIT is not a full lowercase commit",
                    expected="40 or 64 lowercase hexadecimal characters",
                    actual=env["SOURCE_COMMIT"],
                    repair="set SOURCE_COMMIT to the full public release commit",
                )
            ]
        return "release-dir", []
    if has_source_commit:
        return None, [
            _failure(
                "unexpected SOURCE_COMMIT",
                expected="SOURCE_COMMIT only with RELEASE_DIR mode",
                actual="SOURCE_COMMIT set without RELEASE_DIR",
                repair="unset SOURCE_COMMIT for fixtures mode",
            )
        ]
    return "fixtures", []


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Validate solstone-core Rust release manifests offline."
    )
    parser.parse_args(argv)
    runtime_env = os.environ if env is None else env
    mode, failures = _select_mode(runtime_env)
    if not failures and mode == "fixtures":
        failures = run_fixtures_mode()
    elif not failures and mode == "manifest":
        failures = validate_manifest_file(Path(runtime_env["MANIFEST"]))
    elif not failures and mode == "release-dir":
        failures = validate_release_dir(
            Path(runtime_env["RELEASE_DIR"]),
            expected_source_commit=runtime_env["SOURCE_COMMIT"],
        )
    if failures:
        _format_failures(failures)
        return 1
    print("rust release manifest check ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
