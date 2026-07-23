#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Four-way native-sol lead-slice conformance check."""

from __future__ import annotations

import ast
import inspect
import json
import sys
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from flask import Flask

import solstone.convey.reasons as reasons
from solstone.apps.activities.routes import activities_bp
from solstone.apps.support.routes import support_bp
from solstone.convey.chat import chat_bp
from solstone.convey.contract.assemble import build_document, rule_to_openapi_path
from solstone.convey.health import bp as health_bp
from solstone.convey.reasons import Reason
from solstone.convey.root import bp as root_bp

try:
    from scripts.build_native_sol_inventory import (
        AuthorityEntry,
        discover,
        is_private_app_authority,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution path.
    from build_native_sol_inventory import (  # type: ignore[no-redef]
        AuthorityEntry,
        discover,
        is_private_app_authority,
    )

REPO_ROOT = Path(__file__).resolve().parents[1]
LEAD_MANIFEST_PATH = REPO_ROOT / "core/fixtures/native-sol/lead-manifest.json"
SCHEMA = "native-sol-lead-manifest-v1"
REASON_CODES_BY_NAME = {
    name: value.code
    for name, value in vars(reasons).items()
    if isinstance(value, Reason)
}


@dataclass(frozen=True)
class ContractOperation:
    operation_id: str
    method: str
    route: str
    reason_codes: frozenset[str]


@dataclass(frozen=True)
class RawAuthorityEntry:
    authority: Path
    raw: dict[str, Any]


def load_manifest(path: Path = LEAD_MANIFEST_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def check_conformance(
    *,
    manifest: dict[str, Any] | None = None,
    root: Path = REPO_ROOT,
    document: dict[str, Any] | None = None,
    authorities: list[AuthorityEntry] | None = None,
    route_map: dict[tuple[str, str], Callable[..., Any]] | None = None,
) -> list[str]:
    root = root.resolve()
    manifest = manifest if manifest is not None else load_manifest()
    document = document if document is not None else build_document()
    authorities = authorities if authorities is not None else discover(root)
    route_map = route_map if route_map is not None else collect_flask_routes()

    errors = validate_manifest_shape(manifest)
    if errors:
        return errors

    manifest_entries = manifest["entries"]
    manifest_by_operation = {
        require_str(entry, "operation_id", "manifest entry"): entry
        for entry in manifest_entries
    }
    authority_by_operation = {entry.operation_id: entry for entry in authorities}
    raw_authority_by_operation = load_raw_authority_entries(root)
    contract_by_operation = collect_contract_operations(document)

    errors.extend(compare_operation_sets(manifest_by_operation, authority_by_operation))
    for operation_id in sorted(manifest_by_operation):
        manifest_entry = manifest_by_operation[operation_id]
        authority = authority_by_operation.get(operation_id)
        raw_authority = raw_authority_by_operation.get(operation_id)
        if authority is None:
            continue
        errors.extend(
            check_authority_entry(
                operation_id, manifest_entry, authority, raw_authority
            )
        )
        entry_type = manifest_entry["entry_type"]
        if entry_type == "http":
            errors.extend(
                check_http_entry(
                    operation_id,
                    manifest_entry,
                    authority,
                    contract_by_operation,
                    route_map,
                )
            )
        elif entry_type == "moved-stub":
            errors.extend(
                check_moved_stub(operation_id, manifest_entry, contract_by_operation)
            )
        elif entry_type == "top-level-chat":
            errors.extend(
                check_top_level_chat(
                    operation_id,
                    manifest_entry,
                    raw_authority,
                    contract_by_operation,
                    route_map,
                )
            )
        else:
            errors.append(f"{operation_id}: unsupported entry_type {entry_type!r}")
    return errors


def validate_manifest_shape(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema") != SCHEMA:
        errors.append(f"lead manifest schema must be {SCHEMA!r}")
        return errors
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        errors.append("lead manifest entries must be a list")
        return errors
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        label = f"lead manifest entry {index}"
        if not isinstance(entry, dict):
            errors.append(f"{label}: must be an object")
            continue
        operation_id = entry.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            errors.append(f"{label}: operation_id must be a non-empty string")
            continue
        if operation_id in seen:
            errors.append(f"{operation_id}: duplicate lead manifest operation_id")
        seen.add(operation_id)
        path = entry.get("path")
        if (
            not isinstance(path, list)
            or not path
            or any(not isinstance(item, str) or not item for item in path)
        ):
            errors.append(f"{operation_id}: path must be a non-empty string list")
        for key in ("surface", "kind", "entry_type"):
            if not isinstance(entry.get(key), str) or not entry[key]:
                errors.append(f"{operation_id}: {key} must be a non-empty string")
    return errors


def compare_operation_sets(
    manifest_by_operation: dict[str, dict[str, Any]],
    authority_by_operation: dict[str, AuthorityEntry],
) -> list[str]:
    errors: list[str] = []
    missing_authority = sorted(set(manifest_by_operation) - set(authority_by_operation))
    extra_authority = sorted(set(authority_by_operation) - set(manifest_by_operation))
    for operation_id in missing_authority:
        errors.append(f"{operation_id}: manifest entry has no app-local authority")
    for operation_id in extra_authority:
        authority = authority_by_operation[operation_id]
        errors.append(
            f"{operation_id}: migrated authority is not in lead manifest "
            f"({authority.authority})"
        )
    return errors


def check_authority_entry(
    operation_id: str,
    manifest_entry: dict[str, Any],
    authority: AuthorityEntry,
    raw_authority: RawAuthorityEntry | None,
) -> list[str]:
    errors: list[str] = []
    if raw_authority is None:
        errors.append(f"{operation_id}: raw authority entry was not found")
    checks = {
        "surface": authority.surface,
        "kind": authority.kind,
        "entry_type": authority.entry_type,
        "path": list(authority.path),
    }
    for key, actual in checks.items():
        expected = manifest_entry.get(key)
        if actual != expected:
            errors.append(
                f"{operation_id}: authority {key} {actual!r} != manifest {expected!r}"
            )
    return errors


def check_http_entry(
    operation_id: str,
    manifest_entry: dict[str, Any],
    authority: AuthorityEntry,
    contract_by_operation: dict[str, ContractOperation],
    route_map: dict[tuple[str, str], Callable[..., Any]],
) -> list[str]:
    errors: list[str] = []
    expected_method = require_str(manifest_entry, "method", operation_id)
    expected_route = require_str(manifest_entry, "route", operation_id)
    expected_contract = require_str(
        manifest_entry, "contract_operation_id", operation_id
    )
    expected_reason_codes = frozenset(manifest_entry.get("reason_codes", []))

    if authority.method != expected_method:
        errors.append(
            f"{operation_id}: authority method {authority.method!r} "
            f"!= manifest {expected_method!r}"
        )
    if authority.route != expected_route:
        errors.append(
            f"{operation_id}: authority route {authority.route!r} "
            f"!= manifest {expected_route!r}"
        )
    if authority.contract_operation_id != expected_contract:
        errors.append(
            f"{operation_id}: authority contract_operation_id "
            f"{authority.contract_operation_id!r} != manifest {expected_contract!r}"
        )

    route_key = (expected_method, expected_route)
    view = route_map.get(route_key)
    if view is None:
        errors.append(
            f"{operation_id}: no Flask route for {expected_method} {expected_route}"
        )
    else:
        route_reason_codes = route_error_reason_codes(view)
        if route_reason_codes != expected_reason_codes:
            errors.append(
                f"{operation_id}: route reason codes {sorted(route_reason_codes)!r} "
                f"!= manifest {sorted(expected_reason_codes)!r}"
            )

    contract = contract_by_operation.get(expected_contract)
    if contract is None:
        errors.append(f"{operation_id}: no contract operation {expected_contract}")
        return errors
    if contract.method != expected_method:
        errors.append(
            f"{operation_id}: contract method {contract.method!r} "
            f"!= manifest {expected_method!r}"
        )
    if contract.route != expected_route:
        errors.append(
            f"{operation_id}: contract route {contract.route!r} "
            f"!= manifest {expected_route!r}"
        )
    if contract.reason_codes != expected_reason_codes:
        errors.append(
            f"{operation_id}: contract reason codes {sorted(contract.reason_codes)!r} "
            f"!= manifest {sorted(expected_reason_codes)!r}"
        )
    return errors


def check_moved_stub(
    operation_id: str,
    manifest_entry: dict[str, Any],
    contract_by_operation: dict[str, ContractOperation],
) -> list[str]:
    errors: list[str] = []
    if any(
        key in manifest_entry for key in ("method", "route", "contract_operation_id")
    ):
        errors.append(
            f"{operation_id}: moved-stub manifest must not declare HTTP fields"
        )
    if operation_id in contract_by_operation:
        errors.append(f"{operation_id}: moved-stub must not have a contract operation")
    return errors


def check_top_level_chat(
    operation_id: str,
    manifest_entry: dict[str, Any],
    raw_authority: RawAuthorityEntry | None,
    contract_by_operation: dict[str, ContractOperation],
    route_map: dict[tuple[str, str], Callable[..., Any]],
) -> list[str]:
    errors: list[str] = []
    backing_contracts = manifest_entry.get("backing_contracts")
    if not isinstance(backing_contracts, list) or not backing_contracts:
        return [f"{operation_id}: top-level-chat must declare backing_contracts"]

    manifest_ids = [
        require_str(item, "operation_id", operation_id)
        for item in backing_contracts
        if isinstance(item, dict)
    ]
    authority_ids = (
        raw_authority.raw.get("backing_contract_operation_ids")
        if raw_authority is not None
        else None
    )
    if authority_ids != manifest_ids:
        errors.append(
            f"{operation_id}: authority backing_contract_operation_ids "
            f"{authority_ids!r} != manifest {manifest_ids!r}"
        )

    for item in backing_contracts:
        if not isinstance(item, dict):
            errors.append(f"{operation_id}: backing contract entry must be an object")
            continue
        backing_id = require_str(item, "operation_id", operation_id)
        expected_method = require_str(item, "method", backing_id)
        expected_route = require_str(item, "route", backing_id)
        contract = contract_by_operation.get(backing_id)
        if contract is None:
            errors.append(f"{operation_id}: missing chat backing contract {backing_id}")
            continue
        if contract.method != expected_method or contract.route != expected_route:
            errors.append(
                f"{operation_id}: backing contract {backing_id} is "
                f"{contract.method} {contract.route}, expected "
                f"{expected_method} {expected_route}"
            )
        if (expected_method, expected_route) not in route_map:
            errors.append(
                f"{operation_id}: missing chat backing route "
                f"{expected_method} {expected_route}"
            )
    return errors


def require_str(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}: {key} must be a non-empty string")
    return value


def collect_contract_operations(
    document: dict[str, Any],
) -> dict[str, ContractOperation]:
    output: dict[str, ContractOperation] = {}
    for route, methods in document["paths"].items():
        for raw_method, operation in methods.items():
            operation_id = operation["operationId"]
            reason_codes = {
                reason_code
                for response in operation.get("responses", {}).values()
                for reason_code in response.get("x-reason-codes", [])
            }
            output[operation_id] = ContractOperation(
                operation_id=operation_id,
                method=raw_method.upper(),
                route=route,
                reason_codes=frozenset(reason_codes),
            )
    return output


def collect_flask_routes() -> dict[tuple[str, str], Callable[..., Any]]:
    app = Flask(__name__)
    for blueprint in (activities_bp, support_bp, health_bp, chat_bp, root_bp):
        app.register_blueprint(blueprint)
    routes: dict[tuple[str, str], Callable[..., Any]] = {}
    for rule in app.url_map.iter_rules():
        view = app.view_functions[rule.endpoint]
        route = rule_to_openapi_path(rule.rule)
        for method in sorted(rule.methods or ()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            routes[(method, route)] = view
    return routes


def route_error_reason_codes(view: Callable[..., Any]) -> frozenset[str]:
    source = inspect.getsourcefile(view)
    if source is None:
        return frozenset()
    functions = module_functions(Path(source))
    return frozenset(collect_function_reason_codes(view.__name__, functions, set()))


@lru_cache(maxsize=None)
def module_functions(source: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def collect_function_reason_codes(
    function_name: str,
    functions: dict[str, ast.FunctionDef],
    seen: set[str],
) -> set[str]:
    if function_name in seen:
        return set()
    seen.add(function_name)
    function = functions.get(function_name)
    if function is None:
        return set()
    reason_codes: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node.func)
        if name == "error_response" and node.args:
            reason_code = reason_code_from_ast(node.args[0])
            if reason_code is not None:
                reason_codes.add(reason_code)
        elif (
            isinstance(node.func, ast.Name)
            and node.func.id in functions
            and node.func.id != function_name
        ):
            reason_codes.update(
                collect_function_reason_codes(node.func.id, functions, seen)
            )
    return reason_codes


def call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def reason_code_from_ast(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return REASON_CODES_BY_NAME.get(node.id)
    if isinstance(node, ast.Attribute):
        return REASON_CODES_BY_NAME.get(node.attr)
    return None


def load_raw_authority_entries(root: Path) -> dict[str, RawAuthorityEntry]:
    output: dict[str, RawAuthorityEntry] = {}
    authority_paths = sorted(
        set((root / "solstone").glob("**/native/authority.toml"))
        | set((root / "solstone").glob("**/native/**/authority.toml"))
    )
    for authority in authority_paths:
        if is_private_app_authority(authority, root):
            continue
        data = tomllib.loads(authority.read_text(encoding="utf-8"))
        for raw in data.get("entries", []):
            if not isinstance(raw, dict):
                continue
            operation_id = raw.get("operation_id")
            if isinstance(operation_id, str):
                output[operation_id] = RawAuthorityEntry(authority=authority, raw=raw)
    return output


def format_errors(errors: Iterable[str]) -> str:
    return "\n".join(f"- {error}" for error in errors)


def main() -> int:
    errors = check_conformance()
    if errors:
        print("native sol conformance failed:", file=sys.stderr)
        print(format_errors(errors), file=sys.stderr)
        return 1
    print("native sol conformance ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
