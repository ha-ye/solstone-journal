#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Reject removed chat imports, names, and DOM literals.

This is a repository architecture check, not a unit test. It inventories tracked
Python, HTML, and JavaScript files once, parses each Python file once, and reports
every legacy chat surface still present.
"""

from __future__ import annotations

import ast
import subprocess
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _parts(*pieces: str) -> str:
    return "".join(pieces)


ALLOWED_UNIFIED_PATHS = {
    "apps/sol/maint/006_rename_unified_triage_providers.py",
    "tests/test_maint_006_rename_unified_triage_providers.py",
}
FORBIDDEN_CHAT_LITERALS = {
    "conversationBackdrop",
    "conversationMessages",
    "chatBarResponsePanel",
    "chatBarThinking",
    "chatBarResponse",
    "chatBarDismiss",
    "conversation-backdrop",
    "conversation-messages",
    "conversation-separator",
    "solstone:conversationState",
    "solstone:chatBarState",
    "panelFocusTrapHandler",
    "openPanel",
    "closePanel",
    "_closeConversationPanel",
}
BANNED_NAMES = {
    _parts("_", "display_", "mode"),
    _parts("record_", "exchange"),
    _parts("build_", "memory_", "context"),
    _parts("INJECTION_", "MARKER"),
    _parts("inject_", "memory"),
    _parts("get_", "recent_", "exchanges"),
    _parts("get_", "today_", "exchanges"),
    _parts("TRIAGE_", "AGENT_", "NAMES"),
    _parts("record_", "triage_", "exchange"),
    _parts("compute_", "display_", "mode"),
}
LEGACY_CHAT_MODULE = _parts("think", ".", "conversation")
LEGACY_MEMORY_MODULE = _parts("talent", ".", "conversation_", "memory")
LEGACY_NAME = _parts("uni", "fied")


def scan_python_source(source: str, relative_path: str) -> list[str]:
    """Return legacy Python findings for one source file."""
    try:
        tree = ast.parse(source, filename=relative_path)
    except SyntaxError:
        return []

    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {LEGACY_CHAT_MODULE, LEGACY_MEMORY_MODULE}:
                    findings.append(f"{relative_path}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in {LEGACY_CHAT_MODULE, LEGACY_MEMORY_MODULE}:
                findings.append(f"{relative_path}: from {node.module} import ...")
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            findings.append(f"{relative_path}: name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_NAMES:
            findings.append(f"{relative_path}: attribute {node.attr}")
        elif (
            isinstance(node, ast.Constant)
            and node.value == LEGACY_NAME
            and relative_path not in ALLOWED_UNIFIED_PATHS
        ):
            findings.append(f"{relative_path}: legacy literal")
    return findings


def scan_text_source(source: str, relative_path: str) -> list[str]:
    """Return legacy DOM findings for one HTML or JavaScript file."""
    if "tests/fixtures" in relative_path:
        return []
    return [
        f"{relative_path}: {literal}"
        for literal in sorted(FORBIDDEN_CHAT_LITERALS)
        if literal in source
    ]


def scan_paths(root: Path, relative_paths: Iterable[str]) -> list[str]:
    """Scan an already-discovered path inventory."""
    findings: list[str] = []
    for relative_path in relative_paths:
        path = root / relative_path
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            findings.extend(scan_python_source(source, relative_path))
        elif path.suffix in {".html", ".js"}:
            findings.extend(scan_text_source(source, relative_path))
    return findings


def tracked_source_paths(root: Path = ROOT) -> list[str]:
    """Return the tracked source inventory in one Git subprocess."""
    result = subprocess.run(
        ["git", "ls-files", "*.py", "*.html", "*.js"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def main() -> int:
    findings = scan_paths(ROOT, tracked_source_paths())
    if findings:
        print("Legacy chat surfaces remain:")
        for finding in findings:
            print(f"  {finding}")
        return 1
    print("No legacy chat surfaces found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
