#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Shared transparency-ledger canonicalization and retained-candidate helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from scripts.check_rust_release_manifest import (
    SOURCE_COMMIT_RE,
    Failure,
)
from scripts.release_candidate_driver import CandidateReport, DriverError, run_recover
from scripts.release_digest import file_sha256_size

PRODUCT = "solstone-journal"
ENTRY_SCHEMA = "https://solpbc.org/schemas/transparency-ledger-entry/v1.json"
LATEST_SCHEMA = "https://solpbc.org/schemas/transparency-latest/v1.json"
DEFAULT_BASE_URL = "https://transparency.solstone.app"
PUBLIC_TRUST_ANCHOR_FILENAME = "solpbc-transparency-1.pub"
PUBLIC_TRUST_ANCHOR_PATH = f"releases/keys/{PUBLIC_TRUST_ANCHOR_FILENAME}"
HEAD_LOG = "transparency-head-log.jsonl"
STAGING_ROOT = Path("target/transparency-publish")
ENTRY_OBJECT_NAME = "ledger-entry.json"
ENTRY_SIGNATURE_NAME = "ledger-entry.json.minisig"
LEDGER_OBJECT_NAME = "ledger.jsonl"
LATEST_OBJECT_NAME = "latest.json"
LATEST_SIGNATURE_NAME = "latest.json.minisig"
ZERO_SHA256 = "0" * 64
PUBLISHED_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

ENTRY_KEYS = frozenset(
    (
        "artifacts",
        "manifests",
        "prev_sha256",
        "prev_version",
        "product",
        "proofs",
        "published_utc",
        "schema",
        "seq",
        "source_commit",
        "version",
    )
)
ARTIFACT_KEYS = frozenset(("bytes", "name", "sha256"))
NAMED_DIGEST_KEYS = frozenset(("name", "sha256"))
LATEST_KEYS = frozenset(
    (
        "chain_length",
        "product",
        "schema",
        "signed_at",
        "tip_sha256",
        "valid_until",
        "version",
    )
)


@dataclass(frozen=True)
class NamedDigest:
    name: str
    sha256: str
    bytes: int | None = None

    def as_artifact(self) -> dict[str, Any]:
        if self.bytes is None:
            raise AssertionError("artifact byte count is missing")
        return {"bytes": self.bytes, "name": self.name, "sha256": self.sha256}

    def as_named_digest(self) -> dict[str, str]:
        return {"name": self.name, "sha256": self.sha256}


@dataclass(frozen=True)
class CandidateTransparencyParts:
    artifacts: tuple[NamedDigest, ...]
    manifests: tuple[NamedDigest, ...]
    proofs: tuple[NamedDigest, ...]
    artifact_files: Mapping[str, Path]
    version_files: Mapping[str, Path]
    source_commit: str
    retained_ledger: Mapping[str, Any]


@dataclass(frozen=True)
class EntryRecord:
    entry: Mapping[str, Any]
    bytes: bytes
    sha256: str


@dataclass(frozen=True)
class LatestRecord:
    pointer: Mapping[str, Any]
    bytes: bytes
    sha256: str


@dataclass(frozen=True)
class TrustedComment:
    kind: str
    fields: Mapping[str, str]


def failure(error: str, *, expected: str, actual: str, repair: str) -> Failure:
    return Failure(error=error, expected=expected, actual=actual, repair=repair)


def fail_closed(error: str, *, expected: str, actual: str, repair: str) -> None:
    raise DriverError([failure(error, expected=expected, actual=actual, repair=repair)])


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded_stage_key(product: str, version: str) -> str:
    return f"{quote(product, safe='')}/{quote(version, safe='')}"


def version_prefix(product: str, version: str) -> str:
    return f"releases/{product}/v/{version}/"


def version_object_key(product: str, version: str, name: str) -> str:
    return f"{version_prefix(product, version)}{name}"


def product_prefix(product: str) -> str:
    return f"releases/{product}/"


