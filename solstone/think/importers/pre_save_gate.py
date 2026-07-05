# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pre-save approval gate for sensitive file importers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solstone.think.utils import get_journal

APPROVAL_SCHEMA = "solstone.health_import_preflight.v1"
CHECKLIST_VERSION = "solstone.health_import_preflight.checklist.v1"
APPROVAL_RELATIVE_PATH = Path("imports") / "_approvals" / "health_import_preflight.json"
CHECKLIST_DESTINATIONS = (
    "time_machine",
    "icloud",
    "solbase",
    "hosted_backup",
    "other",
)
DESTINATION_DECISIONS = {"approved", "excluded"}
SENSITIVE_IMPORTERS = frozenset({"apple_health", "oura"})


@dataclass(frozen=True, slots=True)
class PreSaveGateDecision:
    """Successful gate result for a file-import save attempt."""

    importer: str
    enforced: bool
    approval_path: str | None
    checklist_version: str


@dataclass(frozen=True, slots=True)
class PreSaveGateFailure:
    """JSON-safe failure payload for expected pre-save gate blocks."""

    importer: str
    reason: str
    approval_path: str
    target_journal: str
    missing_fields: tuple[str, ...] = ()
    invalid_fields: tuple[str, ...] = ()
    checklist_version: str = CHECKLIST_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "skipped": True,
            "reason": "health_pre_save_gate_required",
            "gate_reason": self.reason,
            "importer": self.importer,
            "approval_path": self.approval_path,
            "target_journal": self.target_journal,
            "missing_fields": list(self.missing_fields),
            "invalid_fields": list(self.invalid_fields),
            "checklist_version": self.checklist_version,
        }


class PreSaveGateError(RuntimeError):
    """Raised when a sensitive importer is not approved for save mode."""

    exit_code = 2

    def __init__(self, failure: PreSaveGateFailure) -> None:
        self.failure = failure
        super().__init__(f"{failure.importer} save blocked: {failure.reason}")

    def to_dict(self) -> dict[str, Any]:
        return self.failure.to_dict()

    def format_text(self) -> str:
        """Return a traceback-free human message for CLI callers."""

        return "\n".join(
            [
                "Health import save blocked before journal write.",
                "",
                f"Importer: {self.failure.importer}",
                f"Target journal: {self.failure.target_journal}",
                f"Reason: {self.failure.reason.replace('_', ' ')}",
                "",
                "No import directory was created.",
                "",
                f"Approval artifact: {self.failure.approval_path}",
                "",
                "Next:",
                "  1. Run dry-run preview only.",
                "  2. Complete the journal replication and raw-retention review.",
                "  3. Record the approval artifact.",
                "  4. Re-run with --confirm-health-save.",
            ]
        )


def approval_path_for_journal(journal_root: Path | str) -> Path:
    """Return the health-import approval path for a journal root."""
    return Path(journal_root) / APPROVAL_RELATIVE_PATH


def enforce_pre_save_gate(
    importer: str | object,
    *,
    dry_run: bool,
    confirm_health_save: bool = False,
) -> PreSaveGateDecision:
    """Fail closed before save-mode writes for sensitive file importers.

    Non-sensitive importers and dry-run invocations bypass the gate.
    """
    importer_name = _importer_name(importer)
    if importer_name not in SENSITIVE_IMPORTERS or dry_run:
        return PreSaveGateDecision(
            importer=importer_name,
            enforced=False,
            approval_path=None,
            checklist_version=CHECKLIST_VERSION,
        )

    journal_root = Path(get_journal()).resolve()
    approval_path = approval_path_for_journal(journal_root)

    if not approval_path.is_file():
        _block(
            importer_name,
            "missing_approval_artifact",
            journal_root,
            approval_path,
            missing_fields=("approval_artifact",),
        )

    artifact = _read_artifact(importer_name, journal_root, approval_path)
    _validate_artifact(importer_name, journal_root, approval_path, artifact)

    if not confirm_health_save:
        _block(
            importer_name,
            "per_run_confirmation_missing",
            journal_root,
            approval_path,
            missing_fields=("confirm_health_save",),
        )

    return PreSaveGateDecision(
        importer=importer_name,
        enforced=True,
        approval_path=str(approval_path),
        checklist_version=CHECKLIST_VERSION,
    )


