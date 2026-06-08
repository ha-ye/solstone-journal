# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Static guard for direct durable writes to config/schedules.json.

Known limitation: this detects direct durable-write sinks to a schedules path, not
writes routed through a generic helper that receives the path as an opaque
parameter. That indirect case is closed by the journal_io access gate and the
raw-mechanic gate, which together require any new writer to surface.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRITE_PRIMITIVES = {
    "atomic_replace",
    "install_file",
    "write_json",
    "write_jsonl",
    "write_text",
}
WRITE_MODE_CHARS = frozenset({"w", "a", "x", "+"})


def _production_modules() -> list[Path]:
    return sorted(
        path
        for path in (ROOT / "solstone").rglob("*.py")
        if path.is_file()
        and "__pycache__" not in path.parts
        and "tests" not in path.parts
        and path.relative_to(ROOT).as_posix() != "solstone/think/schedule_config.py"
    )


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_schedules_path_expr(node: ast.AST, schedules_vars: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in schedules_vars
    if isinstance(node, ast.Call):
        return _call_name(node.func) == "get_schedules_path"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return (
            isinstance(node.right, ast.Constant)
            and node.right.value == "schedules.json"
        )
    return False


def _is_write_mode(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return any(char in node.value for char in WRITE_MODE_CHARS)
    return False


def _open_mode(call: ast.Call) -> ast.AST | None:
    if len(call.args) >= 2:
        return call.args[1]
    for keyword in call.keywords:
        if keyword.arg == "mode":
            return keyword.value
    return None


class SchedulesWriteVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.schedules_vars: set[str] = set()
        self.violations: list[str] = []

    def visit_Assign(self, node: ast.Assign) -> None:
        if _is_schedules_path_expr(node.value, self.schedules_vars):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.schedules_vars.add(target.id)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if (
            node.value is not None
            and isinstance(node.target, ast.Name)
            and _is_schedules_path_expr(node.value, self.schedules_vars)
        ):
            self.schedules_vars.add(node.target.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._check_open(node)
        self._check_path_open(node)
        self._check_write_primitive(node)
        self._check_replace(node)
        self.generic_visit(node)

    def _record(self, node: ast.AST, kind: str) -> None:
        rel = self.path.relative_to(ROOT)
        self.violations.append(f"{rel}:{node.lineno}: {kind}")

    def _check_open(self, node: ast.Call) -> None:
        if _call_name(node.func) != "open" or not node.args:
            return
        if _is_schedules_path_expr(
            node.args[0], self.schedules_vars
        ) and _is_write_mode(_open_mode(node)):
            self._record(node, "open-write")

    def _check_path_open(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "open":
            return
        if _is_schedules_path_expr(
            node.func.value, self.schedules_vars
        ) and _is_write_mode(_open_mode(node)):
            self._record(node, "Path.open-write")

    def _check_write_primitive(self, node: ast.Call) -> None:
        if _call_name(node.func) not in WRITE_PRIMITIVES or not node.args:
            return
        if _is_schedules_path_expr(node.args[0], self.schedules_vars):
            self._record(node, _call_name(node.func) or "write-primitive")

    def _check_replace(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "replace":
            if len(node.args) >= 2 and _is_schedules_path_expr(
                node.args[1], self.schedules_vars
            ):
                self._record(node, "os.replace")
                return
            if node.args and _is_schedules_path_expr(node.args[0], self.schedules_vars):
                self._record(node, "Path.replace")
            return
        if _call_name(node.func) == "replace" and len(node.args) >= 2:
            if _is_schedules_path_expr(node.args[1], self.schedules_vars):
                self._record(node, "os.replace")


def test_only_schedule_config_directly_writes_schedules_json() -> None:
    violations: list[str] = []
    for path in _production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = SchedulesWriteVisitor(path)
        visitor.visit(tree)
        violations.extend(visitor.violations)

    assert violations == []