def ledger_key(product: str) -> str:
    return f"{product_prefix(product)}{LEDGER_OBJECT_NAME}"


def latest_key(product: str) -> str:
    return f"{product_prefix(product)}{LATEST_OBJECT_NAME}"


def latest_signature_key(product: str) -> str:
    return f"{product_prefix(product)}{LATEST_SIGNATURE_NAME}"


def public_url(base_url: str, key: str) -> str:
    return f"{base_url.rstrip('/')}/{key}"


def validate_ascii_json_value(label: str, value: Any) -> list[Failure]:
    failures: list[Failure] = []
    if isinstance(value, str):
        try:
            value.encode("ascii")
        except UnicodeEncodeError:
            failures.append(
                failure(
                    f"{label} contains a non-ASCII string",
                    expected="ASCII string before JSON serialization",
                    actual=repr(value),
                    repair="publish only byte-fixed ASCII transparency metadata",
                )
            )
        return failures
    if isinstance(value, bool):
        failures.append(
            failure(
                f"{label} contains a boolean",
                expected="integer numbers only; booleans are not integers",
                actual=repr(value),
                repair="replace the boolean with an explicit contract value",
            )
        )
        return failures
    if isinstance(value, int):
        return failures
    if isinstance(value, float):
        failures.append(
            failure(
                f"{label} contains a float",
                expected="integer numbers only",
                actual=repr(value),
                repair="replace the float with an integer contract value",
            )
        )
        return failures
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                failures.append(
                    failure(
                        f"{label} contains a non-string key",
                        expected="ASCII string object key",
                        actual=repr(key),
                        repair="use byte-fixed string keys only",
                    )
                )
                continue
            failures.extend(validate_ascii_json_value(f"{label}.{key}", key))
            failures.extend(validate_ascii_json_value(f"{label}.{key}", item))
        return failures
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for index, item in enumerate(value):
            failures.extend(validate_ascii_json_value(f"{label}[{index}]", item))
        return failures
    if value is None:
        return failures
    failures.append(
        failure(
            f"{label} contains an unsupported JSON value",
            expected="object, array, ASCII string, integer, or null",
            actual=type(value).__name__,
            repair="emit only the fixed transparency JSON value types",
        )
    )
    return failures


def canonical_json_bytes(
    value: Mapping[str, Any], *, label: str = "transparency"
) -> bytes:
    failures = validate_ascii_json_value(label, value)
    if failures:
        raise DriverError(failures)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    return payload.encode("utf-8")


