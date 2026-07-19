#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Provider runtime start-command architecture gate.

Provider runtime process lifecycle is owned by the supervisor reconciler. Production
code must not reintroduce Callosum lifecycle commands that ask the supervisor to
start bundled local or Parakeet directly.

The production allowlist is intentionally empty. A new production violation should
fail immediately.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OWNER_FILES: frozenset[str] = frozenset()
ALLOWLIST: dict[tuple[str, str], int] = {}
START_EVENTS: frozenset[str] = frozenset({"start_local", "start_parakeet"})
FORBIDDEN_FUNCTION_NAMES: frozenset[str] = frozenset(
    {
        "_handle_supervisor_start_local",
        "_handle_supervisor_start_parakeet",
        "_request_local_server_start",
        "_request_parakeet_server_start",
    }
)


def _is_owner(rel: Path) -> bool:
    return rel.as_posix() in OWNER_FILES


def _is_test_file(rel: Path) -> bool:
    return (
        "tests" in rel.parts
        or rel.name == "conftest.py"
        or (rel.name.startswith("test_") and rel.suffix == ".py")
    )


def discover_modules(root: Path) -> list[Path]:
    scope = root / "solstone"
    if not scope.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(scope.rglob("*.py")):
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts:
            continue
        if _is_test_file(rel) or _is_owner(rel):
            continue
        found.append(rel)
    return found


class _Bindings:
    def __init__(self) -> None:
        self.callosum_send_names: set[str] = {"callosum_send"}
        self.callosum_modules: set[str] = set()


def _collect_bindings(tree: ast.AST) -> _Bindings:
    bindings = _Bindings()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "solstone.think.callosum":
                    bindings.callosum_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module != "solstone.think.callosum":
                continue
            for alias in node.names:
                if alias.name == "callosum_send":
                    bindings.callosum_send_names.add(alias.asname or alias.name)
    return bindings


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        if base:
            return f"{base}.{node.attr}"
    return None


def _constant_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_arg_or_kw(call: ast.Call, index: int, name: str) -> str | None:
    if len(call.args) > index:
        value = _constant_str(call.args[index])
        if value is not None:
            return value
    for keyword in call.keywords:
        if keyword.arg == name:
            return _constant_str(keyword.value)
    return None


def _is_callosum_send_call(call: ast.Call, bindings: _Bindings) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in bindings.callosum_send_names
    if not isinstance(func, ast.Attribute) or func.attr != "callosum_send":
        return False
    if isinstance(func.value, ast.Name) and func.value.id in bindings.callosum_modules:
        return True
    return _dotted_name(func) == "solstone.think.callosum.callosum_send"


def _is_emit_call(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr == "emit"


def _forbidden_start_call(call: ast.Call, bindings: _Bindings) -> bool:
    if not (_is_callosum_send_call(call, bindings) or _is_emit_call(call)):
        return False
    return (
        _call_arg_or_kw(call, 0, "tract") == "supervisor"
        and _call_arg_or_kw(call, 1, "event") in START_EVENTS
    )


def _forbidden_message_dict(node: ast.Dict) -> str | None:
    values: dict[str, str] = {}
    for key, value in zip(node.keys, node.values):
        key_str = _constant_str(key)
        value_str = _constant_str(value)
        if key_str is not None and value_str is not None:
            values[key_str] = value_str
    if values.get("tract") == "supervisor" and values.get("event") in START_EVENTS:
        return values["event"]
    return None


def scan_source(source: str, filename: str = "<source>") -> list[tuple[int, str, str]]:
    tree = ast.parse(source, filename=filename)
    bindings = _collect_bindings(tree)
    findings: list[tuple[int, str, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in FORBIDDEN_FUNCTION_NAMES:
                findings.append(
                    (
                        node.lineno,
                        "provider_start_handler",
                        node.name,
                    )
                )
        elif isinstance(node, ast.Call) and _forbidden_start_call(node, bindings):
            findings.append(
                (
                    node.lineno,
                    "provider_start_command",
                    "supervisor provider start Callosum command",
                )
            )
        elif isinstance(node, ast.Dict):
            event = _forbidden_message_dict(node)
            if event is not None:
                findings.append(
                    (
                        node.lineno,
                        "provider_start_message",
                        f"supervisor {event} message literal",
                    )
                )

    findings.sort()
    return findings


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    return scan_source(path.read_text(encoding="utf-8"), filename=str(path))


def count_violations(root: Path) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for rel in discover_modules(root):
        for _lineno, kind, _detail in scan_file(root / rel):
            key = (rel.as_posix(), kind)
            counts[key] = counts.get(key, 0) + 1
    return counts


def evaluate(
    root: Path,
    allowlist: dict[tuple[str, str], int],
) -> tuple[list[str], list[str], list[str]]:
    over: list[str] = []
    stale: list[str] = []
    tracked: list[str] = []
    counts = count_violations(root)
    for key in sorted(set(counts) | set(allowlist)):
        count = counts.get(key, 0)
        allowed = allowlist.get(key, 0)
        rel, kind = key
        if count > allowed:
            over.append(f"{rel}: {kind} count {count} exceeds allowed {allowed}")
        elif count < allowed:
            stale.append(f"{rel}: {kind} count {count} below allowed {allowed}")
        elif allowed:
            tracked.append(f"{rel}: {count}/{allowed} {kind} (allowlisted)")
    return over, stale, tracked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provider start-command lint")
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repository root to scan (defaults to the checkout root).",
    )
    args = parser.parse_args(argv)
    over, stale, tracked = evaluate(args.root, ALLOWLIST)
    if tracked:
        print("provider-start-commands: known violations (allowlisted):")
        for line in tracked:
            print(f"  {line}")
        print()
    if over or stale:
        print("provider-start-commands: violations:", file=sys.stderr)
        for line in over:
            print(f"  {line}", file=sys.stderr)
        for line in stale:
            print(f"  stale allowlist: {line}", file=sys.stderr)
        print(
            "Provider runtime launches must be driven by durable reconciliation, "
            "not supervisor start Callosum commands.",
            file=sys.stderr,
        )
        return 1
    print("provider-start-commands: pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
