#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Signed-packet advisory mirror audit for ``make audit``."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
for _path in (str(ROOT), str(_SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from scripts.check_rust_release_manifest import (  # noqa: E402
    CONTROL_RE,
    RAW_ENV_RE,
    SECRET_RE,
    SHA256_RE,
    Failure,
    validate_public_evidence_text,
)
from scripts.release_advisory_policy import (  # noqa: E402
    ADVISORY_TABLE_RE,
    ReleasePolicyError,
    _assert_scanned_snapshot,
    _cleanup_temp,
    _combined_release_policy_error,
    _count_advisories,
    _default_temp_path_factory,
    _failure,
    _format_utc,
    _parse_utc,
    _scanned_advisory_db,
    _toml_string,
    _unlink_path,
    _utc_now,
    _validate_advisory_count,
    _validate_source,
    advisory_check_argv,
    is_normalized_utc_timestamp,
)
from scripts.release_tool_pins import CARGO_DENY_VERSION  # noqa: E402
from scripts.transparency_signing import (  # noqa: E402
    DriverError,
    LocalMinisignSigner,
    TransparencySigner,
    check_minisign_binary,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]
Clock = Callable[[], datetime]
TempPathFactory = Callable[[str], Path]
PathRemover = Callable[[Path], None]

PINNED_KEY_ID = "5FCC81CD3DE12315"
PINNED_PUBKEY_SHA256 = (
    "c9fb713fe57791afbdebddde7b334e950ce1efcc167d49daf4cc1cbd930bb122"
)
ADVISORY_COHORT_ID = "sol-controlled-rustsec-mirror-v1"
TRUSTED_COMMENT_SCHEME = "solpbc-advisory-mirror-v1"
RECEIPT_MAX_AGE = 86400
MAX_CLOCK_SKEW = timedelta(minutes=5)

PRODUCT = "solstone-journal"
ADVISORY_LOCATOR_TERMINALS = frozenset({"advisory-db", "rustsec-advisory-db.git"})
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
KEY_ID_RE = re.compile(r"^[0-9A-F]{16}$")
SCP_LOCATOR_RE = re.compile(r"(?:[^@:/]+@)?(?P<host>[^:/]+):(?P<path>[^:]+)")
ABSOLUTE_PATH_RE = re.compile(
    r"(^|\s)(?:/[^ \t\r\n]+|~[^ \t\r\n]*|[A-Za-z]:[\\/][^ \t\r\n]*)"
)
EMAIL_RE = re.compile(r"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}")
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
PRIVATE_HOST_RE = re.compile(r"(?i)\b(?:localhost|[A-Za-z0-9-]+\.local)\b")
IP_CANDIDATE_RE = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-f:]*:[0-9a-f:]+\b",
    re.IGNORECASE,
)
CONTROL_EXCEPT_NEWLINE_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")

AUDIT_REPAIR = "reacquire the signed advisory mirror packet and rerun make audit"
INPUT_REPAIR = (
    "set AUDIT_ADVISORY_BUNDLE, AUDIT_ADVISORY_RECEIPT, "
    "AUDIT_ADVISORY_PUBKEY, and AUDIT_ADVISORY_LOCATOR"
)


@dataclass(frozen=True)
class ReceiptAuthority:
    synced_commit: str
    utc: str
    max_age: int
    canonical_bytes: bytes
    trusted_comment: str


