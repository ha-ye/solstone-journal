#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Provider install ownership lint.

Provider install state, leases, proof caches, and artifact manifests are
provider-owned operational domains. Production code must route status writes
through ``solstone.think.providers.install_state``, lease acquisition through
``install_lease``, and manifest/proof-cache mechanics through
``artifact_proof``. This gate catches raw writes, raw locks, private-owner
wrappers, and operational access to retired ``providers.bundled`` install state.

The production allowlist is intentionally empty. The one numbered migration calls
the owner migration API and is clean by construction, so a new production
violation should fail immediately.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OWNER_FILES: frozenset[str] = frozenset(
    {
        "solstone/think/providers/artifact_proof.py",
        "solstone/think/providers/install_lease.py",
        "solstone/think/providers/install_state.py",
    }
)
ALLOWLIST: dict[tuple[str, str], int] = {}
PRIVATE_OWNER_SYMBOLS = {
    "_cleanup_legacy_provider_install_config",
    "_read_current_unlocked",
    "_write_affirmative_cache",
}


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
        self.atomic_replace_names: set[str] = set()
        self.hold_lock_names: set[str] = set()
        self.flock_names: set[str] = set()
        self.open_names: set[str] = {"open"}
        self.os_modules: set[str] = set()
        self.os_open_names: set[str] = set()
        self.os_replace_names: set[str] = set()
        self.fcntl_modules: set[str] = set()
        self.journal_io_modules: set[str] = set()
        self.owner_modules: set[str] = set()
        self.private_owner_names: set[str] = set()
        self.status_path_names: set[str] = set()
        self.proof_cache_path_names: set[str] = set()
        self.lease_path_names: set[str] = set()
        self.manifest_path_names: set[str] = set()


def _collect_bindings(tree: ast.AST) -> _Bindings:
    bindings = _Bindings()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name in {
                    "solstone.think.journal_io",
                    "solstone.think.journal_io.atomic",
                    "solstone.think.journal_io.locking",
                }:
                    bindings.journal_io_modules.add(bound)
                elif alias.name in {
                    "solstone.think.providers.artifact_proof",
                    "solstone.think.providers.install_lease",
                    "solstone.think.providers.install_state",
                }:
                    bindings.owner_modules.add(bound)
                elif alias.name == "os":
                    bindings.os_modules.add(bound)
                elif alias.name == "fcntl":
                    bindings.fcntl_modules.add(bound)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                bound = alias.asname or alias.name
                if (
                    module
                    in {
                        "solstone.think.journal_io",
                        "solstone.think.journal_io.atomic",
                    }
                    and alias.name == "atomic_replace"
                ):
                    bindings.atomic_replace_names.add(bound)
                elif (
                    module
                    in {
                        "solstone.think.journal_io",
                        "solstone.think.journal_io.locking",
                    }
                    and alias.name == "hold_lock"
                ):
                    bindings.hold_lock_names.add(bound)
                elif module == "fcntl" and alias.name == "flock":
                    bindings.flock_names.add(bound)
                elif module == "os":
                    if alias.name == "open":
                        bindings.os_open_names.add(bound)
                    elif alias.name == "replace":
                        bindings.os_replace_names.add(bound)
                elif module == "builtins" and alias.name == "open":
                    bindings.open_names.add(bound)
                elif module == "solstone.think.providers.install_state":
                    if alias.name == "provider_status_path":
                        bindings.status_path_names.add(bound)
                    elif alias.name in PRIVATE_OWNER_SYMBOLS:
                        bindings.private_owner_names.add(bound)
                elif module == "solstone.think.providers.artifact_proof":
                    if alias.name == "proof_cache_path":
                        bindings.proof_cache_path_names.add(bound)
                    elif alias.name in {
                        "artifact_manifest_path",
                        "mlx_snapshot_manifest_path",
                        "mlx_variant_manifest_path",
                    }:
                        bindings.manifest_path_names.add(bound)
                    elif alias.name in PRIVATE_OWNER_SYMBOLS:
                        bindings.private_owner_names.add(bound)
                elif module == "solstone.think.providers.install_lease":
                    if alias.name == "lease_path":
                        bindings.lease_path_names.add(bound)
                    elif alias.name in PRIVATE_OWNER_SYMBOLS:
                        bindings.private_owner_names.add(bound)
    return bindings


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        if base:
            return f"{base}.{node.attr}"
    return None