def _importer_name(importer: str | object) -> str:
    if isinstance(importer, str):
        return importer
    name = getattr(importer, "name", None)
    if isinstance(name, str) and name:
        return name
    raise TypeError("importer must be a name string or object with a non-empty name")


def _read_artifact(
    importer: str, journal_root: Path, approval_path: Path
) -> dict[str, Any]:
    try:
        data = json.loads(approval_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _block(importer, "invalid_approval_json", journal_root, approval_path)
    if not isinstance(data, dict):
        _block(
            importer,
            "invalid_approval_artifact",
            journal_root,
            approval_path,
            invalid_fields=("artifact",),
        )
    return data


def _validate_artifact(
    importer: str,
    journal_root: Path,
    approval_path: Path,
    artifact: dict[str, Any],
) -> None:
    if artifact.get("schema") != APPROVAL_SCHEMA:
        _block(
            importer,
            "unsupported_approval_schema",
            journal_root,
            approval_path,
            invalid_fields=("schema",),
        )

    if artifact.get("checklist_version") != CHECKLIST_VERSION:
        _block(
            importer,
            "checklist_version_mismatch",
            journal_root,
            approval_path,
            invalid_fields=("checklist_version",),
        )

    approved_target = artifact.get("target_journal_path")
    if approved_target is None:
        _block(
            importer,
            "target_journal_path_mismatch",
            journal_root,
            approval_path,
            missing_fields=("target_journal_path",),
        )
    if Path(str(approved_target)).resolve() != journal_root:
        _block(
            importer,
            "target_journal_path_mismatch",
            journal_root,
            approval_path,
            invalid_fields=("target_journal_path",),
        )

    approved_importers = artifact.get("approved_importers")
    if not isinstance(approved_importers, list) or importer not in approved_importers:
        _block(
            importer,
            "importer_not_approved",
            journal_root,
            approval_path,
            invalid_fields=("approved_importers",),
        )

    missing, invalid = _replication_decision_errors(
        artifact.get("replication_destinations")
    )
    if missing or invalid:
        _block(
            importer,
            "replication_decision_incomplete",
            journal_root,
            approval_path,
            missing_fields=tuple(missing),
            invalid_fields=tuple(invalid),
        )

    raw_retention = artifact.get("raw_retention")
    if (
        not isinstance(raw_retention, dict)
        or not isinstance(raw_retention.get("decision"), str)
        or not raw_retention["decision"].strip()
    ):
        _block(
            importer,
            "raw_retention_decision_missing",
            journal_root,
            approval_path,
            missing_fields=("raw_retention.decision",),
        )

    if artifact.get("requires_per_run_confirmation") is not True:
        _block(
            importer,
            "invalid_approval_artifact",
            journal_root,
            approval_path,
            invalid_fields=("requires_per_run_confirmation",),
        )


def _replication_decision_errors(
    destinations: object,
) -> tuple[list[str], list[str]]:
    if not isinstance(destinations, dict):
        return (["replication_destinations"], [])

    actual_destinations = set(destinations)
    expected_destinations = set(CHECKLIST_DESTINATIONS)
    missing = [
        f"replication_destinations.{name}"
        for name in CHECKLIST_DESTINATIONS
        if name not in actual_destinations
    ]
    invalid = [
        f"replication_destinations.{name}"
        for name in sorted(actual_destinations - expected_destinations)
    ]

    for name in CHECKLIST_DESTINATIONS:
        if name not in destinations:
            continue
        item = destinations[name]
        if isinstance(item, dict):
            decision = item.get("decision")
        else:
            decision = item
        if decision not in DESTINATION_DECISIONS:
            invalid.append(f"replication_destinations.{name}.decision")

    return (missing, invalid)


def _block(
    importer: str,
    reason: str,
    journal_root: Path,
    approval_path: Path,
    *,
    missing_fields: tuple[str, ...] = (),
    invalid_fields: tuple[str, ...] = (),
) -> None:
    raise PreSaveGateError(
        PreSaveGateFailure(
            importer=importer,
            reason=reason,
            approval_path=str(approval_path),
            target_journal=str(journal_root),
            missing_fields=missing_fields,
            invalid_fields=invalid_fields,
        )
    )