@dataclass(frozen=True)
class PublicKeyMinisignVerifier:
    public_key: Path
    minisign: str = "minisign"

    def check(self) -> None:
        check_minisign_binary(self.minisign)

    def sign_file(
        self,
        message_path: Path,
        signature_path: Path,
        *,
        trusted_comment: str,
    ) -> None:
        raise NotImplementedError("advisory mirror audit verifier cannot sign")

    def verify_file(
        self,
        message_path: Path,
        signature_path: Path,
        *,
        expected_trusted_comment: str,
    ) -> None:
        # LocalMinisignSigner.verify_file never reads the secret key; check() only
        # performs the minisign 0.12 binary preflight and never requires a secret key.
        LocalMinisignSigner(
            secret_key=Path("__verify_only_unused_secret__"),
            public_key=self.public_key,
            minisign=self.minisign,
        ).verify_file(
            message_path,
            signature_path,
            expected_trusted_comment=expected_trusted_comment,
        )

    def trusted_comment(self, signature_path: Path) -> str:
        return LocalMinisignSigner(
            secret_key=Path("__verify_only_unused_secret__"),
            public_key=self.public_key,
            minisign=self.minisign,
        ).trusted_comment(signature_path)


def _failure_record(
    error: str,
    *,
    expected: str,
    actual: str,
    repair: str = AUDIT_REPAIR,
) -> Failure:
    return _failure(error, expected=expected, actual=actual, repair=repair)


def _raise_one(
    error: str,
    *,
    expected: str,
    actual: str,
    repair: str = AUDIT_REPAIR,
) -> None:
    raise ReleasePolicyError(
        [_failure_record(error, expected=expected, actual=actual, repair=repair)]
    )


def _redact_child_output(text: str, *, secrets: set[str]) -> str:
    redacted = text
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        redacted = redacted.replace(secret, "<redacted>")
    redacted = RAW_ENV_RE.sub("<redacted-env>", redacted)
    redacted = SECRET_RE.sub("<redacted-secret>", redacted)
    redacted = ABSOLUTE_PATH_RE.sub(
        lambda match: f"{match.group(1)}<redacted-path>", redacted
    )
    redacted = EMAIL_RE.sub("<redacted-email>", redacted)
    redacted = UUID_RE.sub("<redacted-uuid>", redacted)
    redacted = PRIVATE_HOST_RE.sub("<redacted-host>", redacted)
    redacted = IP_CANDIDATE_RE.sub("<redacted-ip>", redacted)
    redacted = CONTROL_EXCEPT_NEWLINE_RE.sub("?", redacted)
    if validate_public_evidence_text("child-output", redacted):
        return "<redacted child output>"
    return redacted


def _convert_driver_error(exc: DriverError, *, secrets: set[str]) -> ReleasePolicyError:
    failures: list[Failure] = []
    for failure in exc.failures:
        failures.append(
            _failure_record(
                failure.error,
                expected=failure.expected,
                actual=_redact_child_output(failure.actual, secrets=secrets),
                repair=failure.repair,
            )
        )
    return ReleasePolicyError(failures)


def _safe_failure(failure: Failure) -> Failure:
    text = (
        f"ERROR: {failure.error}\n"
        f"expected: {failure.expected}\n"
        f"actual: {failure.actual}\n"
        f"repair: {failure.repair}\n"
    )
    if not validate_public_evidence_text("failure", text):
        return failure
    return _failure_record(
        failure.error,
        expected=failure.expected,
        actual="redacted",
        repair=failure.repair,
    )


def _format_failures(failures: Sequence[Failure]) -> None:
    for failure in failures:
        safe = _safe_failure(failure)
        print(f"ERROR: {safe.error}", file=sys.stderr)
        print(f"  expected: {safe.expected}", file=sys.stderr)
        print(f"  actual: {safe.actual}", file=sys.stderr)
        print(f"  repair command: {safe.repair}", file=sys.stderr)


def _validate_regular_input(path: Path, *, label: str) -> list[Failure]:
    if path.is_symlink():
        return [
            _failure_record(
                f"advisory mirror input {label} is unsafe",
                expected=f"{label} regular file, not a symlink",
                actual="symlink",
                repair=INPUT_REPAIR,
            )
        ]
    if not path.exists():
        return [
            _failure_record(
                f"advisory mirror input {label} is missing",
                expected=f"{label} existing regular file",
                actual="missing",
                repair=INPUT_REPAIR,
            )
        ]
    if not path.is_file():
        return [
            _failure_record(
                f"advisory mirror input {label} is unsafe",
                expected=f"{label} regular file",
                actual="not a regular file",
                repair=INPUT_REPAIR,
            )
        ]
    return []