def _called_attr(func: ast.expr, modules: set[str], attr: str, full_name: str) -> bool:
    if not isinstance(func, ast.Attribute) or func.attr != attr:
        return False
    if isinstance(func.value, ast.Name) and func.value.id in modules:
        return True
    return _dotted_name(func) == full_name


def _constant_path_parts(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _constant_path_parts(node.left) + _constant_path_parts(node.right)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = "".join(_constant_path_parts(node.left))
        right = "".join(_constant_path_parts(node.right))
        if left or right:
            return [left + right]
    if isinstance(node, ast.Call):
        parts: list[str] = []
        for arg in node.args:
            parts.extend(_constant_path_parts(arg))
        return parts
    return []


def _normalized_parts(parts: list[str]) -> list[str]:
    normalized: list[str] = []
    for part in parts:
        normalized.extend(
            piece for piece in part.replace("\\", "/").split("/") if piece
        )
    return normalized


def _contains_health_provider(parts: list[str]) -> bool:
    normalized = _normalized_parts(parts)
    return any(
        normalized[index : index + 2] == ["health", "providers"]
        for index in range(len(normalized) - 1)
    )


def _contains_manifest(parts: list[str]) -> bool:
    return any(
        part.endswith(".solstone-provider-manifest.json")
        or part.endswith("snapshot.manifest.json")
        or part.endswith("variant-solstone-budget1120.manifest.json")
        for part in _normalized_parts(parts)
    )


def _contains_provider_status(parts: list[str]) -> bool:
    if not _contains_health_provider(parts):
        return False
    return any(
        part in {"local.json", "parakeet.json"} for part in _normalized_parts(parts)
    )


def _contains_proof_cache(parts: list[str]) -> bool:
    if not _contains_health_provider(parts):
        return False
    return any(part.endswith(".proof-cache.json") for part in _normalized_parts(parts))


def _contains_lease(parts: list[str]) -> bool:
    if not _contains_health_provider(parts):
        return False
    return any(part.endswith(".lease") for part in _normalized_parts(parts))


def _is_status_path_call(node: ast.AST, bindings: _Bindings) -> bool:
    return _is_path_helper_call(
        node,
        bindings.status_path_names,
        "provider_status_path",
        "solstone.think.providers.install_state.provider_status_path",
        bindings,
    )


def _is_proof_cache_path_call(node: ast.AST, bindings: _Bindings) -> bool:
    return _is_path_helper_call(
        node,
        bindings.proof_cache_path_names,
        "proof_cache_path",
        "solstone.think.providers.artifact_proof.proof_cache_path",
        bindings,
    )


def _is_lease_path_call(node: ast.AST, bindings: _Bindings) -> bool:
    return _is_path_helper_call(
        node,
        bindings.lease_path_names,
        "lease_path",
        "solstone.think.providers.install_lease.lease_path",
        bindings,
    )


def _is_manifest_path_call(node: ast.AST, bindings: _Bindings) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id in bindings.manifest_path_names:
        return True
    dotted = _dotted_name(func)
    return dotted in {
        "solstone.think.providers.artifact_proof.artifact_manifest_path",
        "solstone.think.providers.artifact_proof.mlx_snapshot_manifest_path",
        "solstone.think.providers.artifact_proof.mlx_variant_manifest_path",
    }


def _is_path_helper_call(
    node: ast.AST,
    direct_names: set[str],
    attr: str,
    full_name: str,
    bindings: _Bindings,
) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id in direct_names:
        return True
    return _called_attr(func, bindings.owner_modules, attr, full_name)


def _assigned_path_names(
    tree: ast.AST, bindings: _Bindings
) -> tuple[set[str], set[str], set[str], set[str]]:
    status_names: set[str] = set()
    cache_names: set[str] = set()
    lease_names: set[str] = set()
    manifest_names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            value: ast.AST | None = None
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                value = node.value
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                value = node.value
                targets = [node.target]
            if value is None:
                continue
            is_status = _is_owned_path_expr(value, bindings, status_names, "status")
            is_cache = _is_owned_path_expr(value, bindings, cache_names, "cache")
            is_lease = _is_owned_path_expr(value, bindings, lease_names, "lease")
            is_manifest = _is_owned_path_expr(
                value, bindings, manifest_names, "manifest"
            )
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if is_status and target.id not in status_names:
                    status_names.add(target.id)
                    changed = True
                if is_cache and target.id not in cache_names:
                    cache_names.add(target.id)
                    changed = True
                if is_lease and target.id not in lease_names:
                    lease_names.add(target.id)
                    changed = True
                if is_manifest and target.id not in manifest_names:
                    manifest_names.add(target.id)
                    changed = True
    return status_names, cache_names, lease_names, manifest_names


def _is_owned_path_expr(
    node: ast.AST,
    bindings: _Bindings,
    known_names: set[str],
    kind: str,
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in known_names
    if kind == "status" and _is_status_path_call(node, bindings):
        return True
    if kind == "cache" and _is_proof_cache_path_call(node, bindings):
        return True
    if kind == "lease" and _is_lease_path_call(node, bindings):
        return True
    if kind == "manifest" and _is_manifest_path_call(node, bindings):
        return True
    parts = _constant_path_parts(node)
    if kind == "status":
        return _contains_provider_status(parts)
    if kind == "cache":
        return _contains_proof_cache(parts)
    if kind == "lease":
        return _contains_lease(parts)
    return _contains_manifest(parts)


def _called_atomic_replace(func: ast.expr, bindings: _Bindings) -> bool:
    if isinstance(func, ast.Name):
        return func.id in bindings.atomic_replace_names
    return _called_attr(
        func,
        bindings.journal_io_modules,
        "atomic_replace",
        "solstone.think.journal_io.atomic_replace",
    )


def _called_hold_lock(func: ast.expr, bindings: _Bindings) -> bool:
    if isinstance(func, ast.Name):
        return func.id in bindings.hold_lock_names
    return _called_attr(
        func,
        bindings.journal_io_modules,
        "hold_lock",
        "solstone.think.journal_io.hold_lock",
    )


def _called_flock(func: ast.expr, bindings: _Bindings) -> bool:
    if isinstance(func, ast.Name):
        return func.id in bindings.flock_names
    return _called_attr(func, bindings.fcntl_modules, "flock", "fcntl.flock")


def _called_os_open(func: ast.expr, bindings: _Bindings) -> bool:
    if isinstance(func, ast.Name):
        return func.id in bindings.os_open_names
    return _called_attr(func, bindings.os_modules, "open", "os.open")


def _called_os_replace(func: ast.expr, bindings: _Bindings) -> bool:
    if isinstance(func, ast.Name):
        return func.id in bindings.os_replace_names
    return _called_attr(func, bindings.os_modules, "replace", "os.replace")


def _called_open(func: ast.expr, bindings: _Bindings) -> bool:
    return isinstance(func, ast.Name) and func.id in bindings.open_names


def _is_path_replace_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Attribute) and node.func.attr == "replace"


def _is_path_write_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Attribute) and node.func.attr in {
        "write_text",
        "write_bytes",
        "open",
        "unlink",
    }