def parse_published_utc(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or PUBLISHED_UTC_RE.fullmatch(value) is None:
        fail_closed(
            f"{label} is malformed",
            expected="YYYY-MM-DDTHH:MM:SSZ with no offset or fractional seconds",
            actual=repr(value),
            repair="publish using canonical UTC seconds",
        )
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        fail_closed(
            f"{label} is invalid",
            expected="valid UTC timestamp",
            actual=repr(value),
            repair="publish using a real UTC timestamp",
        )


def format_published_utc(value: datetime) -> str:
    utc_value = value.astimezone(UTC).replace(microsecond=0)
    return utc_value.strftime("%Y-%m-%dT%H:%M:%SZ")


def plus_14_days(value: str) -> str:
    return format_published_utc(
        parse_published_utc(value, label="signed_at") + timedelta(days=14)
    )


def validate_named_digest(label: str, value: Any, *, with_bytes: bool) -> list[Failure]:
    keys = ARTIFACT_KEYS if with_bytes else NAMED_DIGEST_KEYS
    failures: list[Failure] = []
    if not isinstance(value, Mapping) or set(value) != keys:
        return [
            failure(
                f"{label} entry has invalid fields",
                expected=", ".join(sorted(keys)),
                actual=repr(value),
                repair="rebuild the transparency entry from retained release evidence",
            )
        ]
    name = value.get("name")
    digest = value.get("sha256")
    if not isinstance(name, str) or not name:
        failures.append(
            failure(
                f"{label} name is invalid",
                expected="non-empty object basename",
                actual=repr(name),
                repair="rebuild the transparency entry from retained release evidence",
            )
        )
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        failures.append(
            failure(
                f"{label} sha256 is invalid",
                expected="64 lowercase hexadecimal characters",
                actual=repr(digest),
                repair="rebuild the transparency entry from retained release evidence",
            )
        )
    if with_bytes:
        byte_count = value.get("bytes")
        if (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
        ):
            failures.append(
                failure(
                    f"{label} bytes is invalid",
                    expected="non-negative integer byte count",
                    actual=repr(byte_count),
                    repair="rebuild the transparency entry from retained release evidence",
                )
            )
    return failures


def validate_ledger_entry(value: Any, *, label: str = "ledger entry") -> list[Failure]:
    failures: list[Failure] = []
    if not isinstance(value, Mapping) or set(value) != ENTRY_KEYS:
        return [
            failure(
                f"{label} has invalid fields",
                expected=", ".join(sorted(ENTRY_KEYS)),
                actual=repr(value),
                repair="rebuild the transparency entry from retained release evidence",
            )
        ]
    if value.get("schema") != ENTRY_SCHEMA:
        failures.append(
            failure(
                f"{label} schema is invalid",
                expected=ENTRY_SCHEMA,
                actual=repr(value.get("schema")),
                repair="publish using transparency-ledger-entry/v1",
            )
        )
    if value.get("product") != PRODUCT:
        failures.append(
            failure(
                f"{label} product is invalid",
                expected=PRODUCT,
                actual=repr(value.get("product")),
                repair="publish only the compiled-in product chain",
            )
        )
    seq = value.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        failures.append(
            failure(
                f"{label} seq is invalid",
                expected="positive integer",
                actual=repr(seq),
                repair="rebuild the transparency chain from locked entries",
            )
        )
    for key in ("version", "prev_version"):
        if not isinstance(value.get(key), str):
            failures.append(
                failure(
                    f"{label} {key} is invalid",
                    expected="string",
                    actual=repr(value.get(key)),
                    repair="rebuild the transparency entry from retained release evidence",
                )
            )
    for key in ("prev_sha256",):
        digest = value.get(key)
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            failures.append(
                failure(
                    f"{label} {key} is invalid",
                    expected="64 lowercase hexadecimal characters",
                    actual=repr(digest),
                    repair="rebuild the transparency chain from locked entries",
                )
            )
    source_commit = value.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or SOURCE_COMMIT_RE.fullmatch(source_commit) is None
    ):
        failures.append(
            failure(
                f"{label} source_commit is invalid",
                expected="40 or 64 lowercase hexadecimal characters",
                actual=repr(source_commit),
                repair="publish from retained release evidence only",
            )
        )
    try:
        parse_published_utc(value.get("published_utc"), label=f"{label} published_utc")
    except DriverError as exc:
        failures.extend(exc.failures)
    for index, item in enumerate(value.get("artifacts", ())):
        failures.extend(
            validate_named_digest(f"{label}.artifacts[{index}]", item, with_bytes=True)
        )
    if not isinstance(value.get("artifacts"), list):
        failures.append(
            failure(
                f"{label} artifacts is invalid",
                expected="artifact array",
                actual=type(value.get("artifacts")).__name__,
                repair="rebuild the transparency entry from retained release evidence",
            )
        )
    for key, with_bytes in (("manifests", False), ("proofs", False)):
        items = value.get(key)
        if not isinstance(items, list):
            failures.append(
                failure(
                    f"{label} {key} is invalid",
                    expected=f"{key} array",
                    actual=type(items).__name__,
                    repair="rebuild the transparency entry from retained release evidence",
                )
            )
            continue
        for index, item in enumerate(items):
            failures.extend(
                validate_named_digest(
                    f"{label}.{key}[{index}]", item, with_bytes=with_bytes
                )
            )
    return failures


