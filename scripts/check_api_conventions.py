#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""HTTP API conventions lint.

Static check for the response/error conventions documented in
``docs/CONVEY.md`` § "HTTP API conventions". It scans every convey route
module — discovered by *which files register a Flask ``Blueprint``* under
``solstone/apps/`` and ``solstone/convey/`` (so a new app or core blueprint is
picked up automatically) — and, inside each JSON-returning handler, flags the
escape hatches the conventions forbid:

  - ``abort``        — an ``abort(...)`` call.
  - ``bare-return``  — a bare ``return "", <4xx>`` tuple.
  - ``inline-error`` — an in-band error body built inline
    (``jsonify({"error": ...})`` / ``return {"error": ...}``) outside the
    sanctioned ``error_response`` / ``error_response_with_reason`` helpers.
  - ``bare-array``   — returning a bare top-level list
    (``jsonify([...])`` / ``jsonify(<list-var>)`` / ``return [...]``).

A handler is **JSON-governed** when any of its return paths produces a JSON
body: it calls ``jsonify(...)``, returns one of the response helpers
(``error_response`` / ``error_response_with_reason`` / ``success_response`` /
``respond_collection`` / ``created``), or returns a dict/list literal. A handler
whose only returns are ``render_template`` / ``redirect`` / ``send_file`` / a
plain string (a page-or-file route) is exempt from the ``abort`` and
``bare-return`` rules; a bare top-level array is always flagged. Classification
is by return style, never by URL — the ``/api/`` path segment is an unreliable
signal (app routes carry it in the decorator, ``chat_bp`` carries it in the
blueprint ``url_prefix``, and genuinely-JSON endpoints carry it nowhere).

The check ships green via a committed ``ALLOWLIST`` of the violations that exist
on the current tree, keyed by ``(file, kind)`` with an allowed **count**. A new
violation that pushes any ``(file, kind)`` count above its allowed number fails
the check; fixing occurrences lets the allowed count be lowered, so the
allowlist ratchets toward empty. It is never keyed by line number (brittle to
edits above the occurrence) and never a blanket per-file disable (which would
hide future new violations in that file).

Exit codes:
  0 — no un-allowlisted violations
  1 — a (file, kind) count exceeds its allowlisted number
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Trees scanned for Blueprint-registering route modules.
SCAN_SCOPES: tuple[str, ...] = (
    "solstone/apps",
    "solstone/convey",
)

# Response helpers that legitimately produce a JSON body. A call to one of
# these is JSON-producing for classification and is NOT an inline error.
RESPONSE_HELPERS: frozenset[str] = frozenset(
    {
        "error_response",
        "error_response_with_reason",
        "success_response",
        "respond_collection",
        "created",
    }
)

# Decorator attributes that mark a function as a Flask route handler.
ROUTE_DECORATORS: frozenset[str] = frozenset(
    {"route", "get", "post", "put", "patch", "delete"}
)

# Committed allowlist of violations on the current tree, keyed by
# (posix-relative-path, kind) -> allowed count. Ratchets toward empty: lower a
# count as occurrences are fixed; never raise one to admit a new violation.
ALLOWLIST: dict[tuple[str, str], int] = {
    ("solstone/apps/link/routes.py", "abort"): 1,
}


def _func_name(func: ast.expr) -> str | None:
    """Return the called name for ``Name``/``Attribute`` call targets."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _iter_local_nodes(node: ast.AST):
    """Yield descendants of ``node``, not descending into nested functions.

    Stops at nested ``FunctionDef`` / ``AsyncFunctionDef`` / ``Lambda`` so that
    a handler's classification and violations are not polluted by inner helpers
    or generators (e.g. an SSE ``generate()`` closure).
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _iter_local_nodes(child)


def _is_route_handler(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in func.decorator_list:
        if isinstance(dec, ast.Call) and _func_name(dec.func) in ROUTE_DECORATORS:
            return True
    return False


def _is_jsonify_call(expr: ast.expr | None) -> bool:
    return isinstance(expr, ast.Call) and _func_name(expr.func) == "jsonify"


def _collect_list_names(nodes: list[ast.AST]) -> set[str]:
    """Names bound to an obvious list value within the handler body."""
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Assign):
            value = node.value
            is_list = isinstance(value, (ast.List, ast.ListComp)) or (
                isinstance(value, ast.Call) and _func_name(value.func) == "list"
            )
            if is_list:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            ann = node.annotation
            ann_name = None
            if isinstance(ann, ast.Name):
                ann_name = ann.id
            elif isinstance(ann, ast.Subscript) and isinstance(ann.value, ast.Name):
                ann_name = ann.value.id
            if ann_name in ("list", "List"):
                names.add(node.target.id)
            if isinstance(node.value, (ast.List, ast.ListComp)):
                names.add(node.target.id)
    return names


def _is_list_payload(expr: ast.expr | None, list_names: set[str]) -> bool:
    if isinstance(expr, (ast.List, ast.ListComp)):
        return True
    return isinstance(expr, ast.Name) and expr.id in list_names


def _payload_expr(ret: ast.Return) -> ast.expr | None:
    """The value carried by a return, unwrapping ``(body, status)`` tuples."""
    value = ret.value
    if isinstance(value, ast.Tuple) and value.elts:
        return value.elts[0]
    return value


def _produces_json(expr: ast.expr | None, list_names: set[str]) -> bool:
    if expr is None:
        return False
    if _is_jsonify_call(expr):
        return True
    if isinstance(expr, ast.Call) and _func_name(expr.func) in RESPONSE_HELPERS:
        return True
    if isinstance(expr, (ast.Dict, ast.List, ast.ListComp, ast.DictComp)):
        return True
    return _is_list_payload(expr, list_names)