def _owned_kind(
    node: ast.AST,
    bindings: _Bindings,
    status_names: set[str],
    cache_names: set[str],
    lease_names: set[str],
    manifest_names: set[str],
) -> str | None:
    if _is_owned_path_expr(node, bindings, status_names, "status"):
        return "provider_status"
    if _is_owned_path_expr(node, bindings, cache_names, "cache"):
        return "proof_cache"
    if _is_owned_path_expr(node, bindings, lease_names, "lease"):
        return "provider_lease"
    if _is_owned_path_expr(node, bindings, manifest_names, "manifest"):
        return "provider_manifest"
    return None


def _bundled_access(node: ast.AST) -> bool:
    if isinstance(node, ast.Subscript):
        slc = node.slice
        return isinstance(slc, ast.Constant) and slc.value == "bundled"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return (
            node.func.attr == "get"
            and bool(node.args)
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "bundled"
        )
    return False


def _is_private_owner_expr(node: ast.AST, bindings: _Bindings) -> bool:
    if isinstance(node, ast.Name):
        return node.id in bindings.private_owner_names
    if isinstance(node, ast.Attribute) and node.attr in PRIVATE_OWNER_SYMBOLS:
        if isinstance(node.value, ast.Name) and node.value.id in bindings.owner_modules:
            return True
        dotted = _dotted_name(node)
        return dotted in {
            f"solstone.think.providers.install_state.{node.attr}",
            f"solstone.think.providers.artifact_proof.{node.attr}",
            f"solstone.think.providers.install_lease.{node.attr}",
        }
    return False