def validate_latest_pointer(
    value: Any, *, label: str = "latest pointer"
) -> list[Failure]:
    failures: list[Failure] = []
    if not isinstance(value, Mapping) or set(value) != LATEST_KEYS:
        return [
            failure(
                f"{label} has invalid fields",
                expected=", ".join(sorted(LATEST_KEYS)),
                actual=repr(value),
                repair="rebuild the signed transparency pointer",
            )
        ]
    if value.get("schema") != LATEST_SCHEMA:
        failures.append(
            failure(
                f"{label} schema is invalid",
                expected=LATEST_SCHEMA,
                actual=repr(value.get("schema")),
                repair="publish using transparency-latest/v1",
            )
        )
    if value.get("product") != PRODUCT:
        failures.append(
            failure(
                f"{label} product is invalid",
                expected=PRODUCT,
                actual=repr(value.get("product")),
                repair="publish only the compiled-in product chain",
            )
        )
    chain_length = value.get("chain_length")
    if (
        isinstance(chain_length, bool)
        or not isinstance(chain_length, int)
        or chain_length < 1
    ):
        failures.append(
            failure(
                f"{label} chain_length is invalid",
                expected="positive integer",
                actual=repr(chain_length),
                repair="rebuild the signed transparency pointer",
            )
        )
    tip = value.get("tip_sha256")
    if not isinstance(tip, str) or SHA256_RE.fullmatch(tip) is None:
        failures.append(
            failure(
                f"{label} tip_sha256 is invalid",
                expected="64 lowercase hexadecimal characters",
                actual=repr(tip),
                repair="rebuild the signed transparency pointer",
            )
        )
    if not isinstance(value.get("version"), str) or not value["version"]:
        failures.append(
            failure(
                f"{label} version is invalid",
                expected="non-empty version string",
                actual=repr(value.get("version")),
                repair="rebuild the signed transparency pointer",
            )
        )
    for key in ("signed_at", "valid_until"):
        try:
            parse_published_utc(value.get(key), label=f"{label} {key}")
        except DriverError as exc:
            failures.extend(exc.failures)
    return failures


def assert_no_canonical_failures(label: str, value: Mapping[str, Any]) -> None:
    failures = validate_ascii_json_value(label, value)
    if failures:
        raise DriverError(failures)


def build_ledger_entry(
    *,
    artifacts: Sequence[NamedDigest],
    manifests: Sequence[NamedDigest],
    proofs: Sequence[NamedDigest],
    prev_sha256: str,
    prev_version: str,
    product: str,
    published_utc: str,
    seq: int,
    source_commit: str,
    version: str,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "artifacts": [item.as_artifact() for item in artifacts],
        "manifests": [item.as_named_digest() for item in manifests],
        "prev_sha256": prev_sha256,
        "prev_version": prev_version,
        "product": product,
        "proofs": [item.as_named_digest() for item in proofs],
        "published_utc": published_utc,
        "schema": ENTRY_SCHEMA,
        "seq": seq,
        "source_commit": source_commit,
        "version": version,
    }
    if set(entry) != ENTRY_KEYS:
        raise AssertionError("transparency ledger entry field set drifted")
    failures = validate_ledger_entry(entry)
    failures.extend(validate_ascii_json_value("ledger entry", entry))
    if failures:
        raise DriverError(failures)
    return entry


def build_latest_pointer(
    *,
    chain_length: int,
    product: str,
    signed_at: str,
    tip_sha256: str,
    valid_until: str,
    version: str,
) -> dict[str, Any]:
    pointer: dict[str, Any] = {
        "chain_length": chain_length,
        "product": product,
        "schema": LATEST_SCHEMA,
        "signed_at": signed_at,
        "tip_sha256": tip_sha256,
        "valid_until": valid_until,
        "version": version,
    }
    if set(pointer) != LATEST_KEYS:
        raise AssertionError("transparency latest pointer field set drifted")
    failures = validate_latest_pointer(pointer)
    failures.extend(validate_ascii_json_value("latest pointer", pointer))
    if failures:
        raise DriverError(failures)
    return pointer