def _dict_has_error_key(expr: ast.expr | None) -> bool:
    return isinstance(expr, ast.Dict) and any(
        isinstance(key, ast.Constant) and key.value == "error" for key in expr.keys
    )


def _is_inline_error(payload: ast.expr | None) -> bool:
    if _is_jsonify_call(payload):
        assert isinstance(payload, ast.Call)
        return len(payload.args) == 1 and _dict_has_error_key(payload.args[0])
    return _dict_has_error_key(payload)


def _is_bare_array(payload: ast.expr | None, list_names: set[str]) -> bool:
    if _is_jsonify_call(payload):
        assert isinstance(payload, ast.Call)
        return len(payload.args) == 1 and _is_list_payload(payload.args[0], list_names)
    return _is_list_payload(payload, list_names)


def _is_bare_status_return(ret: ast.Return) -> bool:
    value = ret.value
    if not (isinstance(value, ast.Tuple) and len(value.elts) == 2):
        return False
    body, status = value.elts
    return (
        isinstance(body, ast.Constant)
        and isinstance(body.value, str)
        and isinstance(status, ast.Constant)
        and isinstance(status.value, int)
        and not isinstance(status.value, bool)
        and 400 <= status.value <= 499
    )


def classify_handler(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[int, str]]:
    """Return ``(lineno, kind)`` violations for a single route handler."""
    nodes = list(_iter_local_nodes(func))
    list_names = _collect_list_names(nodes)

    returns = [n for n in nodes if isinstance(n, ast.Return)]
    json_governed = any(_produces_json(_payload_expr(r), list_names) for r in returns)

    findings: list[tuple[int, str]] = []

    # abort(...) anywhere in the handler's own scope.
    if json_governed:
        for node in nodes:
            if isinstance(node, ast.Call) and _func_name(node.func) == "abort":
                findings.append((node.lineno, "abort"))

    for ret in returns:
        payload = _payload_expr(ret)
        if json_governed and _is_bare_status_return(ret):
            findings.append((ret.lineno, "bare-return"))
        if _is_inline_error(payload):
            findings.append((ret.lineno, "inline-error"))
        if _is_bare_array(payload, list_names):
            findings.append((ret.lineno, "bare-array"))

    return findings


def module_registers_blueprint(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _func_name(node.func) == "Blueprint":
            return True
    return False


def discover_modules(root: Path) -> list[Path]:
    """Posix-relative paths of Blueprint-registering modules under the scopes."""
    found: list[Path] = []
    for scope in SCAN_SCOPES:
        scope_dir = root / scope
        if not scope_dir.is_dir():
            continue
        for path in sorted(scope_dir.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            if module_registers_blueprint(tree):
                found.append(path.relative_to(root))
    return found


def scan_source(source: str, filename: str = "<source>") -> list[tuple[int, str, str]]:
    """Return ``(lineno, kind, function_name)`` violations for a module source."""
    tree = ast.parse(source, filename=filename)
    findings: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_route_handler(node):
            continue
        for lineno, kind in classify_handler(node):
            findings.append((lineno, kind, node.name))
    findings.sort()
    return findings


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    return scan_source(path.read_text(encoding="utf-8"), filename=str(path))


def count_violations(root: Path) -> dict[tuple[str, str], int]:
    """Map ``(posix-relpath, kind)`` -> occurrence count across the tree."""
    counts: dict[tuple[str, str], int] = {}
    for rel in discover_modules(root):
        for _lineno, kind, _func in scan_file(root / rel):
            counts[(rel.as_posix(), kind)] = counts.get((rel.as_posix(), kind), 0) + 1
    return counts


def evaluate(
    root: Path,
    allowlist: dict[tuple[str, str], int],
) -> tuple[list[str], list[str]]:
    """Return ``(new_violations, tracked)`` human-readable lines."""
    new: list[str] = []
    tracked: list[str] = []
    for rel in discover_modules(root):
        rel_str = rel.as_posix()
        findings = scan_file(root / rel)
        by_kind: dict[str, list[int]] = {}
        for lineno, kind, _func in findings:
            by_kind.setdefault(kind, []).append(lineno)
        for kind, linenos in sorted(by_kind.items()):
            count = len(linenos)
            allowed = allowlist.get((rel_str, kind), 0)
            if count > allowed:
                lines = ", ".join(str(n) for n in sorted(linenos))
                new.append(
                    f"{rel_str}: {count} {kind} (allowed {allowed}) at line(s) {lines}"
                )
            elif allowed:
                tracked.append(f"{rel_str}: {count}/{allowed} {kind} (allowlisted)")
    return new, tracked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HTTP API conventions lint")
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repository root to scan (defaults to the checkout root).",
    )
    args = parser.parse_args(argv)

    new, tracked = evaluate(args.root, ALLOWLIST)

    if tracked:
        print("api-conventions: known violations (allowlisted, ratcheting down):")
        for line in tracked:
            print(f"  {line}")
        print()

    if new:
        print("api-conventions: NEW violations:", file=sys.stderr)
        for line in new:
            print(f"  {line}", file=sys.stderr)
        print(file=sys.stderr)
        print(
            "See docs/CONVEY.md § HTTP API conventions. Route a collection "
            "through respond_collection(), a create through created(), and "
            "every error through error_response().",
            file=sys.stderr,
        )
        return 1

    print("api-conventions: pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