def _receipt_signature_path(receipt: Path) -> Path:
    return receipt.parent / f"{receipt.name}.minisig"


def _validate_inputs(
    *,
    bundle: Path,
    receipt: Path,
    pubkey: Path,
) -> tuple[Path, list[Failure]]:
    signature = _receipt_signature_path(receipt)
    failures: list[Failure] = []
    failures.extend(_validate_regular_input(bundle, label="bundle"))
    failures.extend(_validate_regular_input(receipt, label="receipt"))
    failures.extend(_validate_regular_input(pubkey, label="pubkey"))
    signature_failures = _validate_regular_input(signature, label="receipt signature")
    for failure in signature_failures:
        failures.append(
            _failure_record(
                "advisory mirror receipt signature is missing or unsafe",
                expected=failure.expected,
                actual=failure.actual,
                repair="place freshness.json.minisig next to freshness.json",
            )
        )
    return signature, failures


def _has_whitespace_or_control(value: str) -> bool:
    return any(char.isspace() for char in value) or CONTROL_RE.search(value) is not None


def _terminal_name(locator: str) -> str | None:
    parsed = urlparse(locator)
    if parsed.scheme:
        path = parsed.path
        if not path:
            return None
        return Path(path).name
    match = SCP_LOCATOR_RE.fullmatch(locator)
    if match is None:
        return None
    path = match.group("path")
    if not path:
        return None
    return Path(path).name


def validate_locator(locator: str) -> list[Failure]:
    failures: list[Failure] = []
    if not isinstance(locator, str) or not locator:
        return [
            _failure_record(
                "advisory mirror locator is empty",
                expected="private advisory mirror git locator",
                actual="<empty>",
                repair=INPUT_REPAIR,
            )
        ]
    if _has_whitespace_or_control(locator):
        failures.append(
            _failure_record(
                "advisory mirror locator contains whitespace or control characters",
                expected="single git locator without whitespace or controls",
                actual="redacted",
                repair=INPUT_REPAIR,
            )
        )
    if locator.endswith("/"):
        failures.append(
            _failure_record(
                "advisory mirror locator has a trailing slash",
                expected="locator without trailing slash",
                actual="trailing slash",
                repair=INPUT_REPAIR,
            )
        )
    if "?" in locator or "#" in locator:
        failures.append(
            _failure_record(
                "advisory mirror locator contains query or fragment",
                expected="locator without query or fragment",
                actual="query or fragment",
                repair=INPUT_REPAIR,
            )
        )
    if failures:
        return failures

    source_failures = _validate_source(ADVISORY_COHORT_ID, (locator,))
    if source_failures:
        return [
            _failure_record(
                failure.error,
                expected=failure.expected,
                actual=failure.actual,
                repair=INPUT_REPAIR,
            )
            for failure in source_failures
        ]

    terminal = _terminal_name(locator)
    if terminal not in ADVISORY_LOCATOR_TERMINALS:
        failures.append(
            _failure_record(
                "advisory mirror locator terminal name is not allowed",
                expected=", ".join(sorted(ADVISORY_LOCATOR_TERMINALS)),
                actual=terminal or "<empty>",
                repair=INPUT_REPAIR,
            )
        )
    return failures


def _canonical_receipt_bytes(*, synced_commit: str, utc: str) -> bytes:
    return (
        f'{{"max_age":86400,"synced_commit":"{synced_commit}","utc":"{utc}"}}\n'
    ).encode("utf-8")


def _trusted_comment(*, synced_commit: str, utc: str) -> str:
    return (
        f"{TRUSTED_COMMENT_SCHEME} synced_commit={synced_commit} "
        f"utc={utc} max_age=86400"
    )


