# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Read-only disposable sandbox marker validation."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from solstone.think.sandbox_profile import manifest

MAX_MARKER_BYTES = 64 * 1024
MARKER_NAME = ".solstone-sandbox.json"


@dataclass(frozen=True, slots=True)
class MarkerContext:
    journal_path: Path
    run_id: str
    profile: str
    contract_version: int


class MarkerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen.add(key)
        result[key] = value
    return result


def _load_marker(path: Path) -> object:
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise MarkerError(
            "sandbox_marker_missing", "sandbox marker is missing"
        ) from exc
    except OSError as exc:
        raise MarkerError(
            "sandbox_marker_missing", "sandbox marker is unreadable"
        ) from exc
    if len(raw) > MAX_MARKER_BYTES:
        raise MarkerError("sandbox_marker_unparseable", "sandbox marker is too large")
    try:
        text = raw.decode("utf-8")
        decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys)
        payload, end = decoder.raw_decode(text.lstrip())
        prefix_len = len(text) - len(text.lstrip())
        if text[prefix_len + end :].strip():
            raise ValueError("trailing content")
        return payload
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise MarkerError(
            "sandbox_marker_unparseable",
            "sandbox marker is not valid JSON",
        ) from exc


def validate_marker(journal_path: str | Path) -> MarkerContext:
    journal = canonical_path(journal_path)
    marker = journal / MARKER_NAME
    try:
        marker_stat = marker.lstat()
    except FileNotFoundError as exc:
        raise MarkerError(
            "sandbox_marker_missing", "sandbox marker is missing"
        ) from exc
    except OSError as exc:
        raise MarkerError(
            "sandbox_marker_missing", "sandbox marker is unreadable"
        ) from exc
    if stat.S_ISLNK(marker_stat.st_mode):
        raise MarkerError(
            "sandbox_marker_symlink", "sandbox marker must not be a symlink"
        )
    if not stat.S_ISREG(marker_stat.st_mode):
        raise MarkerError(
            "sandbox_marker_not_regular",
            "sandbox marker must be a regular file",
        )

    payload = _load_marker(marker)
    if not isinstance(payload, dict):
        raise MarkerError(
            "sandbox_marker_non_object",
            "sandbox marker must be a JSON object",
        )
    if payload.get("kind") != manifest.MARKER_KIND:
        raise MarkerError(
            "sandbox_marker_wrong_kind",
            "sandbox marker kind is unsupported",
        )
    if payload.get("contract_version") != manifest.CONTRACT_VERSION:
        raise MarkerError(
            "sandbox_marker_wrong_contract_version",
            "sandbox marker contract_version is unsupported",
        )
    if payload.get("profile") != manifest.PROFILE:
        raise MarkerError(
            "sandbox_marker_wrong_profile",
            "sandbox marker profile is unsupported",
        )

    run_id_raw = payload.get("run_id")
    if not isinstance(run_id_raw, str):
        raise MarkerError(
            "sandbox_marker_bad_run_id", "sandbox marker run_id is invalid"
        )
    try:
        parsed_uuid = UUID(run_id_raw)
    except ValueError as exc:
        raise MarkerError(
            "sandbox_marker_bad_run_id", "sandbox marker run_id is invalid"
        ) from exc
    if str(parsed_uuid) != run_id_raw:
        raise MarkerError(
            "sandbox_marker_bad_run_id",
            "sandbox marker run_id must be canonical",
        )

    marker_journal = payload.get("journal_path")
    if not isinstance(marker_journal, str):
        raise MarkerError(
            "sandbox_marker_path_mismatch",
            "sandbox marker journal_path is invalid",
        )
    if canonical_path(marker_journal) != journal:
        raise MarkerError(
            "sandbox_marker_path_mismatch",
            "sandbox marker journal_path does not match resolved journal",
        )

    return MarkerContext(
        journal_path=journal,
        run_id=run_id_raw,
        profile=manifest.PROFILE,
        contract_version=manifest.CONTRACT_VERSION,
    )
