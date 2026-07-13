# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Pre-save approval gates for sensitive health importers and syncs.

Two artifact families live under ``imports/_approvals/``, both fail-closed
and read-only from this module (creating an artifact is a separately
approved setup action):

- ``health_import_preflight.json`` — file-import save mode for the
  importers in ``SENSITIVE_IMPORTERS`` (checked by
  :func:`enforce_pre_save_gate`).
- ``oura_sync_preflight.json`` — the Oura API sync lane's own artifact
  (checked by :func:`enforce_oura_sync_gate`), same schema family, plus a
  distinct standing-consent block for *scheduled* (unattended) runs.

Journal-path binding (checklist v3)
-----------------------------------

Every artifact binds to the exact journal it authorizes via a
``journal_root`` field; the gate verifies that binding against the target
journal root that will actually be written (root-explicit — an artifact
copied into another journal never authorizes it). Older checklist versions
fail closed; ``target_journal_path`` is not accepted.

Scheduled-sync consent
----------------------

Owner-present one-shot sync saves require the per-run
``--confirm-health-save`` flag. Scheduled (``--scheduled``) sync runs
cannot click "yes", so they are authorized only by an explicit standing
consent recorded in the sync artifact::

    {"scheduled_sync": {
        "approved": true,
        "cadence": "every 6 hours",
        "valid_until": "2026-08-01T00:00:00-06:00"
    }}

A scheduled run without that consent fails closed regardless of any
other approval in the artifact.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from solstone.think.importers.health_schema import (
    SOURCE_APPLE_HEALTH,
    SOURCE_OURA,
)
from solstone.think.utils import get_journal

APPROVAL_SCHEMA = "solstone.health_import_preflight.v1"
CHECKLIST_VERSION = "solstone.health_import_preflight.checklist.v3"
APPROVAL_RELATIVE_PATH = Path("imports") / "_approvals" / "health_import_preflight.json"

OURA_SYNC_APPROVAL_SCHEMA = "solstone.oura_sync_preflight.v1"
OURA_SYNC_CHECKLIST_VERSION = "solstone.oura_sync_preflight.checklist.v2"
OURA_SYNC_APPROVAL_RELATIVE_PATH = (
    Path("imports") / "_approvals" / "oura_sync_preflight.json"
)

CHECKLIST_DESTINATIONS = (
    "time_machine",
    "icloud",
    "solbase",
    "hosted_backup",
    "other",
)
DESTINATION_DECISIONS = {"approved", "excluded"}
# Approval artifacts are keyed by importer/backend names, not source families.
# Do not derive this from KNOWN_SOURCE_FAMILIES: oura_api and dexcom_clarity
# are source families with no approval-artifact importer name.
SENSITIVE_IMPORTERS: Final = frozenset({"apple_health", "oura"})


class RawRetentionDecision(StrEnum):
    DISCARD = "discard"
    RETAIN_PARSED = "retain_parsed"
    RETAIN_COMPLETE = "retain_complete"


@dataclass(frozen=True, slots=True)
class PreSaveGateDecision:
    """Successful gate result for a save attempt."""

    importer: str
    enforced: bool
    approval_path: str | None
    checklist_version: str
    raw_retention: RawRetentionDecision | None = None
    scheduled_sync: "ScheduledSyncConsent | None" = None


