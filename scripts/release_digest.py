#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Deterministic release-candidate digest helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from scripts.check_rust_release_manifest import canonical_json_bytes


def file_sha256_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            total += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), total


def candidate_digest(release_dir: Path) -> str:
    files = sorted(
        (path for path in release_dir.rglob("*") if path.is_file()),
        key=lambda path: (
            path.name.encode("utf-8"),
            path.relative_to(release_dir).as_posix().encode("utf-8"),
        ),
    )
    stream = bytearray()
    for path in files:
        digest, byte_count = file_sha256_size(path)
        stream.extend(f"{digest}  {byte_count}  {path.name}\n".encode("ascii"))
    return hashlib.sha256(bytes(stream)).hexdigest()


def bundle_digest(
    candidate_digest: str,
    ledger_sha256: str,
    proof_sha256_by_target: Mapping[str, str],
    nvattest_sha256_by_target: Mapping[str, str],
) -> str:
    payload = {
        "candidate_digest": candidate_digest,
        "ledger_sha256": ledger_sha256,
        "nvattest_sha256": {
            target: nvattest_sha256_by_target[target]
            for target in sorted(nvattest_sha256_by_target)
        },
        "proof_sha256": {
            target: proof_sha256_by_target[target]
            for target in sorted(proof_sha256_by_target)
        },
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
