# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Tests for backup app copy discipline."""

from __future__ import annotations

import json
import re
from pathlib import Path

from solstone.apps.backup.copy import backup_copy_payload, backup_copy_values


def _backup_js_text() -> str:
    return Path("solstone/apps/backup/static/backup.js").read_text(encoding="utf-8")


def _backup_copy_literal() -> dict:
    text = _backup_js_text()
    prefix = "  const BACKUP_COPY = "
    start = text.index(prefix) + len(prefix)
    payload, end = json.JSONDecoder().raw_decode(text[start:])
    assert text[start + end :].lstrip().startswith(";")
    return payload


def test_backup_copy_verbatim_strings() -> None:
    payload = backup_copy_payload()

    assert payload["service_name"] == "encrypted backup"
    assert payload["intro"]["title"] == "encrypted backup"
    # the journal-bound brand-lock — load-bearing trust beat (CSO-required)
    assert payload["brand_lock"] == "your journal is always private, only yours."
    assert (
        payload["intro"]["subtitle"]
        == "make an encrypted copy of your journal somewhere safe — only you can read it."
    )
    assert payload["intro"]["bullets"] == [
        "end-to-end encrypted",
        "optional, always",
        "delete anytime",
    ]
    # load-bearing honesty beats — must survive verbatim (CSO-required)
    assert (
        payload["educate"]["stakes"]
        == "if you lose your recovery key, no one can recover your journal — not even sol pbc."
    )
    assert (
        payload["key"]["theft_honesty"]
        == "anyone with your recovery key can read everything in your backup — store it like a master password."
    )
    assert payload["confirm"]["prompt"] == "enter the recovery key you just recorded."
    assert payload["confirm"]["escape"] == "see key again"
    assert (
        payload["key"]["pm_caution"]
        == "only store your recovery key in a password manager you trust. sol pbc doesn't recommend a specific one."
    )
    assert payload["management"]["destructive_action"] == "turn off & delete backup"
    assert (
        payload["management"]["destructive_caption"]
        == "this deletes all your backup data. no new backups will be created."
    )
    # the byo covenant beat — "sol pbc is never in the path" (mode selector)
    assert (
        payload["destination"]["modes"]["byo"]["note"]
        == "sol pbc is never in the path."
    )
    assert payload["destination"]["modes"]["byo"]["title"] == "your own"
    assert payload["destination"]["modes"]["hosted"]["title"] == "operated by sol pbc"
    assert (
        payload["destination"]["modes"]["hosted"]["note"]
        == "sol pbc only ever holds an encrypted copy it can't read."
    )
    assert payload["destination"]["modes"]["hosted"]["cta"] == "set up backup →"
    assert (
        payload["destination"]["object_lock_warning"]
        == "don't enable Compliance-mode Object Lock on the bucket — it conflicts with backup pruning and lock cleanup. if you need immutability, use Governance mode."
    )
    assert (
        payload["intro"]["optional"]
        == "your journal lives on your device; backup is optional."
    )
    assert payload["key"]["save_password_manager"] == "save to my password manager"
    assert payload["key"]["copy_label"] == "copy"
    assert payload["key"]["continue"] == "continue"
    assert payload["destination"]["field_labels"]["b2_key_id"] == "key id"
    assert (
        payload["destination"]["field_labels"]["b2_application_key"]
        == "application key"
    )


def test_operated_lane_copy_neutralizes_hosting_terms() -> None:
    banned = re.compile(
        r"hosting|hosted|operated backup|\bsign in\b|\bsubscribe\b",
        re.IGNORECASE,
    )
    offenders = [value for value in backup_copy_values() if banned.search(value)]
    assert offenders == []


def test_no_literal_copy_in_workspace_template() -> None:
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
    path = root / "workspace.html"
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


def test_backup_copy_json_round_trips_from_static_js() -> None:
    assert _backup_copy_literal() == backup_copy_payload()


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
            "hosted",
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
