# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Owner-facing copy constants for the backup app."""

from __future__ import annotations

from typing import Any

SERVICE_NAME = "solstone backup"

# The journal-bound brand-lock (entry point) — the trust promise binds to the
# journal (the memory store where Article 8 binds), never to the software.
JOURNAL_BRAND_LOCK = "your journal is always private, only yours."
INTRO_SUBTITLE = (
    "Make an encrypted copy of your journal somewhere safe — only you can read it."
)
INTRO_BULLETS = [
    "end-to-end encrypted",
    "optional, always",
    "delete anytime",
]
INTRO_STEPS = "you'll save a recovery key, then choose where your backup lives."
# The byo ⟷ solstone hosted mode selector (destination step). v1 is byo-only;
# the hosted lane is shown as an honest "coming later" state — never a dead
# "set up hosting" control, since the hosted service does not exist yet.
MODE_BYO_TITLE = "your own"
MODE_BYO_DESC = "your bucket, your credentials. the default."
# the byo covenant beat — load-bearing ("sol pbc is never in the path").
MODE_BYO_NOTE = "sol pbc is never in the path."
MODE_HOSTED_TITLE = "solstone hosted"
MODE_HOSTED_TAG = "coming later"
MODE_HOSTED_DESC = "sol pbc runs the off-machine part for you."
MODE_HOSTED_NOTE = "operated by sol pbc"
MODE_HOSTED_COMING = (
    "this isn't available yet. for now, solstone backup uses your own bucket "
    "— sol pbc is never in the path."
)
EDUCATE_STAKES = (
    "If you lose your recovery key, no one can recover your journal — not even sol pbc."
)
THEFT_HONESTY = "Anyone with your recovery key can read everything in your backup — store it like a master password."
CONFIRM_PROMPT = "Enter the recovery key you just recorded."
CONFIRM_ESCAPE = "see key again"
PM_CAUTION = "Only store your recovery key in a password manager you trust. sol pbc doesn't recommend a specific one."
DESTRUCTIVE_ACTION = "turn off & delete backup"
DESTRUCTIVE_CAPTION = (
    "This deletes all your backup data. No new backups will be created."
)
OBJECT_LOCK_WARNING = "Don't enable Compliance-mode Object Lock on the bucket — it conflicts with backup pruning and lock cleanup. If you need immutability, use Governance mode."
OBJECT_LOCK_SUMMARY = "bucket setup notes"
OPTIONAL_INVARIANT = "solstone runs on your machine; this is optional."
SAVE_PASSWORD_MANAGER = "save to my password manager"
SAVE_COPY = "copy"
SAVE_CONTINUE = "continue"
CLIPBOARD_CAVEAT = (
    "Copying puts your recovery key on the clipboard — clear it after you save it."
)
REPOSITORY_HINT = (
    "the restic repository for your bucket — e.g. s3:s3.amazonaws.com/your-bucket"
)
RETENTION_HINT = "how many recent copies to keep at each interval."

PHASE_LABELS = {
    "setting_up": "setting up your backup…",
    "restoring": "restoring your journal…",
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
    "start": "get started",
    "understand": "i understand",
    "save_destination": "save destination",
    "enable": "turn on backup",
    "backup_now": "back up now",
    "view_key": "view recovery key",
    "rotate_key": "regenerate recovery key",
    "teardown": DESTRUCTIVE_ACTION,
    "save_retention": "save retention",
    "restore": "restore",
    "try_again": "try again",
    "cancel": "cancel",
    "use_byo": "use your own bucket",
}

DESTINATION_FIELD_LABELS = {
    "repository": "repository",
    "backend": "backend",
    "s3": "S3",
    "b2": "B2",
    "access_key_id": "access key id",
    "secret_access_key": "secret access key",
    "b2_key_id": "key id",
    "b2_application_key": "application key",
}

RETENTION_FIELD_LABELS = {
    "hourly": "hourly",
    "daily": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
}

STATUS_LABELS = {
    "last_backup": "last backup",
    "last_prune": "last prune",
    "storage_used": "storage used",
    "snapshot_history": "snapshot history",
    "not_available": "not yet available",
    "not_yet": "not yet",
    "enabled": "on",
    "disabled": "off",
    "destination": "where your backup lives",
    "retention": "retention",
    "setup": "set up your recovery key",
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
        "brand_lock": JOURNAL_BRAND_LOCK,
        "intro": {
            "title": SERVICE_NAME,
            "subtitle": INTRO_SUBTITLE,
            "bullets": list(INTRO_BULLETS),
            "optional": OPTIONAL_INVARIANT,
            "steps": INTRO_STEPS,
        },
        "educate": {
            "stakes": EDUCATE_STAKES,
        },
        "key": {
            "theft_honesty": THEFT_HONESTY,
            "pm_caution": PM_CAUTION,
            "save_password_manager": SAVE_PASSWORD_MANAGER,
            "copy_label": SAVE_COPY,
            "continue": SAVE_CONTINUE,
            "clipboard_caveat": CLIPBOARD_CAVEAT,
        },
        "confirm": {
            "prompt": CONFIRM_PROMPT,
            "escape": CONFIRM_ESCAPE,
        },
        "destination": {
            "repository_hint": REPOSITORY_HINT,
            "object_lock_warning": OBJECT_LOCK_WARNING,
            "object_lock_summary": OBJECT_LOCK_SUMMARY,
            "field_labels": dict(DESTINATION_FIELD_LABELS),
            "reason_labels": dict(DESTINATION_REASON_LABELS),
            "modes": {
                "byo": {
                    "title": MODE_BYO_TITLE,
                    "desc": MODE_BYO_DESC,
                    "note": MODE_BYO_NOTE,
                },
                "hosted": {
                    "title": MODE_HOSTED_TITLE,
                    "tag": MODE_HOSTED_TAG,
                    "desc": MODE_HOSTED_DESC,
                    "note": MODE_HOSTED_NOTE,
                    "coming": MODE_HOSTED_COMING,
                },
            },
        },
        "management": {
            "destructive_action": DESTRUCTIVE_ACTION,
            "destructive_caption": DESTRUCTIVE_CAPTION,
            "retention_hint": RETENTION_HINT,
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
