# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_DIR = (
    Path(__file__).resolve().parents[2]
    / "solstone"
    / "think"
    / "services"
    / "spp_attest"
)
BANNED_IMPORT_ROOTS = {"subprocess", "socket", "urllib", "requests", "shutil"}
BANNED_WRITE_ATTRS = {
    "write_text",
    "write_bytes",
    "mkdir",
    "unlink",
    "rename",
    "replace",
    "rmtree",
    "atomic_write",
    "atomic_replace",
}
BANNED_WRITE_NAMES = {"atomic_write", "atomic_replace"}
WRITE_MODE_CHARS = frozenset({"w", "a", "x", "+"})


def test_spp_attest_package_stays_pure_python_read_only() -> None:
    files = sorted(PACKAGE_DIR.rglob("*.py"))
    assert files, f"no Python files found under {PACKAGE_DIR}"

    findings: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            findings.extend(_scan_node(path, node))

    assert findings == []


def test_purity_scanner_bans_aliased_shutil_import() -> None:
    tree = ast.parse("import shutil as sh\nsh.which('tpm2_checkquote')\n")
    findings = [
        finding
        for node in ast.walk(tree)
        for finding in _scan_node(Path("snippet.py"), node)
    ]

    assert findings == ["snippet.py:1: banned import shutil"]


def _scan_node(path: Path, node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return _scan_import(path, node)
    if isinstance(node, ast.ImportFrom):
        return _scan_import_from(path, node)
    if isinstance(node, ast.Call):
        return _scan_call(path, node)
    return []


def _scan_import(path: Path, node: ast.Import) -> list[str]:
    findings: list[str] = []
    for alias in node.names:
        root = alias.name.split(".", maxsplit=1)[0]
        if root in BANNED_IMPORT_ROOTS:
            findings.append(f"{path}:{node.lineno}: banned import {alias.name}")
    return findings


def _scan_import_from(path: Path, node: ast.ImportFrom) -> list[str]:
    findings: list[str] = []
    module = node.module or ""
    root = module.split(".", maxsplit=1)[0]
    if root in BANNED_IMPORT_ROOTS:
        findings.append(f"{path}:{node.lineno}: banned import from {module}")
    for alias in node.names:
        if alias.name in BANNED_WRITE_NAMES:
            findings.append(f"{path}:{node.lineno}: banned write helper {alias.name}")
    return findings


def _scan_call(path: Path, node: ast.Call) -> list[str]:
    func = node.func
    if isinstance(func, ast.Attribute):
        if _is_shutil_which(func):
            return [f"{path}:{node.lineno}: banned shutil.which call"]
        if _is_json_dump(func):
            return [f"{path}:{node.lineno}: banned json.dump call"]
        if func.attr in BANNED_WRITE_ATTRS:
            return [f"{path}:{node.lineno}: banned write API {func.attr}"]
    if isinstance(func, ast.Name):
        if func.id in BANNED_WRITE_NAMES:
            return [f"{path}:{node.lineno}: banned write helper {func.id}"]
        if func.id == "open" and _open_uses_write_mode(node):
            return [f"{path}:{node.lineno}: banned write-mode open call"]
    return []


def _is_shutil_which(func: ast.Attribute) -> bool:
    return (
        func.attr == "which"
        and isinstance(func.value, ast.Name)
        and func.value.id == "shutil"
    )


def _is_json_dump(func: ast.Attribute) -> bool:
    return (
        func.attr == "dump"
        and isinstance(func.value, ast.Name)
        and func.value.id == "json"
    )


def _open_uses_write_mode(node: ast.Call) -> bool:
    mode_node = None
    if len(node.args) >= 2:
        mode_node = node.args[1]
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode_node = keyword.value
            break
    if mode_node is None:
        return False
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        return any(char in mode_node.value for char in WRITE_MODE_CHARS)
    return True