def _read_receipt_authority(receipt: Path) -> ReceiptAuthority:
    raw = receipt.read_bytes()
    try:
        text = raw.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _raise_one(
            "advisory mirror receipt is not valid canonical JSON",
            expected="canonical UTF-8 JSON object",
            actual=type(exc).__name__,
        )
    if not isinstance(payload, dict):
        _raise_one(
            "advisory mirror receipt is not a JSON object",
            expected="receipt JSON object",
            actual=type(payload).__name__,
        )
    if set(payload) != {"max_age", "synced_commit", "utc"}:
        _raise_one(
            "advisory mirror receipt key set is invalid",
            expected="max_age, synced_commit, utc",
            actual=", ".join(sorted(str(key) for key in payload)) or "<empty>",
        )
    max_age = payload.get("max_age")
    synced_commit = payload.get("synced_commit")
    utc = payload.get("utc")
    if type(max_age) is not int or max_age != RECEIPT_MAX_AGE:
        _raise_one(
            "advisory mirror receipt max_age is invalid",
            expected=str(RECEIPT_MAX_AGE),
            actual=repr(max_age),
        )
    if (
        not isinstance(synced_commit, str)
        or GIT_COMMIT_RE.fullmatch(synced_commit) is None
    ):
        _raise_one(
            "advisory mirror receipt synced_commit is invalid",
            expected="40 lowercase hexadecimal git commit",
            actual="redacted",
        )
    if not isinstance(utc, str) or not is_normalized_utc_timestamp(utc):
        _raise_one(
            "advisory mirror receipt utc is invalid",
            expected="RFC3339 UTC timestamp normalized with Z",
            actual="redacted",
        )
    canonical = _canonical_receipt_bytes(synced_commit=synced_commit, utc=utc)
    if canonical != raw:
        _raise_one(
            "advisory mirror receipt bytes are not canonical",
            expected='{"max_age":86400,"synced_commit":"<40hex>","utc":"<RFC3339Z>"}\\n',
            actual="non-canonical JSON",
        )
    return ReceiptAuthority(
        synced_commit=synced_commit,
        utc=utc,
        max_age=max_age,
        canonical_bytes=canonical,
        trusted_comment=_trusted_comment(synced_commit=synced_commit, utc=utc),
    )


def _validate_receipt_freshness(receipt: ReceiptAuthority, *, clock: Clock) -> datetime:
    now = clock()
    receipt_time = _parse_utc(receipt.utc, label="advisory mirror receipt utc")
    if receipt_time - now > MAX_CLOCK_SKEW:
        _raise_one(
            "advisory mirror receipt utc is in the future",
            expected="receipt utc no more than 5 minutes in the future",
            actual=receipt.utc,
            repair="check the system clock, then reacquire the signed advisory mirror packet",
        )
    if now - receipt_time > timedelta(seconds=receipt.max_age):
        _raise_one(
            "advisory mirror receipt is stale",
            expected="receipt utc within max_age",
            actual=receipt.utc,
            repair=AUDIT_REPAIR,
        )
    return now