@dataclass(frozen=True, slots=True)
class ScheduledSyncConsent:
    cadence: str
    valid_until: dt.datetime


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
    flow: str = "import"

    def to_dict(self) -> dict[str, Any]:
        return {
            "skipped": True,
            "reason": "health_pre_save_gate_required",
            "gate_reason": self.reason,
            "importer": self.importer,
            "flow": self.flow,
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

        if self.failure.flow == "sync":
            headline = "Health sync save blocked before journal write."
            next_steps = [
                "  1. Run catalog (dry-run) sync only.",
                "  2. Complete the journal replication and raw-retention review.",
                "  3. Record the sync approval artifact.",
                "  4. Re-run with --save --confirm-health-save (owner present),",
                "     or record scheduled_sync consent for --scheduled runs.",
            ]
        else:
            headline = "Health import save blocked before journal write."
            next_steps = [
                "  1. Run dry-run preview only.",
                "  2. Complete the journal replication and raw-retention review.",
                "  3. Record the approval artifact.",
                "  4. Re-run with --confirm-health-save.",
            ]
        return "\n".join(
            [
                headline,
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
                *next_steps,
            ]
        )


def approval_path_for_journal(journal_root: Path | str) -> Path:
    """Return the health-import approval path for a journal root."""
    return Path(journal_root) / APPROVAL_RELATIVE_PATH


def oura_sync_approval_path_for_journal(journal_root: Path | str) -> Path:
    """Return the Oura sync approval path for a journal root."""
    return Path(journal_root) / OURA_SYNC_APPROVAL_RELATIVE_PATH


def read_oura_sync_approval(journal_root: Path | str) -> dict[str, Any] | None:
    """Read the Oura sync approval artifact without validating it.

    Read-only convenience for status surfaces (e.g. the scheduled-sync
    cron guidance). Gate decisions always go through
    :func:`enforce_oura_sync_gate` instead.
    """

    approval_path = oura_sync_approval_path_for_journal(journal_root)
    if not approval_path.is_file():
        return None
    try:
        loaded = json.loads(approval_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return loaded if isinstance(loaded, dict) else None


def enforce_pre_save_gate(
    importer: str | object,
    *,
    dry_run: bool,
    confirm_health_save: bool = False,
    journal_root: Path | str | None = None,
) -> PreSaveGateDecision:
    """Fail closed before save-mode writes for sensitive file importers.

    Non-sensitive importers and dry-run invocations bypass the gate.
    ``journal_root`` is the exact target the caller intends to write
    (root-explicit); when omitted it resolves via ``get_journal()``.
    """
    importer_name = _importer_name(importer)
    if importer_name not in SENSITIVE_IMPORTERS or dry_run:
        return PreSaveGateDecision(
            importer=importer_name,
            enforced=False,
            approval_path=None,
            checklist_version=CHECKLIST_VERSION,
        )

    target_root = Path(journal_root if journal_root is not None else get_journal())
    if not target_root.is_absolute():
        _block(
            importer_name,
            "target_journal_not_absolute",
            target_root,
            approval_path_for_journal(target_root),
            invalid_fields=("journal_root",),
        )
    target_root = target_root.resolve()
    approval_path = approval_path_for_journal(target_root)

    if not approval_path.is_file():
        _block(
            importer_name,
            "missing_approval_artifact",
            target_root,
            approval_path,
            missing_fields=("approval_artifact",),
        )

    artifact = _read_artifact(importer_name, target_root, approval_path)
    raw_retention = _validate_artifact(
        importer_name, target_root, approval_path, artifact
    )

    if not confirm_health_save:
        _block(
            importer_name,
            "per_run_confirmation_missing",
            target_root,
            approval_path,
            missing_fields=("confirm_health_save",),
        )

    return PreSaveGateDecision(
        importer=importer_name,
        enforced=True,
        approval_path=str(approval_path),
        checklist_version=CHECKLIST_VERSION,
        raw_retention=raw_retention,
    )


def enforce_oura_sync_gate(
    journal_root: Path | str,
    *,
    confirm_health_save: bool = False,
    scheduled: bool = False,
    now: dt.datetime | None = None,
) -> PreSaveGateDecision:
    """Fail closed before any Oura sync save-mode journal write.

    Only save-mode sync calls this — catalog (dry-run) sync writes
    nothing, including no cursor, so it never needs approval. Owner-present
    one-shot runs require the per-run ``confirm_health_save`` flag;
    ``scheduled`` runs instead require the artifact's standing
    ``scheduled_sync`` consent (a cron job cannot click "yes").
    """

    target_root = Path(journal_root)
    if not target_root.is_absolute():
        _block(
            "oura",
            "target_journal_not_absolute",
            target_root,
            oura_sync_approval_path_for_journal(target_root),
            invalid_fields=("journal_root",),
            flow="sync",
        )
    target_root = target_root.resolve()
    approval_path = oura_sync_approval_path_for_journal(target_root)
    resolved_now = _resolve_gate_now(now)

    if not approval_path.is_file():
        _block(
            "oura",
            "missing_approval_artifact",
            target_root,
            approval_path,
            missing_fields=("approval_artifact",),
            flow="sync",
        )

    artifact = _read_artifact("oura", target_root, approval_path, flow="sync")

    if artifact.get("schema") != OURA_SYNC_APPROVAL_SCHEMA:
        _block(
            "oura",
            "unsupported_approval_schema",
            target_root,
            approval_path,
            invalid_fields=("schema",),
            flow="sync",
        )
    if artifact.get("checklist_version") != OURA_SYNC_CHECKLIST_VERSION:
        _block(
            "oura",
            "checklist_version_mismatch",
            target_root,
            approval_path,
            invalid_fields=("checklist_version",),
            flow="sync",
        )

    _validate_journal_root_binding(
        "oura", target_root, approval_path, artifact, flow="sync"
    )
    raw_retention = _validate_shared_checklist(
        "oura", target_root, approval_path, artifact, flow="sync"
    )

    scheduled_sync = _validated_optional_scheduled_sync(
        target_root,
        approval_path,
        artifact,
        now=resolved_now,
    )
    if scheduled:
        scheduled_sync = _validate_scheduled_sync_consent(
            target_root, approval_path, artifact, now=resolved_now
        )
    elif not confirm_health_save:
        _block(
            "oura",
            "per_run_confirmation_missing",
            target_root,
            approval_path,
            missing_fields=("confirm_health_save",),
            flow="sync",
        )

    return PreSaveGateDecision(
        importer="oura",
        enforced=True,
        approval_path=str(approval_path),
        checklist_version=OURA_SYNC_CHECKLIST_VERSION,
        raw_retention=raw_retention,
        scheduled_sync=scheduled_sync,
    )


def _resolve_gate_now(now: dt.datetime | None) -> dt.datetime:
    resolved = now if now is not None else dt.datetime.now(dt.UTC)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return resolved.astimezone(dt.UTC)


def _validated_optional_scheduled_sync(
    target_root: Path,
    approval_path: Path,
    artifact: dict[str, Any],
    *,
    now: dt.datetime,
) -> ScheduledSyncConsent | None:
    consent = artifact.get("scheduled_sync")
    if not isinstance(consent, dict) or consent.get("approved") is not True:
        return None
    return _validate_scheduled_sync_consent(
        target_root, approval_path, artifact, now=now
    )


def _validate_scheduled_sync_consent(
    target_root: Path,
    approval_path: Path,
    artifact: dict[str, Any],
    *,
    now: dt.datetime,
) -> ScheduledSyncConsent:
    consent = artifact.get("scheduled_sync")
    if not isinstance(consent, dict):
        _block(
            "oura",
            "scheduled_sync_consent_missing",
            target_root,
            approval_path,
            missing_fields=("scheduled_sync",),
            flow="sync",
        )
    if consent.get("approved") is not True:
        _block(
            "oura",
            "scheduled_sync_not_approved",
            target_root,
            approval_path,
            invalid_fields=("scheduled_sync.approved",),
            flow="sync",
        )
    cadence = consent.get("cadence")
    if not isinstance(cadence, str) or not cadence.strip():
        _block(
            "oura",
            "scheduled_sync_cadence_invalid",
            target_root,
            approval_path,
            invalid_fields=("scheduled_sync.cadence",),
            flow="sync",
        )
    valid_until = _parse_scheduled_valid_until(
        target_root,
        approval_path,
        consent.get("valid_until"),
    )
    if now >= valid_until.astimezone(dt.UTC):
        _block(
            "oura",
            "scheduled_sync_consent_expired",
            target_root,
            approval_path,
            invalid_fields=("scheduled_sync.valid_until",),
            flow="sync",
        )
    return ScheduledSyncConsent(cadence=cadence.strip(), valid_until=valid_until)


def _parse_scheduled_valid_until(
    target_root: Path,
    approval_path: Path,
    value: object,
) -> dt.datetime:
    if value is None:
        _block(
            "oura",
            "scheduled_sync_valid_until_missing",
            target_root,
            approval_path,
            missing_fields=("scheduled_sync.valid_until",),
            flow="sync",
        )
    if not isinstance(value, str) or not value.strip():
        _block(
            "oura",
            "scheduled_sync_valid_until_invalid",
            target_root,
            approval_path,
            invalid_fields=("scheduled_sync.valid_until",),
            flow="sync",
        )
    raw = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(
            raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        )
    except ValueError:
        _block(
            "oura",
            "scheduled_sync_valid_until_invalid",
            target_root,
            approval_path,
            invalid_fields=("scheduled_sync.valid_until",),
            flow="sync",
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _block(
            "oura",
            "scheduled_sync_valid_until_naive",
            target_root,
            approval_path,
            invalid_fields=("scheduled_sync.valid_until",),
            flow="sync",
        )
    return parsed


def _importer_name(importer: str | object) -> str:
    if isinstance(importer, str):
        return importer
    name = getattr(importer, "name", None)
    if isinstance(name, str) and name:
        return name
    raise TypeError("importer must be a name string or object with a non-empty name")


def _read_artifact(
    importer: str,
    journal_root: Path,
    approval_path: Path,
    *,
    flow: str = "import",
) -> dict[str, Any]:
    try:
        data = json.loads(approval_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _block(
            importer, "invalid_approval_json", journal_root, approval_path, flow=flow
        )
    if not isinstance(data, dict):
        _block(
            importer,
            "invalid_approval_artifact",
            journal_root,
            approval_path,
            invalid_fields=("artifact",),
            flow=flow,
        )
    return data


def _validate_artifact(
    importer: str,
    journal_root: Path,
    approval_path: Path,
    artifact: dict[str, Any],
) -> RawRetentionDecision:
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

    _validate_journal_root_binding(importer, journal_root, approval_path, artifact)
    raw_retention = _validate_shared_checklist(
        importer, journal_root, approval_path, artifact
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
    return raw_retention


def _validate_journal_root_binding(
    importer: str,
    journal_root: Path,
    approval_path: Path,
    artifact: dict[str, Any],
    *,
    flow: str = "import",
) -> None:
    """Verify the artifact's recorded journal binding against the target."""

    bound = artifact.get("journal_root")
    if bound is None:
        _block(
            importer,
            "journal_root_binding_missing",
            journal_root,
            approval_path,
            missing_fields=("journal_root",),
            flow=flow,
        )
    bound_path = Path(str(bound))
    if not bound_path.is_absolute():
        _block(
            importer,
            "journal_root_binding_not_absolute",
            journal_root,
            approval_path,
            invalid_fields=("journal_root",),
            flow=flow,
        )
    if bound_path.resolve() != journal_root:
        _block(
            importer,
            "journal_root_binding_mismatch",
            journal_root,
            approval_path,
            invalid_fields=("journal_root",),
            flow=flow,
        )


def _validate_shared_checklist(
    importer: str,
    journal_root: Path,
    approval_path: Path,
    artifact: dict[str, Any],
    *,
    flow: str = "import",
) -> RawRetentionDecision:
    """Checks shared by both artifact kinds (replication, retention, per-run)."""

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
            flow=flow,
        )

    raw_retention = _validate_raw_retention(
        importer,
        journal_root,
        approval_path,
        artifact.get("raw_retention"),
        flow=flow,
    )

    if artifact.get("requires_per_run_confirmation") is not True:
        _block(
            importer,
            "invalid_approval_artifact",
            journal_root,
            approval_path,
            invalid_fields=("requires_per_run_confirmation",),
            flow=flow,
        )
    return raw_retention


def _validate_raw_retention(
    importer: str,
    journal_root: Path,
    approval_path: Path,
    raw_retention: object,
    *,
    flow: str,
) -> RawRetentionDecision:
    if not isinstance(raw_retention, dict):
        _block(
            importer,
            "raw_retention_decision_missing",
            journal_root,
            approval_path,
            missing_fields=("raw_retention.decision",),
            flow=flow,
        )
    raw_decision = raw_retention.get("decision")
    if not isinstance(raw_decision, str) or not raw_decision.strip():
        _block(
            importer,
            "raw_retention_decision_missing",
            journal_root,
            approval_path,
            missing_fields=("raw_retention.decision",),
            flow=flow,
        )
    try:
        decision = RawRetentionDecision(raw_decision.strip())
    except ValueError:
        _block(
            importer,
            "raw_retention_decision_invalid",
            journal_root,
            approval_path,
            invalid_fields=("raw_retention.decision",),
            flow=flow,
        )
    if importer == SOURCE_OURA and decision is RawRetentionDecision.RETAIN_COMPLETE:
        _block(
            importer,
            "raw_retention_decision_incompatible",
            journal_root,
            approval_path,
            invalid_fields=("raw_retention.decision",),
            flow=flow,
        )
    if decision is RawRetentionDecision.RETAIN_COMPLETE:
        ack_field = "raw_retention.unparsed_sensitive_modalities_acknowledged"
        if "unparsed_sensitive_modalities_acknowledged" not in raw_retention:
            _block(
                importer,
                "raw_retention_acknowledgement_missing",
                journal_root,
                approval_path,
                missing_fields=(ack_field,),
                flow=flow,
            )
        if raw_retention.get("unparsed_sensitive_modalities_acknowledged") is not True:
            _block(
                importer,
                "raw_retention_acknowledgement_missing",
                journal_root,
                approval_path,
                invalid_fields=(ack_field,),
                flow=flow,
            )
    if importer not in {SOURCE_APPLE_HEALTH, SOURCE_OURA}:
        _block(
            importer,
            "importer_not_approved",
            journal_root,
            approval_path,
            invalid_fields=("importer",),
            flow=flow,
        )
    return decision


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
    flow: str = "import",
) -> None:
    raise PreSaveGateError(
        PreSaveGateFailure(
            importer=importer,
            reason=reason,
            approval_path=str(approval_path),
            target_journal=str(journal_root),
            missing_fields=missing_fields,
            invalid_fields=invalid_fields,
            checklist_version=(
                OURA_SYNC_CHECKLIST_VERSION if flow == "sync" else CHECKLIST_VERSION
            ),
            flow=flow,
        )
    )