def entry_trusted_comment(entry: Mapping[str, Any], entry_sha256: str) -> str:
    return (
        "solpbc-transparency-v1 entry "
        f"product={entry['product']} "
        f"seq={entry['seq']} "
        f"version={entry['version']} "
        f"sha256={entry_sha256} "
        f"prev={entry['prev_sha256']}"
    )


def latest_trusted_comment(pointer: Mapping[str, Any]) -> str:
    return (
        "solpbc-transparency-v1 latest "
        f"product={pointer['product']} "
        f"chain_length={pointer['chain_length']} "
        f"tip={pointer['tip_sha256']} "
        f"valid_until={pointer['valid_until']}"
    )


def parse_trusted_comment(comment: str) -> TrustedComment:
    parts = comment.split(" ")
    if len(parts) < 3 or parts[0] != "solpbc-transparency-v1":
        fail_closed(
            "transparency trusted comment prefix is invalid",
            expected="solpbc-transparency-v1 trusted comment",
            actual=comment,
            repair="re-sign the transparency object with the fixed trusted comment",
        )
    fields: dict[str, str] = {}
    for raw in parts[2:]:
        if "=" not in raw:
            fail_closed(
                "transparency trusted comment field is invalid",
                expected="key=value fields",
                actual=comment,
                repair="re-sign the transparency object with the fixed trusted comment",
            )
        key, value = raw.split("=", 1)
        fields[key] = value
    return TrustedComment(kind=parts[1], fields=fields)


def validate_entry_trusted_comment(
    comment: str,
    *,
    entry: Mapping[str, Any],
    entry_sha256: str,
) -> list[Failure]:
    try:
        parsed = parse_trusted_comment(comment)
    except DriverError as exc:
        return list(exc.failures)
    expected = {
        "product": str(entry.get("product")),
        "seq": str(entry.get("seq")),
        "version": str(entry.get("version")),
        "sha256": entry_sha256,
        "prev": str(entry.get("prev_sha256")),
    }
    if parsed.kind != "entry" or parsed.fields != expected:
        return [
            failure(
                "transparency entry trusted comment does not match body",
                expected=entry_trusted_comment(entry, entry_sha256),
                actual=comment,
                repair="re-sign the transparency entry after rebuilding it",
            )
        ]
    if parsed.fields.get("product") != PRODUCT:
        return [
            failure(
                "transparency trusted comment product is invalid",
                expected=PRODUCT,
                actual=repr(parsed.fields.get("product")),
                repair="use the product-specific transparency chain",
            )
        ]
    return []


def validate_latest_trusted_comment(
    comment: str,
    *,
    pointer: Mapping[str, Any],
) -> list[Failure]:
    try:
        parsed = parse_trusted_comment(comment)
    except DriverError as exc:
        return list(exc.failures)
    expected = {
        "product": str(pointer.get("product")),
        "chain_length": str(pointer.get("chain_length")),
        "tip": str(pointer.get("tip_sha256")),
        "valid_until": str(pointer.get("valid_until")),
    }
    if parsed.kind != "latest" or parsed.fields != expected:
        return [
            failure(
                "transparency latest trusted comment does not match body",
                expected=latest_trusted_comment(pointer),
                actual=comment,
                repair="re-sign the transparency pointer after rebuilding it",
            )
        ]
    if parsed.fields.get("product") != PRODUCT:
        return [
            failure(
                "transparency trusted comment product is invalid",
                expected=PRODUCT,
                actual=repr(parsed.fields.get("product")),
                repair="use the product-specific transparency chain",
            )
        ]
    return []