def _validate_pubkey_binding(
    pubkey: Path,
    *,
    pinned_key_id: str,
    pinned_pubkey_sha256: str,
) -> None:
    if KEY_ID_RE.fullmatch(pinned_key_id) is None:
        _raise_one(
            "advisory mirror pinned key ID is invalid",
            expected="16 uppercase hexadecimal characters",
            actual="redacted",
        )
    if SHA256_RE.fullmatch(pinned_pubkey_sha256) is None:
        _raise_one(
            "advisory mirror pinned public key SHA-256 is invalid",
            expected="64 lowercase hexadecimal characters",
            actual="redacted",
        )
    raw = pubkey.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != pinned_pubkey_sha256:
        _raise_one(
            "advisory mirror public key SHA-256 does not match the pin",
            expected="pinned public key SHA-256",
            actual="sha256 mismatch",
        )
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if len(lines) < 2:
        _raise_one(
            "advisory mirror public key is malformed",
            expected="minisign public key with base64 body on line 2",
            actual=f"{len(lines)} lines",
        )
    try:
        decoded = base64.b64decode(lines[1], validate=True)
    except ValueError:
        _raise_one(
            "advisory mirror public key body is not valid base64",
            expected="base64 minisign public key body",
            actual="invalid base64",
        )
    if len(decoded) != 42 or decoded[:2] != b"Ed":
        _raise_one(
            "advisory mirror public key body is not an Ed25519 minisign key",
            expected="42-byte minisign Ed public key blob",
            actual="invalid public key blob",
        )
    key_id = decoded[2:10][::-1].hex().upper()
    if key_id != pinned_key_id:
        _raise_one(
            "advisory mirror public key ID does not match the pin",
            expected="pinned minisign key ID",
            actual="key ID mismatch",
        )


def _verify_signature(
    *,
    verifier: TransparencySigner,
    receipt: Path,
    signature: Path,
    trusted_comment: str,
    secrets: set[str],
) -> None:
    try:
        verifier.check()
        verifier.verify_file(
            receipt,
            signature,
            expected_trusted_comment=trusted_comment,
        )
    except DriverError as exc:
        raise _convert_driver_error(exc, secrets=secrets) from exc


