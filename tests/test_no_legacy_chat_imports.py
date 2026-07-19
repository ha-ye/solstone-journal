# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Unit tests for the legacy-chat repository scanner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_no_legacy_chat.py"

spec = importlib.util.spec_from_file_location("check_no_legacy_chat", SCRIPT)
assert spec and spec.loader
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_python_scan_rejects_legacy_imports_names_and_literal() -> None:
    source = "\n".join(
        [
            "import think.conversation",
            "from talent.conversation_memory import load",
            "record_exchange()",
            "value = 'unified'",
        ]
    )

    findings = checker.scan_python_source(source, "solstone/example.py")

    assert set(findings) == {
        "solstone/example.py: import think.conversation",
        "solstone/example.py: from talent.conversation_memory import ...",
        "solstone/example.py: name record_exchange",
        "solstone/example.py: legacy literal",
    }


def test_python_scan_allows_migration_literal() -> None:
    path = "apps/sol/maint/006_rename_unified_triage_providers.py"

    assert checker.scan_python_source("value = 'unified'\n", path) == []


def test_python_scan_ignores_clean_source_and_unparseable_templates() -> None:
    assert (
        checker.scan_python_source("def clean():\n    return 'chat'\n", "clean.py")
        == []
    )
    assert checker.scan_python_source("{{ template }}", "template.py") == []


def test_text_scan_rejects_legacy_dom_literals() -> None:
    findings = checker.scan_text_source(
        "<div id='conversationMessages'></div>\nopenPanel();",
        "solstone/apps/example/workspace.html",
    )

    assert findings == [
        "solstone/apps/example/workspace.html: conversationMessages",
        "solstone/apps/example/workspace.html: openPanel",
    ]


def test_text_scan_ignores_fixture_content() -> None:
    assert (
        checker.scan_text_source(
            "<div id='conversationMessages'></div>",
            "tests/fixtures/example.html",
        )
        == []
    )