def parse_ledger_entry_bytes(
    data: bytes, *, label: str = "ledger entry"
) -> EntryRecord:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail_closed(
            f"{label} is not valid JSON",
            expected="canonical transparency ledger entry JSON",
            actual=type(exc).__name__,
            repair="rebuild the locked transparency entry",
        )
    failures = validate_ledger_entry(value)
    if failures:
        raise DriverError(failures)
    canonical = canonical_json_bytes(value, label=label)
    if canonical != data:
        fail_closed(
            f"{label} is not canonical",
            expected="canonical JSON bytes with one trailing newline",
            actual="bytes differ after canonicalization",
            repair="rebuild the locked transparency entry",
        )
    return EntryRecord(entry=value, bytes=data, sha256=sha256_bytes(data))


def parse_latest_bytes(data: bytes, *, label: str = "latest pointer") -> LatestRecord:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail_closed(
            f"{label} is not valid JSON",
            expected="canonical transparency latest JSON",
            actual=type(exc).__name__,
            repair="rebuild the signed transparency pointer",
        )
    failures = validate_latest_pointer(value)
    if failures:
        raise DriverError(failures)
    canonical = canonical_json_bytes(value, label=label)
    if canonical != data:
        fail_closed(
            f"{label} is not canonical",
            expected="canonical JSON bytes with one trailing newline",
            actual="bytes differ after canonicalization",
            repair="rebuild the signed transparency pointer",
        )
    return LatestRecord(pointer=value, bytes=data, sha256=sha256_bytes(data))


def parse_ledger_jsonl(data: bytes) -> tuple[EntryRecord, ...]:
    if not data:
        return ()
    lines = data.splitlines(keepends=True)
    records: list[EntryRecord] = []
    for index, line in enumerate(lines, start=1):
        if not line.endswith(b"\n"):
            fail_closed(
                "transparency ledger.jsonl line is not newline-terminated",
                expected="each JSONL row includes its trailing newline",
                actual=f"line {index}",
                repair="re-derive ledger.jsonl from locked entries",
            )
        records.append(
            parse_ledger_entry_bytes(line, label=f"ledger.jsonl line {index}")
        )
    return tuple(records)


def ledger_jsonl_bytes(entries: Sequence[EntryRecord]) -> bytes:
    return b"".join(entry.bytes for entry in entries)


def validate_entry_chain(entries: Sequence[EntryRecord]) -> list[Failure]:
    failures: list[Failure] = []
    previous_sha = ZERO_SHA256
    previous_version = ""
    previous_published: datetime | None = None
    expected_seq = 1
    for record in entries:
        entry = record.entry
        seq = entry.get("seq")
        if seq != expected_seq:
            failures.append(
                failure(
                    "transparency ledger seq is non-monotonic or gapped",
                    expected=str(expected_seq),
                    actual=repr(seq),
                    repair="re-derive the chain from locked entries and retry",
                )
            )
        if entry.get("prev_sha256") != previous_sha:
            failures.append(
                failure(
                    "transparency ledger prev_sha256 linkage is broken",
                    expected=previous_sha,
                    actual=repr(entry.get("prev_sha256")),
                    repair="stop and audit the locked transparency entries",
                )
            )
        if entry.get("prev_version") != previous_version:
            failures.append(
                failure(
                    "transparency ledger prev_version linkage is broken",
                    expected=previous_version,
                    actual=repr(entry.get("prev_version")),
                    repair="stop and audit the locked transparency entries",
                )
            )
        try:
            published = parse_published_utc(
                entry.get("published_utc"),
                label="ledger entry published_utc",
            )
        except DriverError as exc:
            failures.extend(exc.failures)
            published = None
        if (
            previous_published is not None
            and published is not None
            and published <= previous_published
        ):
            failures.append(
                failure(
                    "transparency ledger published_utc is not strictly increasing",
                    expected=f"later than {format_published_utc(previous_published)}",
                    actual=str(entry.get("published_utc")),
                    repair="retry with a later publication time",
                )
            )
        previous_sha = record.sha256
        previous_version = str(entry.get("version"))
        previous_published = published
        expected_seq += 1
    return failures


