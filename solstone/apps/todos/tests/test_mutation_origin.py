# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Static guard: todos mutations only originate inside ``locked_modify``.

Scans the todos ``call.py`` and ``routes.py`` surfaces and asserts that every
call to a mutating ``TodoChecklist`` method is lexically nested inside a
function passed as the ``modify_fn`` of a ``locked_modify(...)`` call. This is
the invariant that keeps every read-modify-write under the exclusive lock.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MUTATING_METHODS = frozenset(
    {
        "append_entry",
        "mark_done",
        "mark_undone",
        "cancel_entry",
        "update_entry_text",
        "save",
    }
)

APP_DIR = Path(__file__).resolve().parent.parent  # solstone/apps/todos
TARGET_FILES = [APP_DIR / "call.py", APP_DIR / "routes.py"]


def _locked_modify_fn_names(tree: ast.AST) -> set[str]:
    """Return names of functions passed as ``modify_fn`` to locked_modify."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "locked_modify"
        ):
            arg: ast.expr | None = node.args[2] if len(node.args) >= 3 else None
            for keyword in node.keywords:
                if keyword.arg == "modify_fn":
                    arg = keyword.value
            if isinstance(arg, ast.Name):
                names.add(arg.id)
    return names


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _enclosing_func(
    parents: dict[ast.AST, ast.AST], node: ast.AST
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


def _violations(path: Path) -> list[tuple[str, str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents = _parent_map(tree)
    locked = _locked_modify_fn_names(tree)

    bad: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in MUTATING_METHODS
        ):
            enclosing = _enclosing_func(parents, node)
            if enclosing is None or enclosing.name not in locked:
                bad.append((path.name, node.func.attr, node.lineno))
    return bad


@pytest.mark.parametrize("path", TARGET_FILES, ids=lambda p: p.name)
def test_no_unlocked_todo_mutation(path: Path) -> None:
    assert _violations(path) == []


def test_guard_flags_unlocked_mutation(tmp_path: Path) -> None:
    source = "def handler(cl):\n    cl.append_entry('x')\n"
    fake = tmp_path / "fake_surface.py"
    fake.write_text(source, encoding="utf-8")
    assert _violations(fake) != []


def test_guard_allows_locked_mutation(tmp_path: Path) -> None:
    source = (
        "def cmd():\n"
        "    def _add(cl):\n"
        "        return cl.append_entry('x')\n"
        "    Obj.locked_modify(d, f, _add)\n"
    )
    ok = tmp_path / "ok_surface.py"
    ok.write_text(source, encoding="utf-8")
    assert _violations(ok) == []
