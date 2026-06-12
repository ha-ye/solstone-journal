# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-facing copy constants for the backup app."""

from __future__ import annotations

from typing import Any

SERVICE_NAME = "private backup"

INTRO_SUBTITLE = (
    "Keep an encrypted copy of your journal somewhere safe — only you can read it."
)
INTRO_BULLETS = [
    "End-to-end encrypted",
    "Optional, always",
    "Delete anytime",
]
EDUCATE_STAKES = (
    "If you lose your recovery key, no one can recover your journal — not even sol pbc."
)
THEFT_HONESTY = "Anyone with your recovery key can read everything in your backup — store it like a master password."
CONFIRM_PROMPT = "Enter the recovery key you just recorded."
CONFIRM_ESCAPE = "See Key Again"
PM_CAUTION = "Only store your recovery key in a password manager you trust. sol pbc doesn't recommend a specific one."
DESTRUCTIVE_ACTION = "Turn Off & Delete Backup"
DESTRUCTIVE_CAPTION = (
    "This deletes all your backup data. No new backups will be created."
)
OBJECT_LOCK_WARNING = "Don't enable Compliance-mode Object Lock on the bucket — it conflicts with backup pruning and lock cleanup. If you need immutability, use Governance mode."
OPTIONAL_INVARIANT = "solstone runs on your machine; this is optional."
SAVE_PASSWORD_MANAGER = "Save to my password manager"
SAVE_COPY = "Copy"
SAVE_CONTINUE = "Continue"
CLIPBOARD_CAVEAT = (
    "Copying puts your recovery key on the clipboard — clear it after you save it."
)

PHASE_LABELS = {
    "setting_up": "setting up…",
    "restoring": "restoring…",
    "rotating": "making a new recovery key…",
    "tearing_down": "turning off…",
    "done": "done",
    "error": "couldn't finish",
    "loading": "loading…",
    "empty": "not set up yet",
}

DESTINATION_REASON_LABELS = {
    "repo_exists": "Destination is reachable and already set up.",
    "repo_missing": "Destination is reachable and needs setup.",
    "auth_failed": "The destination rejected the key or credentials. Check the recovery key and destination details.",
    "locked": "The destination is busy. Try again shortly.",
    "timeout": "The destination took too long to respond. Try again shortly.",
    "unreachable": "I couldn't reach the destination. Check the repository path and try again.",
}

OPERATION_REASON_LABELS = {
    "backup_busy": "Another backup task is already running. Try again in a moment.",
    "backup_not_confirmed": "Confirm your recovery key before turning on backup.",
    "backup_operation_failed": "I couldn't finish that backup action. Check the recovery key and destination, then try again.",
    "backup_unavailable": "I couldn't ask the background service to start a backup. Start it, then try again.",
    "invalid_key": "That recovery key didn't unlock the backup. Re-enter the key from your saved copy.",
    "invalid_config_value": "Use non-negative whole numbers, then save again.",
    "invalid_operation_for_state": "Finish the current backup setup step, then try again.",
    "invalid_request_value": "Check the destination details and try again.",
    "restic_unavailable": "I couldn't prepare the backup tool. Try again after setup finishes.",
    "repo_missing": "I couldn't find a backup repository at that destination.",
    "auth_failed": "That recovery key didn't unlock the backup. Check the key first, then the destination details.",
    "locked": "The destination is busy. Try again shortly.",
    "timeout": "The destination took too long to respond. Try again shortly.",
    "failed": "I couldn't finish the backup action. Check the recovery key and destination, then try again.",
    "incomplete": "The backup action didn't finish. You can try again.",
    "missing_required_field": "Fill in the required fields, then try again.",
    "recovery_key_mismatch": "That didn't match your recovery key. Re-enter the key from your saved copy.",
}

ACTION_LABELS = {
    "start": "Get Started",
    "understand": "I Understand",
    "save_destination": "Save Destination",
    "enable": "Turn On Backup",
    "backup_now": "Back Up Now",
    "view_key": "View Recovery Key",
    "rotate_key": "Regenerate Recovery Key",
    "teardown": DESTRUCTIVE_ACTION,
    "save_retention": "Save Retention",
    "restore": "Restore",
    "try_again": "Try Again",
    "cancel": "Cancel",
}

DESTINATION_FIELD_LABELS = {
    "repository": "Repository",
    "backend": "Backend",
    "s3": "S3",
    "b2": "B2",
    "access_key_id": "Access Key ID",
    "secret_access_key": "Secret Access Key",
    "b2_key_id": "Key ID",
    "b2_application_key": "Application Key",
}

RETENTION_FIELD_LABELS = {
    "hourly": "Hourly",
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
}

STATUS_LABELS = {
    "last_backup": "Last Backup",
    "last_prune": "Last Prune",
    "storage_used": "Storage Used",
    "snapshot_history": "Snapshot History",
    "not_available": "not yet available",
    "not_yet": "not yet",
    "enabled": "on",
    "disabled": "off",
    "destination": "Destination",
    "retention": "Retention",
    "setup": "Setup",
}

RESTORE_EXPECTATION = (
    "A large restore can take a while. You can leave this page open while it runs."
)
ERROR_INTRO = (
    "Start with the recovery key. If it still fails, check the destination details."
)


def backup_copy_payload() -> dict[str, Any]:
    """Return copy constants for templates and browser code."""

    return {
        "service_name": SERVICE_NAME,
        "intro": {
            "title": SERVICE_NAME,
            "subtitle": INTRO_SUBTITLE,
            "bullets": list(INTRO_BULLETS),
            "optional": OPTIONAL_INVARIANT,
        },
        "educate": {
            "stakes": EDUCATE_STAKES,
        },
        "key": {
            "theft_honesty": THEFT_HONESTY,
            "pm_caution": PM_CAUTION,
            "save_password_manager": SAVE_PASSWORD_MANAGER,
            "copy": SAVE_COPY,
            "continue": SAVE_CONTINUE,
            "clipboard_caveat": CLIPBOARD_CAVEAT,
        },
        "confirm": {
            "prompt": CONFIRM_PROMPT,
            "escape": CONFIRM_ESCAPE,
        },
        "destination": {
            "object_lock_warning": OBJECT_LOCK_WARNING,
            "field_labels": dict(DESTINATION_FIELD_LABELS),
            "reason_labels": dict(DESTINATION_REASON_LABELS),
        },
        "management": {
            "destructive_action": DESTRUCTIVE_ACTION,
            "destructive_caption": DESTRUCTIVE_CAPTION,
            "status_labels": dict(STATUS_LABELS),
            "retention_labels": dict(RETENTION_FIELD_LABELS),
        },
        "restore": {
            "expectation": RESTORE_EXPECTATION,
        },
        "phase_labels": dict(PHASE_LABELS),
        "operation_reason_labels": dict(OPERATION_REASON_LABELS),
        "action_labels": dict(ACTION_LABELS),
        "error_intro": ERROR_INTRO,
    }


def backup_copy_values() -> list[str]:
    """Return all verbatim copy values, flattening nested constants."""

    values: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(backup_copy_payload())
    return values