def recover_candidate(
    root: Path, *, version: str, source_commit: str
) -> CandidateReport:
    return run_recover(root, version=version, source_commit=source_commit)


def snapshot_candidate(
    *,
    source_root: Path,
    snapshot_root: Path,
    version: str,
) -> None:
    release_src = source_root / "dist" / "release-candidate" / version
    evidence_src = source_root / "target" / "release-evidence" / version
    release_dst = snapshot_root / "dist" / "release-candidate" / version
    evidence_dst = snapshot_root / "target" / "release-evidence" / version
    if release_dst.exists() or evidence_dst.exists():
        fail_closed(
            "transparency snapshot already exists",
            expected="fresh snapshot directory",
            actual=str(snapshot_root),
            repair="remove only the version-specific transparency staging dir and retry",
        )
    shutil.copytree(release_src, release_dst, symlinks=False)
    shutil.copytree(evidence_src, evidence_dst, symlinks=False)


def read_retained_ledger(report: CandidateReport) -> Mapping[str, Any]:
    try:
        return json.loads(
            (report.evidence_dir / "ledger.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        fail_closed(
            "retained release ledger could not be read",
            expected="target/release-evidence/<version>/ledger.json",
            actual=type(exc).__name__,
            repair="bash scripts/release.sh --recover <version> <source-commit>",
        )


def collect_candidate_parts(report: CandidateReport) -> CandidateTransparencyParts:
    ledger = read_retained_ledger(report)
    source_commit = ledger.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or SOURCE_COMMIT_RE.fullmatch(source_commit) is None
    ):
        fail_closed(
            "retained ledger source_commit is invalid",
            expected="retained source_commit",
            actual=repr(source_commit),
            repair="bash scripts/release.sh --recover <version> <source-commit>",
        )
    files = (
        ledger.get("candidate", {}).get("files")
        if isinstance(ledger.get("candidate"), Mapping)
        else None
    )
    if not isinstance(files, list):
        fail_closed(
            "retained ledger candidate.files is invalid",
            expected="candidate.files array",
            actual=type(files).__name__,
            repair="bash scripts/release.sh --recover <version> <source-commit>",
        )
    artifacts: list[NamedDigest] = []
    manifests: list[NamedDigest] = []
    artifact_files: dict[str, Path] = {}
    version_files: dict[str, Path] = {}
    for item in files:
        if not isinstance(item, Mapping):
            fail_closed(
                "retained ledger candidate.files entry is invalid",
                expected="candidate file object",
                actual=repr(item),
                repair="bash scripts/release.sh --recover <version> <source-commit>",
            )
        name = item.get("name")
        digest = item.get("sha256")
        byte_count = item.get("bytes")
        if (
            not isinstance(name, str)
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
        ):
            fail_closed(
                "retained ledger candidate.files entry is invalid",
                expected="name, sha256, bytes",
                actual=repr(item),
                repair="bash scripts/release.sh --recover <version> <source-commit>",
            )
        path = report.release_dir / name
        actual_digest, actual_bytes = file_sha256_size(path)
        if actual_digest != digest or actual_bytes != byte_count:
            fail_closed(
                "retained candidate file digest does not match ledger",
                expected=f"{name} {digest}/{byte_count}",
                actual=f"{actual_digest}/{actual_bytes}",
                repair="bash scripts/release.sh --recover <version> <source-commit>",
            )
        digest_record = NamedDigest(name=name, sha256=digest, bytes=byte_count)
        if name.endswith(".rust-release-manifest.json"):
            manifests.append(digest_record)
            version_files[name] = path
        else:
            artifacts.append(digest_record)
            artifact_files[name] = path
    candidate = ledger.get("candidate")
    if not isinstance(candidate, Mapping):
        fail_closed(
            "retained ledger candidate is invalid",
            expected="candidate object",
            actual=type(candidate).__name__,
            repair="bash scripts/release.sh --recover <version> <source-commit>",
        )
    if candidate.get("package_file_count") != len(artifacts):
        fail_closed(
            "retained ledger package_file_count is invalid",
            expected=str(len(artifacts)),
            actual=repr(candidate.get("package_file_count")),
            repair="bash scripts/release.sh --recover <version> <source-commit>",
        )
    if candidate.get("manifest_file_count") != len(manifests):
        fail_closed(
            "retained ledger manifest_file_count is invalid",
            expected=str(len(manifests)),
            actual=repr(candidate.get("manifest_file_count")),
            repair="bash scripts/release.sh --recover <version> <source-commit>",
        )
    proofs: list[NamedDigest] = []
    proofs_dir = report.evidence_dir / "proofs"
    for target, digest in sorted(report.proof_sha256.items()):
        name = f"{target}.json"
        path = proofs_dir / name
        actual_digest, _actual_bytes = file_sha256_size(path)
        if actual_digest != digest:
            fail_closed(
                "retained proof digest does not match candidate report",
                expected=digest,
                actual=actual_digest,
                repair="bash scripts/release.sh --recover <version> <source-commit>",
            )
        proofs.append(NamedDigest(name=name, sha256=digest))
        version_files[name] = path
    validate_retained_source_clean(report)
    validate_retained_proof_versions(report, ledger)
    return CandidateTransparencyParts(
        artifacts=tuple(sorted(artifacts, key=lambda item: item.name)),
        manifests=tuple(sorted(manifests, key=lambda item: item.name)),
        proofs=tuple(sorted(proofs, key=lambda item: item.name)),
        artifact_files=artifact_files,
        version_files=version_files,
        source_commit=source_commit,
        retained_ledger=ledger,
    )


