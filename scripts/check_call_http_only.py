#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""``sol call`` HTTP-only lint.

Invariant: a direct-child ``solstone/apps/*/call.py`` must reach the journal
only over the Convey HTTP client. It must not (a) import a journal/domain/server
module (``solstone.think.*`` / ``solstone.apps.*`` / ``solstone.convey.*``)
outside the enumerated allow-set, nor (b) perform a direct filesystem I/O
mechanic.

The enumerated allow-set is two path strings, compared as strings:
``solstone.think.convey_client`` and ``solstone.convey.reasons``. The gate never
imports these modules. This is not a purity predicate; a future pure shared
module is a one-line addition to the allow-set.

All imports count, including function-local/lazy ones. That keeps the eventual
capstone zero-violation invariant evasion-proof: a cutover cannot hide a journal
reach inside a function body.

The committed ``ALLOWLIST`` is keyed by ``(file, kind)`` with an allowed count.
This is a ``!=`` self-check: a live count above the allowlisted count fails as a
new/over violation, and a live count below the allowlisted count (including a
deleted/renamed/now-clean file with live count 0) fails as a stale entry. The
gate iterates the union of discovered keys and allowlist keys so stale entries
for vanished files are still visited.

Explicitly not flagged: ``subprocess.*`` (residual subprocess filesystem access
is a cutover-review judgment, not this gate's job), ``os.environ`` /
``os.getenv`` (neither a journal import nor a filesystem mechanic), pure path
algebra (``/``, ``.parent``, ``.name``, ``.suffix``, ``.stem``,
``.with_suffix`` / ``.with_name`` / ``.with_stem``), and ``os.path.*``
path-algebra/metadata helpers.

Known limitation: this gate keys off absolute ``solstone.*`` module strings.
Relative imports such as ``from .sibling import x`` are not resolved. The tree
uses absolute imports by convention, so this is not a current gap.

Exit codes:
  0 - clean
  1 - any over violation or stale allowlist entry
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ALLOW_SET: frozenset[str] = frozenset(
    {"solstone.think.convey_client", "solstone.convey.reasons"}
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

# Committed allowlist of current direct app-call violations, keyed by
# (posix-relative-path, kind) -> allowed count. Ratchets toward empty: lower a
# count as occurrences are converted; stale entries fail until lowered/removed.
ALLOWLIST: dict[tuple[str, str], int] = {
    ("solstone/apps/import/call.py", "fs"): 17,
    ("solstone/apps/import/call.py", "import"): 8,
    # Settings keeps only convey network-access enable/disable in-process:
    # these are convey server-lifecycle operations that restart Convey to
    # change its bind host, not journal-data access. Every other settings
    # verb must use the Convey HTTP client.
    ("solstone/apps/settings/call.py", "import"): 4,
    ("solstone/apps/support/call.py", "import"): 14,
    ("solstone/apps/timeline/call.py", "fs"): 4,
    ("solstone/apps/timeline/call.py", "import"): 3,
}


def _is_under_namespace(module: str) -> bool:
    return any(
        module == namespace or module.startswith(f"{namespace}.")
        for namespace in FLAGGED_NAMESPACES
    )


def discover_modules(root: Path) -> list[Path]:
    """Return posix-relative direct-child ``solstone/apps/*/call.py`` files."""
    apps_dir = root / "solstone" / "apps"
    if not apps_dir.is_dir():
        return []

    found: list[Path] = []
    for path in sorted(apps_dir.glob("*/call.py")):
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts:
            continue
        found.append(rel)
    return found


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
    """Return sorted ``(lineno, kind, detail)`` violations for source."""
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


def count_violations(root: Path) -> dict[tuple[str, str], int]:
    """Map ``(posix-relpath, kind)`` -> occurrence count across app calls."""
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
    """Return ``(over, stale, tracked)`` human-readable lines."""
    live: dict[tuple[str, str], list[int]] = {}
    for rel in discover_modules(root):
        rel_str = rel.as_posix()
        for lineno, kind, _detail in scan_file(root / rel):
            live.setdefault((rel_str, kind), []).append(lineno)

    over: list[str] = []
    stale: list[str] = []
    tracked: list[str] = []

    for rel_str, kind in sorted(set(live) | set(allowlist)):
        key = (rel_str, kind)
        linenos = sorted(live.get(key, []))
        count = len(linenos)
        allowed = allowlist.get(key, 0)
        if count > allowed:
            lines = ", ".join(str(n) for n in linenos)
            over.append(
                f"{rel_str}: {kind} count {count} exceeds allowed {allowed} "
                f"at line(s) {lines} - this call.py must reach the journal only "
                "via the convey HTTP client (solstone.think.convey_client); see "
                "the check_call_http_only gate."
            )
        elif count < allowed:
            delete_hint = " (delete it)" if count == 0 else ""
            stale.append(
                f"{rel_str}: {kind} allowlisted at {allowed} but {count} live - "
                f"lower the entry to {count}{delete_hint} - check_call_http_only "
                "ratchets toward empty; a converted call.py removes its entry."
            )
        elif allowed:
            tracked.append(f"{rel_str}: {count}/{allowed} {kind} (allowlisted)")

    return over, stale, tracked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sol call HTTP-only lint")
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repository root to scan (defaults to the checkout root).",
    )
    args = parser.parse_args(argv)

    over, stale, tracked = evaluate(args.root, ALLOWLIST)

    if tracked:
        print("call-http-only: known violations (allowlisted, ratcheting down):")
        for line in tracked:
            print(f"  {line}")
        print()

    if over or stale:
        if over:
            print("call-http-only: NEW violations:", file=sys.stderr)
            for line in over:
                print(f"  {line}", file=sys.stderr)
            print(file=sys.stderr)
        if stale:
            print("call-http-only: STALE allowlist entries:", file=sys.stderr)
            for line in stale:
                print(f"  {line}", file=sys.stderr)
            print(file=sys.stderr)
        print(
            "sol call command modules must reach the journal only via "
            "solstone.think.convey_client; lower stale allowlist counts as "
            "call.py files are converted.",
            file=sys.stderr,
        )
        return 1

    print("call-http-only: pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
