#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Built-in ``sol call`` tools HTTP-only lint."""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TARGETS: tuple[str, ...] = (
    "solstone/think/tools/health.py",
    "solstone/think/tools/ledger.py",
    "solstone/think/tools/profile.py",
)
ALLOW_SET: frozenset[str] = frozenset(
    {
        "solstone.think.convey_client",
        "solstone.convey.reasons",
        "solstone.convey.readiness_snapshot",
        "solstone.think.pipeline_health",
    }
)
FLAGGED_NAMESPACES: tuple[str, ...] = (
    "solstone.think",
    "solstone.apps",
    "solstone.convey",
)
IO_METHODS: frozenset[str] = frozenset(
    {
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "open",
        "unlink",
        "mkdir",
        "rmdir",
        "touch",
        "glob",
        "iterdir",
        "rglob",
    }
)
OS_FS_FUNCS: frozenset[str] = frozenset(
    {
        "remove",
        "unlink",
        "mkdir",
        "makedirs",
        "rmdir",
        "removedirs",
        "rename",
        "renames",
        "replace",
        "symlink",
        "link",
        "listdir",
        "scandir",
        "walk",
        "chmod",
        "chown",
        "truncate",
        "mkfifo",
        "mknod",
        "open",
    }
)
SHUTIL_FS_FUNCS: frozenset[str] = frozenset(
    {
        "move",
        "copy",
        "copy2",
        "copyfile",
        "copytree",
        "rmtree",
        "copyfileobj",
        "make_archive",
        "unpack_archive",
        "copymode",
        "copystat",
        "chown",
    }
)


def _is_under_namespace(module: str) -> bool:
    return any(
        module == namespace or module.startswith(f"{namespace}.")
        for namespace in FLAGGED_NAMESPACES
    )


def _collect_bindings(
    tree: ast.AST,
) -> tuple[set[str], dict[str, str], set[str], dict[str, str]]:
    os_aliases: set[str] = set()
    os_direct_names: dict[str, str] = {}
    shutil_aliases: set[str] = set()
    shutil_direct_names: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name == "os":
                    os_aliases.add(bound)
                elif alias.name == "shutil":
                    shutil_aliases.add(bound)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                bound = alias.asname or alias.name
                if module == "os" and alias.name in OS_FS_FUNCS:
                    os_direct_names[bound] = alias.name
                elif module == "shutil" and alias.name in SHUTIL_FS_FUNCS:
                    shutil_direct_names[bound] = alias.name

    return os_aliases, os_direct_names, shutil_aliases, shutil_direct_names


def _scan_import(node: ast.Import | ast.ImportFrom) -> list[tuple[int, str, str]]:
    findings: list[tuple[int, str, str]] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            module = alias.name
            if _is_under_namespace(module) and module not in ALLOW_SET:
                findings.append((node.lineno, "import", module))
        return findings

    module = node.module or ""
    if not _is_under_namespace(module):
        return findings
    if module in ALLOW_SET:
        return findings
    if len(node.names) == 1 and f"{module}.{node.names[0].name}" in ALLOW_SET:
        return findings
    return [(node.lineno, "import", module)]


def _scan_call(
    node: ast.Call,
    os_aliases: set[str],
    os_direct_names: dict[str, str],
    shutil_aliases: set[str],
    shutil_direct_names: dict[str, str],
) -> tuple[int, str, str] | None:
    func = node.func

    if isinstance(func, ast.Name):
        if func.id == "open":
            return node.lineno, "fs", "open"
        if func.id in os_direct_names:
            return node.lineno, "fs", f"os.{os_direct_names[func.id]}"
        if func.id in shutil_direct_names:
            return node.lineno, "fs", f"shutil.{shutil_direct_names[func.id]}"
        return None

    if not isinstance(func, ast.Attribute):
        return None

    if func.attr in IO_METHODS:
        return node.lineno, "fs", f"Path.{func.attr}"

    if (
        isinstance(func.value, ast.Name)
        and func.value.id in os_aliases
        and func.attr in OS_FS_FUNCS
    ):
        return node.lineno, "fs", f"os.{func.attr}"

    if (
        isinstance(func.value, ast.Name)
        and func.value.id in shutil_aliases
        and func.attr in SHUTIL_FS_FUNCS
    ):
        return node.lineno, "fs", f"shutil.{func.attr}"

    return None


def scan_source(source: str, filename: str = "<source>") -> list[tuple[int, str, str]]:
    tree = ast.parse(source, filename=filename)
    os_aliases, os_direct_names, shutil_aliases, shutil_direct_names = (
        _collect_bindings(tree)
    )

    findings: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            findings.extend(_scan_import(node))
        elif isinstance(node, ast.Call):
            finding = _scan_call(
                node,
                os_aliases,
                os_direct_names,
                shutil_aliases,
                shutil_direct_names,
            )
            if finding:
                findings.append(finding)

    findings.sort()
    return findings


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    return scan_source(path.read_text(encoding="utf-8"), filename=str(path))


def discover_modules(root: Path) -> list[Path]:
    return [Path(target) for target in TARGETS if (root / target).is_file()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="built-in sol call tools HTTP-only lint"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repository root to scan (defaults to the checkout root).",
    )
    args = parser.parse_args(argv)

    findings: list[str] = []
    for rel in discover_modules(args.root):
        for lineno, kind, detail in scan_file(args.root / rel):
            findings.append(f"{rel.as_posix()}:{lineno}: {kind} {detail}")

    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        return 1

    print("tools-http-only: pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