def validate_retained_source_clean(report: CandidateReport) -> None:
    failures: list[Failure] = []
    for manifest in sorted(report.release_dir.glob("*.rust-release-manifest.json")):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(
                failure(
                    "retained companion manifest could not be read",
                    expected=manifest.name,
                    actual=type(exc).__name__,
                    repair="bash scripts/release.sh --recover <version> <source-commit>",
                )
            )
            continue
        if payload.get("source_dirty") is not False:
            failures.append(
                failure(
                    "candidate validated state reports a dirty source",
                    expected=f"{report.version} source_dirty false",
                    actual=f"commit {payload.get('source_commit')} source_dirty {payload.get('source_dirty')!r}",
                    repair="cut the next version from retained clean-source evidence",
                )
            )
    if failures:
        raise DriverError(failures)


def validate_retained_proof_versions(
    report: CandidateReport,
    ledger: Mapping[str, Any],
) -> None:
    expected_targets = (
        ledger.get("proofs", {}).get("expected_targets")
        if isinstance(ledger.get("proofs"), Mapping)
        else None
    )
    expected_target_set = (
        set(expected_targets) if isinstance(expected_targets, list) else set()
    )
    failures: list[Failure] = []
    for path in sorted((report.evidence_dir / "proofs").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(
                failure(
                    "retained proof receipt could not be read",
                    expected=path.name,
                    actual=type(exc).__name__,
                    repair="bash scripts/release.sh --recover <version> <source-commit>",
                )
            )
            continue
        target = payload.get("target")
        if target not in expected_target_set:
            failures.append(
                failure(
                    "retained proof target is stale or unexpected",
                    expected=", ".join(sorted(expected_target_set)),
                    actual=repr(target),
                    repair="bash scripts/release.sh --recover <version> <source-commit>",
                )
            )
        if payload.get("version") != report.version:
            failures.append(
                failure(
                    "retained proof version is stale",
                    expected=report.version,
                    actual=repr(payload.get("version")),
                    repair="bash scripts/release.sh --recover <version> <source-commit>",
                )
            )
    if failures:
        raise DriverError(failures)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_bytes(data)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
