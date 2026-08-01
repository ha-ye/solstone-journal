#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Bind the SPL link-health vocabulary across its two implementations.

The reason codes and the callosum event name are a hard contract: the native SPL
service emits them, and the Python web layer consumes them
(`apps/network/routes.py` classifies offline reasons, `convey/bridge.py` names the
health event). After the native cutover the two live in different languages, and
nothing else makes them agree.

This gate reads BOTH sides from source and fails when they differ. It is
deliberately not a test that asserts one side against literals written beside it —
such a test passes forever regardless of what the other side says, which reads like
a drift gate without being one.

Exit 0 when the vocabularies match, 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_PY = "solstone/think/link/link_health.py"
DEFAULT_RS = "core/crates/solstone-core-spl/src/health.rs"

# `pub const REASON_FOO: &str = "foo";`
_RS_CONST = re.compile(
    r'pub\s+const\s+(?P<name>[A-Z0-9_]+)\s*:\s*&\'?\w*\s*str\s*=\s*"(?P<value>[^"]*)"'
)


class Mismatch(RuntimeError):
    pass


def _python_vocabulary(path: Path) -> dict[str, str]:
    """Read module-level `NAME = "value"` string constants without importing."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.isupper():
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            out[target.id] = node.value.value
    return out


def _python_offline_set(path: Path) -> set[str] | None:
    """Resolve OFFLINE_TUNNEL_REASONS to its literal values, via the names it cites."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    consts = _python_vocabulary(path)
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id != "OFFLINE_TUNNEL_REASONS":
            continue
        names: set[str] = set()
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Name) and sub.id in consts:
                names.add(consts[sub.id])
            elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                names.add(sub.value)
        return names
    return None


def _rust_vocabulary(path: Path) -> dict[str, str]:
    return {
        m.group("name"): m.group("value")
        for m in _RS_CONST.finditer(path.read_text(encoding="utf-8"))
    }


def _rust_offline_set(path: Path, rust: dict[str, str]) -> set[str] | None:
    """Resolve the Rust offline-reason list to literal values."""
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r"OFFLINE_TUNNEL_REASONS[^=]*=\s*(?:&)?\[(?P<body>[^\]]*)\]", text, re.S
    )
    if not match:
        return None
    body = match.group("body")
    found: set[str] = set()
    for literal in re.findall(r'"([^"]*)"', body):
        found.add(literal)
    for name in re.findall(r"\b([A-Z0-9_]+)\b", body):
        if name in rust:
            found.add(rust[name])
    return found


def check(root: Path, py_rel: str, rs_rel: str) -> list[str]:
    py_path, rs_path = root / py_rel, root / rs_rel
    problems: list[str] = []
    for path in (py_path, rs_path):
        if not path.is_file():
            problems.append(f"vocabulary source is missing: {path.relative_to(root)}")
    if problems:
        return problems

    py = _python_vocabulary(py_path)
    rs = _rust_vocabulary(rs_path)

    py_reasons = {v for k, v in py.items() if k.startswith("REASON_")}
    rs_reasons = {v for k, v in rs.items() if k.startswith("REASON_")}

    if not py_reasons:
        problems.append(
            f"{py_rel}: no REASON_* constants found — the gate is not reading it"
        )
    if not rs_reasons:
        problems.append(
            f"{rs_rel}: no REASON_* constants found — the gate is not reading it"
        )
    if problems:
        return problems

    for value in sorted(py_reasons - rs_reasons):
        problems.append(f"reason present in Python but not Rust: {value!r}")
    for value in sorted(rs_reasons - py_reasons):
        problems.append(f"reason present in Rust but not Python: {value!r}")

    # The callosum event name the web layer keys on.
    py_event = py.get("LINK_HEALTH_EVENT")
    rs_event = next((v for k, v in rs.items() if k == "LINK_HEALTH_EVENT"), None)
    if py_event != rs_event:
        problems.append(
            f"LINK_HEALTH_EVENT differs: Python {py_event!r} vs Rust {rs_event!r}"
        )

    # The offline subset drives owner-visible status copy, so drift here is not cosmetic.
    py_offline = _python_offline_set(py_path)
    rs_offline = _rust_offline_set(rs_path, rs)
    if py_offline is None:
        problems.append(f"{py_rel}: OFFLINE_TUNNEL_REASONS not found")
    elif rs_offline is None:
        problems.append(f"{rs_rel}: no offline-reason list found")
    elif py_offline != rs_offline:
        problems.append(
            "OFFLINE_TUNNEL_REASONS differs: "
            f"Python {sorted(py_offline)} vs Rust {sorted(rs_offline)}"
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python-source", default=DEFAULT_PY)
    parser.add_argument("--rust-source", default=DEFAULT_RS)
    args = parser.parse_args(argv)

    problems = check(args.root.resolve(), args.python_source, args.rust_source)
    if problems:
        print("spl health vocabulary: FAIL", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    print("spl health vocabulary: pass (reasons, event name, and offline subset agree)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
