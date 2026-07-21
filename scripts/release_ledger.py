#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Canonical release-candidate ledger writer."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.check_rust_release_manifest import (
    SOURCE_COMMIT_RE,
    Failure,
    canonical_json_bytes,
    rust_artifact_targets,
)
from scripts.release_advisory_policy import PolicyRun, validate_snapshot_identity
from scripts.release_digest import candidate_digest, file_sha256_size
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
        "product",
        "version",
        "source_commit",
        "candidate",
        "core_lock_sha256",
        "rust_targets",
        "tool_evidence",
        "dependency_policy",
        "policy_run",
        "native_summary",
        "proofs",
        "redaction",
    )
)
POLICY_RUN_KEYS = frozenset(
    (
        "advisory_source_id",
        "db_commit",
        "db_archive_sha256",
        "advisory_acquired_at",
        "policy_checked_at",
        "result",
    )
)


class LedgerError(RuntimeError):
    def __init__(self, failures: Sequence[Failure]) -> None:
        self.failures = tuple(failures)
        super().__init__("; ".join(failure.error for failure in self.failures))


def _failure(error: str, *, expected: str, actual: str, repair: str) -> Failure:
    return Failure(error=error, expected=expected, actual=actual, repair=repair)


def _candidate_files(release_dir: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(
        (path for path in release_dir.iterdir() if path.is_file()),
        key=lambda item: item.name,
    ):
        sha256, byte_count = file_sha256_size(path)
        files.append({"name": path.name, "sha256": sha256, "bytes": byte_count})
    return files


def _rust_targets() -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for artifact, (lane, target) in sorted(rust_artifact_targets().items()):
        targets.append({"lane": lane, "artifact": artifact, **target})
    return targets


def _policy_run_payload(policy_run: PolicyRun) -> dict[str, str]:
    payload = {
        "advisory_source_id": policy_run.advisory_source_id,
        "db_commit": policy_run.db_commit,
        "db_archive_sha256": policy_run.db_archive_sha256,
        "advisory_acquired_at": policy_run.advisory_acquired_at,
        "policy_checked_at": policy_run.policy_checked_at,
        "result": policy_run.result,
    }
    if set(payload) != POLICY_RUN_KEYS:
        raise AssertionError("policy run payload key set drifted")
    failures = validate_snapshot_identity(
        "ledger.policy_run",
        db_commit=payload["db_commit"],
        db_archive_sha256=payload["db_archive_sha256"],
    )
    if failures:
        raise LedgerError(failures)
    return payload


def validate_retained_ledger(payload: Mapping[str, Any]) -> list[Failure]:
    failures: list[Failure] = []
    if set(payload) != TOP_LEVEL_KEYS:
        failures.append(
            _failure(
                "retained ledger top-level key set is invalid",
                expected=", ".join(sorted(TOP_LEVEL_KEYS)),
                actual=", ".join(sorted(str(key) for key in payload)) or "<empty>",
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    policy_run = payload.get("policy_run")
    if not isinstance(policy_run, Mapping):
        failures.append(
            _failure(
                "retained ledger policy_run is invalid",
                expected="policy_run object",
                actual=str(policy_run),
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    else:
        if set(policy_run) != POLICY_RUN_KEYS:
            failures.append(
                _failure(
                    "retained ledger policy_run key set is invalid",
                    expected=", ".join(sorted(POLICY_RUN_KEYS)),
                    actual=", ".join(sorted(str(key) for key in policy_run))
                    or "<empty>",
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
        failures.extend(
            validate_snapshot_identity(
                "ledger.policy_run",
                db_commit=policy_run.get("db_commit"),
                db_archive_sha256=policy_run.get("db_archive_sha256"),
            )
        )
    failures.extend(validate_public_evidence_tree("ledger", payload))
    return failures


def read_retained_ledger(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LedgerError(
            [
                _failure(
                    "retained ledger is not valid JSON",
                    expected="JSON object",
                    actual=str(exc),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        ) from exc
    if not isinstance(payload, dict):
        raise LedgerError(
            [
                _failure(
                    "retained ledger is not a JSON object",
                    expected="JSON object",
                    actual=type(payload).__name__,
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            ]
        )
    failures = validate_retained_ledger(payload)
    if failures:
        raise LedgerError(failures)
    return payload


def _native_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_role: dict[str, Mapping[str, Any]] = {}
    failures: list[Failure] = []
    for record in records:
        role = record.get("role")
        if role not in {"root", "core"}:
            failures.append(
                _failure(
                    "macOS native record role is invalid",
                    expected="root or core",
                    actual=str(role),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
            continue
        if role in by_role:
            failures.append(
                _failure(
                    "macOS native record role is duplicated",
                    expected="one root and one core record",
                    actual=str(role),
                    repair="python3 scripts/check_rust_release_manifest.py",
                )
            )
            continue
        by_role[str(role)] = record
    if set(by_role) != {"root", "core"}:
        failures.append(
            _failure(
                "macOS native record set is incomplete",
                expected="exactly root and core records",
                actual=", ".join(sorted(by_role)) or "<empty>",
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    if failures:
        raise LedgerError(failures)

    def summarize(record: Mapping[str, Any]) -> dict[str, Any]:
        signing = record.get("signing", {})
        return {
            "wheel": record.get("wheel"),
            "member": record.get("member"),
            "tools": record.get("tools"),
            "signing_mode": record.get("signing_mode"),
            "signer_pinned": signing.get("signer_pinned")
            if isinstance(signing, Mapping)
            else None,
            "team_pinned": signing.get("team_pinned")
            if isinstance(signing, Mapping)
            else None,
            "hardened_runtime": signing.get("hardened_runtime")
            if isinstance(signing, Mapping)
            else None,
            "trusted_timestamp": signing.get("trusted_timestamp")
            if isinstance(signing, Mapping)
            else None,
            "notarization_status": record.get("notarization_status"),
        }

    return {
        "macos_root_helper": summarize(by_role["root"]),
        "macos_core_script": summarize(by_role["core"]),
    }


def build_ledger(
    *,
    version: str,
    source_commit: str,
    release_dir: Path,
    core_lock_path: Path,
    tool_evidence: Mapping[str, Mapping[str, str]],
    policy_run: PolicyRun,
    native_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    failures: list[Failure] = []
    if not SOURCE_COMMIT_RE.fullmatch(source_commit):
        failures.append(
            _failure(
                "ledger source commit is invalid",
                expected="full lowercase commit",
                actual=source_commit,
                repair="python3 scripts/check_rust_release_manifest.py",
            )
        )
    core_lock_sha256, _core_lock_bytes = file_sha256_size(core_lock_path)
    files = _candidate_files(release_dir)
    candidate = {
        "path": "CANDIDATE",
        "file_count": len(files),
        "package_file_count": sum(
            1
            for item in files
            if not item["name"].endswith(".rust-release-manifest.json")
        ),
        "manifest_file_count": sum(
            1 for item in files if item["name"].endswith(".rust-release-manifest.json")
        ),
        "candidate_digest": candidate_digest(release_dir),
        "files": files,
    }
    try:
        native_summary = _native_summary(native_records)
    except LedgerError as exc:
        failures.extend(exc.failures)
        native_summary = {}
    if failures:
        raise LedgerError(failures)

    ledger: dict[str, Any] = {
        "schema_version": 1,
        "kind": "solstone-release-ledger",
        "product": "solstone",
        "version": version,
        "source_commit": source_commit,
        "candidate": candidate,
        "core_lock_sha256": core_lock_sha256,
        "rust_targets": _rust_targets(),
        "tool_evidence": {
            lane: dict(tool_evidence[lane]) for lane in sorted(tool_evidence)
        },
        "dependency_policy": policy_run.manifest_dependency_policy(),
        "policy_run": _policy_run_payload(policy_run),
        "native_summary": native_summary,
        "proofs": {"expected_targets": list(PROOF_TARGETS)},
        "redaction": {"validator": "recursive-key-value-public-evidence"},
    }
    if set(ledger) != TOP_LEVEL_KEYS:
        raise AssertionError("ledger top-level key set drifted")
    public_failures = validate_public_evidence_tree("ledger", ledger)
    if public_failures:
        raise LedgerError(public_failures)
    return ledger


def write_ledger(
    *,
    evidence_root: Path,
    version: str,
    source_commit: str,
    release_dir: Path,
    core_lock_path: Path,
    tool_evidence: Mapping[str, Mapping[str, str]],
    policy_run: PolicyRun,
    native_records: Sequence[Mapping[str, Any]],
) -> Path:
    ledger = build_ledger(
        version=version,
        source_commit=source_commit,
        release_dir=release_dir,
        core_lock_path=core_lock_path,
        tool_evidence=tool_evidence,
        policy_run=policy_run,
        native_records=native_records,
    )
    output_dir = evidence_root / version
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "ledger.json"
    payload = canonical_json_bytes(ledger)
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
    failures = validate_retained_ledger(readback)
    if failures:
        raise LedgerError(failures)
    return output_path