def audit_config_bytes(
    base_bytes: bytes,
    *,
    db_root: Path,
    db_urls: Sequence[str],
) -> bytes:
    try:
        base_text = base_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleasePolicyError(
            [
                _failure_record(
                    "core deny.toml is not UTF-8",
                    expected="UTF-8 TOML",
                    actual=str(exc),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        ) from exc
    if ADVISORY_TABLE_RE.search(base_text):
        raise ReleasePolicyError(
            [
                _failure_record(
                    "core deny.toml already defines advisories",
                    expected="core/deny.toml without [advisories]",
                    actual="[advisories] present",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        )
    prefix = base_bytes if base_bytes.endswith(b"\n") else base_bytes + b"\n"
    urls = ", ".join(_toml_string(url) for url in db_urls)
    block = (
        f"\n[advisories]\ndb-path = {_toml_string(str(db_root))}\ndb-urls = [{urls}]\n"
    )
    return prefix + block.encode("utf-8")


def _write_audit_config(
    root: Path,
    temp_root: Path,
    *,
    db_root: Path,
    db_urls: Sequence[str],
) -> Path:
    materialized = audit_config_bytes(
        (root / "core" / "deny.toml").read_bytes(),
        db_root=db_root,
        db_urls=db_urls,
    )
    temp_root.mkdir(parents=True, exist_ok=True)
    path = temp_root / "deny.audit-advisories.toml"
    path.write_bytes(materialized)
    return path


def _run(
    runner: Runner,
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "check": False,
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    if env is not None:
        kwargs["env"] = dict(env)
    return runner(list(argv), **kwargs)


def _cargo_env() -> dict[str, str]:
    env = dict(os.environ)
    env["CARGO_NET_OFFLINE"] = "true"
    return env


def _looks_like_remote(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.scheme != "file")


def _assert_local_git_argv(argv: Sequence[str], *, bundle: Path) -> None:
    command = list(argv)
    forbidden = {"fetch", "pull", "ls-remote", "remote"}
    if any(part in forbidden for part in command[1:]):
        _raise_one(
            "advisory mirror attempted a remote git operation",
            expected="local git bundle operations only",
            actual="forbidden git subcommand",
        )
    if len(command) >= 2 and command[1] == "clone":
        source = command[2] if len(command) > 2 else ""
        if source != str(bundle) or _looks_like_remote(source):
            _raise_one(
                "advisory mirror attempted to clone a non-bundle source",
                expected="git clone from the local advisory bundle",
                actual="non-local clone source",
            )


def _run_git_checked(
    runner: Runner,
    argv: Sequence[str],
    *,
    bundle: Path,
    error: str,
    secrets: set[str],
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    _assert_local_git_argv(argv, bundle=bundle)
    result = _run(runner, argv, cwd=cwd)
    if result.returncode != 0:
        _raise_one(
            error,
            expected="git exit 0",
            actual=f"exit {result.returncode}",
        )
    return result


def _run_cargo_deny_checked(
    runner: Runner,
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    secrets: set[str],
) -> subprocess.CompletedProcess[str]:
    result = _run(runner, argv, cwd=cwd, env=env)
    if result.returncode != 0:
        actual = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"exit {result.returncode}"
        )
        _raise_one(
            "advisory mirror cargo-deny final check failed",
            expected="cargo-deny advisory check exit 0",
            actual=_redact_child_output(actual, secrets=secrets),
        )
    return result


def _run_cargo_deny_unchecked(
    runner: Runner,
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return _run(runner, argv, cwd=cwd, env=env)


def _parse_bundle_heads(stdout: str, *, synced_commit: str) -> None:
    lines = stdout.splitlines()
    if len(lines) != 2:
        _raise_one(
            "advisory mirror bundle head set is invalid",
            expected="exactly HEAD and refs/heads/main",
            actual=f"{len(lines)} refs",
        )
    observed: set[tuple[str, str]] = set()
    for line in lines:
        parts = line.split()
        if len(parts) != 2:
            _raise_one(
                "advisory mirror bundle head line is malformed",
                expected="<sha> <ref>",
                actual="malformed line",
            )
        commit, ref = parts
        if GIT_COMMIT_RE.fullmatch(commit) is None:
            _raise_one(
                "advisory mirror bundle commit is invalid",
                expected="40 lowercase hexadecimal git commit",
                actual="redacted",
            )
        observed.add((commit, ref))
    expected = {
        (synced_commit, "HEAD"),
        (synced_commit, "refs/heads/main"),
    }
    if observed != expected:
        _raise_one(
            "advisory mirror bundle head set does not match the signed receipt",
            expected="HEAD and refs/heads/main at synced_commit",
            actual="head set mismatch",
        )


def _assert_direct_child(path: Path, parent: Path) -> None:
    if path.name in {"", ".", ".."}:
        _raise_one(
            "advisory mirror derived database path is invalid",
            expected="safe direct child basename",
            actual="invalid basename",
        )
    if path.resolve(strict=False).parent != parent.resolve(strict=False):
        _raise_one(
            "advisory mirror derived database path escapes the temp parent",
            expected="cargo-deny database path under temp parent",
            actual="redacted",
        )


def _assert_final_scanned_snapshot(stderr: str, snapshot: Path) -> None:
    try:
        _assert_scanned_snapshot(stderr, snapshot)
    except ReleasePolicyError as exc:
        failures = [
            _failure_record(
                failure.error,
                expected="materialized advisory snapshot",
                actual="redacted",
                repair=AUDIT_REPAIR,
            )
            for failure in exc.failures
        ]
        raise ReleasePolicyError(failures) from exc


def _cargo_deny_version(
    cargo_deny: str,
    *,
    runner: Runner,
    secrets: set[str],
) -> str:
    result = _run(runner, [cargo_deny, "--version"])
    actual = (
        result.stdout.strip() or result.stderr.strip() or f"exit {result.returncode}"
    )
    parts = actual.split()
    if (
        result.returncode != 0
        or len(parts) < 2
        or parts[0] != "cargo-deny"
        or parts[1] != CARGO_DENY_VERSION
    ):
        _raise_one(
            "advisory mirror cargo-deny version is not pinned",
            expected=CARGO_DENY_VERSION,
            actual=_redact_child_output(actual, secrets=secrets),
            repair=f"cargo install cargo-deny@{CARGO_DENY_VERSION} --locked --force",
        )
    return parts[1]


def _cargo_lock_sha256(root: Path) -> str:
    try:
        return hashlib.sha256((root / "core" / "Cargo.lock").read_bytes()).hexdigest()
    except OSError as exc:
        raise ReleasePolicyError(
            [
                _failure_record(
                    "advisory mirror cargo lock could not be read",
                    expected="core/Cargo.lock readable",
                    actual=type(exc).__name__,
                    repair="restore core/Cargo.lock and rerun make audit",
                )
            ]
        ) from exc


def _success_bytes(
    *,
    receipt: ReceiptAuthority,
    checked_at: datetime,
    cargo_lock_sha256: str,
    cargo_deny_version: str,
) -> bytes:
    payload = {
        "product": PRODUCT,
        "advisory_cohort": ADVISORY_COHORT_ID,
        "synced_commit": receipt.synced_commit,
        "receipt_utc": receipt.utc,
        "max_age": receipt.max_age,
        "checked_at": _format_utc(checked_at),
        "cargo_lock_sha256": cargo_lock_sha256,
        "cargo_deny_version": cargo_deny_version,
        "verdict": "pass",
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path)


def _materialize_and_check(
    root: Path,
    *,
    bundle: Path,
    receipt: ReceiptAuthority,
    cargo_deny: str,
    runner: Runner,
    temp_root: Path,
    config_path: Path,
    secrets: set[str],
) -> None:
    db_parent = temp_root / "db-root"
    db_parent.mkdir(parents=True, exist_ok=True)
    throwaway = db_parent / "bundle-clone"
    secrets.update({str(temp_root), str(db_parent), str(throwaway)})

    _run_git_checked(
        runner,
        ["git", "bundle", "verify", str(bundle)],
        bundle=bundle,
        error="advisory mirror bundle verification failed",
        secrets=secrets,
    )
    heads = _run_git_checked(
        runner,
        ["git", "bundle", "list-heads", str(bundle)],
        bundle=bundle,
        error="advisory mirror bundle list-heads failed",
        secrets=secrets,
    )
    _parse_bundle_heads(heads.stdout.strip(), synced_commit=receipt.synced_commit)

    _run_git_checked(
        runner,
        ["git", "clone", str(bundle), str(throwaway)],
        bundle=bundle,
        error="advisory mirror bundle clone failed",
        secrets=secrets,
    )
    head = _run_git_checked(
        runner,
        ["git", "-C", str(throwaway), "rev-parse", "HEAD"],
        bundle=bundle,
        error="advisory mirror clone HEAD read failed",
        secrets=secrets,
    )
    if head.stdout.strip() != receipt.synced_commit:
        _raise_one(
            "advisory mirror clone HEAD does not match the signed receipt",
            expected="cloned HEAD equals synced_commit",
            actual="clone HEAD mismatch",
        )
    _validate_advisory_count(_count_advisories(throwaway))

    cargo_env = _cargo_env()
    argv = advisory_check_argv(cargo_deny, config_path, root)
    discovery = _run_cargo_deny_unchecked(runner, argv, cwd=root, env=cargo_env)
    derived = _scanned_advisory_db(discovery.stderr)
    _assert_direct_child(derived, db_parent)
    if derived.exists():
        _raise_one(
            "advisory mirror derived database path already exists",
            expected="cargo-deny derived database path absent before rename",
            actual="preexisting derived path",
        )
    secrets.add(str(derived))
    throwaway.rename(derived)

    final = _run_cargo_deny_checked(
        runner,
        argv,
        cwd=root,
        env=cargo_env,
        secrets=secrets,
    )
    _assert_final_scanned_snapshot(final.stderr, derived)


def audit_advisory_mirror(
    root: Path,
    *,
    bundle: Path,
    receipt: Path,
    pubkey: Path,
    locator: str,
    cargo_deny: str = "cargo-deny",
    runner: Runner = subprocess.run,
    verifier: TransparencySigner | None = None,
    minisign: str = "minisign",
    clock: Clock = _utc_now,
    temp_path_factory: TempPathFactory = _default_temp_path_factory,
    cleanup_unlink: PathRemover = _unlink_path,
    cleanup_rmdir: PathRemover = _remove_tree,
    pinned_key_id: str = PINNED_KEY_ID,
    pinned_pubkey_sha256: str = PINNED_PUBKEY_SHA256,
) -> bytes:
    signature, input_failures = _validate_inputs(
        bundle=bundle,
        receipt=receipt,
        pubkey=pubkey,
    )
    if input_failures:
        raise ReleasePolicyError(input_failures)

    locator_failures = validate_locator(locator)
    if locator_failures:
        raise ReleasePolicyError(locator_failures)

    secrets = {
        locator,
        str(bundle),
        str(receipt),
        str(signature),
        str(pubkey),
    }
    _validate_pubkey_binding(
        pubkey,
        pinned_key_id=pinned_key_id,
        pinned_pubkey_sha256=pinned_pubkey_sha256,
    )
    receipt_authority = _read_receipt_authority(receipt)
    active_verifier = verifier or PublicKeyMinisignVerifier(pubkey, minisign=minisign)
    _verify_signature(
        verifier=active_verifier,
        receipt=receipt,
        signature=signature,
        trusted_comment=receipt_authority.trusted_comment,
        secrets=secrets,
    )
    checked_at = _validate_receipt_freshness(receipt_authority, clock=clock)

    cargo_deny_observed = _cargo_deny_version(
        cargo_deny,
        runner=runner,
        secrets=secrets,
    )

    temp_root = temp_path_factory("advisory-mirror-audit")
    config_path: Path | None = None
    result: bytes | None = None
    primary_error: ReleasePolicyError | None = None
    try:
        db_parent = temp_root / "db-root"
        config_path = _write_audit_config(
            root,
            temp_root,
            db_root=db_parent,
            db_urls=(locator,),
        )
        secrets.update({str(temp_root), str(config_path)})
        _materialize_and_check(
            root,
            bundle=bundle,
            receipt=receipt_authority,
            cargo_deny=cargo_deny,
            runner=runner,
            temp_root=temp_root,
            config_path=config_path,
            secrets=secrets,
        )
        result = _success_bytes(
            receipt=receipt_authority,
            checked_at=checked_at,
            cargo_lock_sha256=_cargo_lock_sha256(root),
            cargo_deny_version=cargo_deny_observed,
        )
    except ReleasePolicyError as exc:
        primary_error = exc
    finally:
        cleanup_error: ReleasePolicyError | None = None
        try:
            _cleanup_temp(
                temp_root,
                config_path,
                unlink_path=cleanup_unlink,
                remove_dir=cleanup_rmdir,
            )
        except ReleasePolicyError as exc:
            cleanup_error = exc
        combined = _combined_release_policy_error(primary_error, cleanup_error)
        if combined is not None:
            raise combined
    if result is None:
        raise AssertionError("advisory mirror audit did not produce a result")
    return result


def _raw_arg_failures(args: argparse.Namespace) -> list[Failure]:
    failures: list[Failure] = []
    for name in ("bundle", "receipt", "pubkey", "locator"):
        value = getattr(args, name)
        if value == "":
            failures.append(
                _failure_record(
                    f"advisory mirror input {name} is empty",
                    expected=f"non-empty {name}",
                    actual="<empty>",
                    repair=INPUT_REPAIR,
                )
            )
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--pubkey", required=True)
    parser.add_argument("--locator", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        raw_failures = _raw_arg_failures(args)
        if raw_failures:
            raise ReleasePolicyError(raw_failures)
        output = audit_advisory_mirror(
            ROOT,
            bundle=Path(args.bundle),
            receipt=Path(args.receipt),
            pubkey=Path(args.pubkey),
            locator=args.locator,
        )
    except ReleasePolicyError as exc:
        _format_failures(exc.failures)
        return 1
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