def scan_source(source: str, filename: str = "<source>") -> list[tuple[int, str, str]]:
    tree = ast.parse(source, filename=filename)
    bindings = _collect_bindings(tree)
    status_names, cache_names, lease_names, manifest_names = _assigned_path_names(
        tree, bindings
    )
    findings: list[tuple[int, str, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "solstone.think.providers.install_state",
            "solstone.think.providers.artifact_proof",
            "solstone.think.providers.install_lease",
        }:
            for alias in node.names:
                if alias.name in PRIVATE_OWNER_SYMBOLS:
                    findings.append((node.lineno, "private_owner_symbol", alias.name))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is not None and _is_private_owner_expr(value, bindings):
                findings.append(
                    (
                        node.lineno,
                        "private_owner_wrapper",
                        "private provider-install owner symbol alias or re-export",
                    )
                )
        elif isinstance(node, ast.FunctionDef):
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and _is_private_owner_expr(
                    child.func, bindings
                ):
                    findings.append(
                        (
                            child.lineno,
                            "private_owner_wrapper",
                            f"{node.name} wraps private provider-install owner symbol",
                        )
                    )
        elif _bundled_access(node):
            findings.append(
                (
                    node.lineno,
                    "providers_bundled_operational",
                    "providers.bundled access outside migration owner",
                )
            )
        if not isinstance(node, ast.Call):
            continue
        if _called_atomic_replace(node.func, bindings) and node.args:
            kind = _owned_kind(
                node.args[0],
                bindings,
                status_names,
                cache_names,
                lease_names,
                manifest_names,
            )
            if kind:
                findings.append((node.lineno, f"{kind}_replace", "atomic_replace"))
        elif _called_os_replace(node.func, bindings) and len(node.args) >= 2:
            kind = _owned_kind(
                node.args[1],
                bindings,
                status_names,
                cache_names,
                lease_names,
                manifest_names,
            )
            if kind:
                findings.append((node.lineno, f"{kind}_replace", "os.replace"))
        elif _is_path_replace_call(node) and node.args:
            kind = _owned_kind(
                node.args[0],
                bindings,
                status_names,
                cache_names,
                lease_names,
                manifest_names,
            )
            if kind:
                findings.append((node.lineno, f"{kind}_replace", "Path.replace"))
        elif _called_open(node.func, bindings) and node.args:
            kind = _owned_kind(
                node.args[0],
                bindings,
                status_names,
                cache_names,
                lease_names,
                manifest_names,
            )
            if kind:
                findings.append((node.lineno, f"{kind}_raw_open", "open"))
        elif _called_os_open(node.func, bindings) and node.args:
            kind = _owned_kind(
                node.args[0],
                bindings,
                status_names,
                cache_names,
                lease_names,
                manifest_names,
            )
            if kind:
                findings.append((node.lineno, f"{kind}_raw_open", "os.open"))
        elif _is_path_write_call(node):
            path_expr = node.func.value
            kind = _owned_kind(
                path_expr,
                bindings,
                status_names,
                cache_names,
                lease_names,
                manifest_names,
            )
            if kind:
                findings.append((node.lineno, f"{kind}_write", node.func.attr))
        elif _called_hold_lock(node.func, bindings) and node.args:
            kind = _owned_kind(
                node.args[0],
                bindings,
                status_names,
                cache_names,
                lease_names,
                manifest_names,
            )
            if kind:
                findings.append((node.lineno, "second_provider_install_lock", kind))
        elif _called_flock(node.func, bindings):
            if lease_names or any(
                _is_owned_path_expr(arg, bindings, lease_names, "lease")
                for arg in node.args
            ):
                findings.append(
                    (
                        node.lineno,
                        "second_provider_install_lock",
                        "fcntl.flock targets provider lease",
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
    parser = argparse.ArgumentParser(description="Provider install owner lint")
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repository root to scan (defaults to the checkout root).",
    )
    args = parser.parse_args(argv)
    over, stale, tracked = evaluate(args.root, ALLOWLIST)
    if tracked:
        print("provider-install-owner: known violations (allowlisted):")
        for line in tracked:
            print(f"  {line}")
        print()
    if over or stale:
        print("provider-install-owner: violations:", file=sys.stderr)
        for line in over:
            print(f"  {line}", file=sys.stderr)
        for line in stale:
            print(f"  stale allowlist: {line}", file=sys.stderr)
        print(
            "Route provider install state, leases, manifests, and proof caches "
            "through their owner APIs.",
            file=sys.stderr,
        )
        return 1
    print("provider-install-owner: pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
