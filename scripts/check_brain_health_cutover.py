#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Guard the active-brain health cutover."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_SUFFIXES = {".py", ".md", ".js", ".html"}
LEGACY_HEALTH_FILE = "talents" + ".json"
PROVIDER_CLI_TOKEN = "providers" + "_cli"
PROVIDER_WORD = "providers"
CHECK_WORD = "check"
PROVIDER_CHECK_TEXT = PROVIDER_WORD + " " + CHECK_WORD
JOURNAL_PROVIDER_CHECK = "jour" + "nal " + PROVIDER_CHECK_TEXT
SOL_PROVIDER_CHECK = "s" + "ol " + PROVIDER_CHECK_TEXT
PROVIDER_CHECK_PREFIXES = (
    tuple(JOURNAL_PROVIDER_CHECK.split()),
    tuple(SOL_PROVIDER_CHECK.split()),
)
OWNER_LABELS = ("Provider " + "Readiness", "Agents " + "Health")
LEGACY_QUOTED_KEYS = (
    '"' + "provider" + "_readiness" + '"',
    "'" + "provider" + "_readiness" + "'",
    '"' + "ai" + "_readiness" + '"',
    "'" + "ai" + "_readiness" + "'",
)
COMMAND_LIST_RE = re.compile(
    r"\[\s*['\"](?:jour"
    r"nal|s"
    r"ol)['\"]\s*,\s*['\"]providers['\"]\s*,\s*['\"]check['\"]"
)
BRAIN_READER_ALLOWLIST = {
    "solstone/apps/health/routes.py",
    "solstone/apps/home/routes.py",
    "solstone/apps/support/diagnostics.py",
    "solstone/apps/thinking/routes.py",
    "solstone/think/brain_cli.py",
    "solstone/think/brain_health.py",
    "solstone/think/cortex.py",
    "solstone/think/doctor.py",
    "solstone/think/surfaces/health.py",
    "solstone/think/top.py",
}


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str
    detail: str


def _tracked_files(root: Path, *, all_files: bool) -> list[Path]:
    if all_files:
        return [
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix in SOURCE_SUFFIXES
        ]
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    files: list[Path] = []
    for line in result.stdout.splitlines():
        if not line or Path(line).suffix not in SOURCE_SUFFIXES:
            continue
        path = root / line
        if path.is_file():
            files.append(path)
    return files


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_test_path(rel: str) -> bool:
    parts = Path(rel).parts
    return "tests" in parts or Path(rel).name.startswith("test_")


def _contains_command_list(path: Path, text: str) -> bool:
    if path.suffix == ".py":
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            return False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) < 3:
                continue
            values = [
                item.value if isinstance(item, ast.Constant) else None
                for item in node.elts[:3]
            ]
            if tuple(values) in PROVIDER_CHECK_PREFIXES:
                return True
        return False
    return COMMAND_LIST_RE.search(text) is not None


def _imported_names(path: Path) -> set[str]:
    if path.suffix != ".py":
        return set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.rsplit(".", 1)[-1])
    return names


def scan(root: Path, *, all_files: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    for path in _tracked_files(root, all_files=all_files):
        rel = _rel(root, path)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        if LEGACY_HEALTH_FILE in text:
            findings.append(Finding(rel, "legacy-health-file", LEGACY_HEALTH_FILE))

        if PROVIDER_CLI_TOKEN in text:
            findings.append(Finding(rel, "legacy-provider-cli", PROVIDER_CLI_TOKEN))
        if JOURNAL_PROVIDER_CHECK in text or SOL_PROVIDER_CHECK in text:
            findings.append(
                Finding(rel, "legacy-provider-check-text", PROVIDER_CHECK_TEXT)
            )
        if _contains_command_list(path, text):
            findings.append(
                Finding(rel, "legacy-provider-check-cmd", PROVIDER_CHECK_TEXT)
            )

        for label in OWNER_LABELS:
            if label in text:
                findings.append(Finding(rel, "legacy-owner-label", label))
        for key in LEGACY_QUOTED_KEYS:
            if key in text:
                findings.append(Finding(rel, "legacy-payload-key", key))

        if rel.startswith(("solstone/", "scripts/")) and not _is_test_path(rel):
            imported = _imported_names(path)
            if {"inspect_brain_state", "build_brain_snapshot"} & imported:
                if rel not in BRAIN_READER_ALLOWLIST:
                    findings.append(
                        Finding(
                            rel,
                            "unauthorized-brain-health-reader",
                            ", ".join(
                                sorted(
                                    {"inspect_brain_state", "build_brain_snapshot"}
                                    & imported
                                )
                            ),
                        )
                    )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="Scan source files under --root instead of git-tracked files.",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    findings = scan(root, all_files=args.all_files)
    if not findings:
        return 0
    print("brain-health cutover guard failed:")
    for finding in findings:
        print(f"  {finding.path}: {finding.rule}: {finding.detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
