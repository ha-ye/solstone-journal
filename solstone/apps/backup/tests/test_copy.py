# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for backup app copy discipline."""

from __future__ import annotations

import json
import re
from pathlib import Path

from solstone.apps.backup.copy import backup_copy_payload, backup_copy_values


def test_backup_copy_verbatim_strings() -> None:
    payload = backup_copy_payload()

    assert payload["service_name"] == "solstone backup"
    assert payload["intro"]["title"] == "solstone backup"
    # the journal-bound brand-lock — load-bearing trust beat (CSO-required)
    assert payload["brand_lock"] == "your journal is always private, only yours."
    assert (
        payload["intro"]["subtitle"]
        == "Make an encrypted copy of your journal somewhere safe — only you can read it."
    )
    assert payload["intro"]["bullets"] == [
        "end-to-end encrypted",
        "optional, always",
        "delete anytime",
    ]
    # load-bearing honesty beats — must survive verbatim (CSO-required)
    assert (
        payload["educate"]["stakes"]
        == "If you lose your recovery key, no one can recover your journal — not even sol pbc."
    )
    assert (
        payload["key"]["theft_honesty"]
        == "Anyone with your recovery key can read everything in your backup — store it like a master password."
    )
    assert payload["confirm"]["prompt"] == "Enter the recovery key you just recorded."
    assert payload["confirm"]["escape"] == "see key again"
    assert (
        payload["key"]["pm_caution"]
        == "Only store your recovery key in a password manager you trust. sol pbc doesn't recommend a specific one."
    )
    assert payload["management"]["destructive_action"] == "turn off & delete backup"
    assert (
        payload["management"]["destructive_caption"]
        == "This deletes all your backup data. No new backups will be created."
    )
    # the byo covenant beat — "sol pbc is never in the path" (mode selector)
    assert (
        payload["destination"]["modes"]["byo"]["note"]
        == "sol pbc is never in the path."
    )
    assert payload["destination"]["modes"]["byo"]["title"] == "your own"
    assert payload["destination"]["modes"]["hosted"]["title"] == "solstone hosted"
    assert payload["destination"]["modes"]["hosted"]["note"] == "operated by sol pbc"
    assert (
        payload["destination"]["object_lock_warning"]
        == "Don't enable Compliance-mode Object Lock on the bucket — it conflicts with backup pruning and lock cleanup. If you need immutability, use Governance mode."
    )
    assert (
        payload["intro"]["optional"]
        == "solstone runs on your machine; this is optional."
    )
    assert payload["key"]["save_password_manager"] == "save to my password manager"
    assert payload["key"]["copy_label"] == "copy"
    assert payload["key"]["continue"] == "continue"
    assert payload["destination"]["field_labels"]["b2_key_id"] == "key id"
    assert (
        payload["destination"]["field_labels"]["b2_application_key"]
        == "application key"
    )


def test_no_literal_copy_in_templates_or_static() -> None:
    root = Path("solstone/apps/backup")
    structural_values = {
        "B2",
        "S3",
        "Copy",
        "Restore",
        "done",
        "couldn't finish",
        "loading…",
        "not yet",
        "not yet available",
        "off",
        "on",
        # lowercased labels that coincide with structural code tokens
        # (form field names / panel + route names in backup.js), not display leaks
        "backend",
        "repository",
        "restore",
    }
    hits: list[tuple[Path, str]] = []
    for path in [root / "workspace.html", root / "static" / "backup.js"]:
        text = path.read_text(encoding="utf-8")
        for value in backup_copy_values():
            if not value or value in structural_values:
                continue
            literal_patterns = (
                re.compile(rf">\s*{re.escape(value)}\s*<"),
                re.compile(rf"(?<!=)['\"`]{re.escape(value)}['\"`]"),
            )
            if any(pattern.search(text) for pattern in literal_patterns):
                hits.append((path, value))

    assert hits == []


def test_backup_copy_json_round_trips_from_rendered_page(backup_env) -> None:
    env = backup_env()

    response = env.client.get("/app/backup/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    match = re.search(r"const BACKUP_COPY = (\{.*\});", html)
    assert match, "BACKUP_COPY assignment not found"
    assert json.loads(match.group(1)) == backup_copy_payload()


def test_all_copy_constants_referenced_by_render_surface() -> None:
    html = Path("solstone/apps/backup/workspace.html").read_text(encoding="utf-8")
    static = Path("solstone/apps/backup/static/backup.js").read_text(encoding="utf-8")
    surface = html + "\n" + static

    missing = [
        key
        for key in (
            "intro",
            "educate",
            "key",
            "confirm",
            "destination",
            "management",
            "restore",
            "phase_labels",
            "operation_reason_labels",
            "action_labels",
            "error_intro",
        )
        if key not in surface
    ]

    assert missing == []
