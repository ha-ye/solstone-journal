#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Check native-sol migrated contracts against their Flask routes."""

from __future__ import annotations

import sys
from typing import Any

from flask import Flask

from solstone.apps.activities.routes import activities_bp
from solstone.apps.support.routes import support_bp
from solstone.convey.contract.assemble import build_document, rule_to_openapi_path
from solstone.convey.health import bp as health_bp

MIGRATED_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/app/activities/api/day/{day}/records"): "activities.list",
    ("GET", "/app/activities/api/day/{day}/record/{span_id}"): "activities.get",
    ("POST", "/app/activities/api/day/{day}/records"): "activities.create",
    (
        "POST",
        "/app/activities/api/day/{day}/record/{span_id}/update",
    ): "activities.update",
    (
        "POST",
        "/app/activities/api/day/{day}/record/{span_id}/mute",
    ): "activities.mute",
    (
        "POST",
        "/app/activities/api/day/{day}/record/{span_id}/unmute",
    ): "activities.unmute",
    ("GET", "/app/support/api/config"): "support.config",
    ("POST", "/app/support/api/draft"): "support.draft",
    ("POST", "/app/support/api/register"): "support.register",
    ("GET", "/app/support/api/articles"): "support.search",
    ("GET", "/app/support/api/articles/{slug}"): "support.article",
    ("GET", "/app/support/api/tickets"): "support.list",
    ("GET", "/app/support/api/tickets/{ticket_id}"): "support.show",
    ("POST", "/app/support/api/tickets"): "support.create",
    ("POST", "/app/support/api/tickets/{ticket_id}/reply"): "support.reply",
    ("POST", "/app/support/api/tickets/{ticket_id}/attachments"): "support.attach",
    ("POST", "/app/support/api/feedback"): "support.feedback",
    ("GET", "/app/support/api/announcements"): "support.announcements",
    ("GET", "/app/support/api/diagnostics"): "support.diagnose",
    ("GET", "/api/health/summary"): "health.summary",
    ("GET", "/api/health/full"): "health.full",
    ("GET", "/api/health/range"): "health.for_range",
    ("GET", "/api/health/pipeline"): "health.pipeline",
}


def _route_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(activities_bp)
    app.register_blueprint(support_bp)
    app.register_blueprint(health_bp)
    return app


def _flask_routes(app: Flask) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        for method in sorted(rule.methods or ()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            routes.add((method, rule_to_openapi_path(rule.rule)))
    return routes


def _contract_operations(
    document: dict[str, Any],
) -> tuple[dict[tuple[str, str], str], list[str]]:
    operations: dict[tuple[str, str], str] = {}
    duplicate_errors: list[str] = []
    seen_operation_ids: dict[str, tuple[str, str]] = {}
    for path, path_item in document["paths"].items():
        for raw_method, operation in path_item.items():
            method = raw_method.upper()
            operation_id = str(operation.get("operationId", ""))
            key = (method, path)
            operations[key] = operation_id
            if operation_id in seen_operation_ids:
                duplicate_errors.append(
                    "duplicate contract operation_id "
                    f"{operation_id}: {seen_operation_ids[operation_id]} and {key}"
                )
            seen_operation_ids[operation_id] = key
    return operations, duplicate_errors


def main() -> int:
    expected = MIGRATED_ROUTES
    expected_operation_ids = set(expected.values())
    flask_routes = _flask_routes(_route_app())
    contract_routes, errors = _contract_operations(build_document())

    for key, operation_id in sorted(expected.items()):
        if key not in flask_routes:
            errors.append(f"missing Flask route for {key[0]} {key[1]}")
        actual_operation_id = contract_routes.get(key)
        if actual_operation_id is None:
            errors.append(
                f"missing contract operation for {key[0]} {key[1]} "
                f"(expected {operation_id})"
            )
        elif actual_operation_id != operation_id:
            errors.append(
                f"contract operation mismatch for {key[0]} {key[1]}: "
                f"expected {operation_id}, found {actual_operation_id}"
            )

    for key, operation_id in sorted(contract_routes.items()):
        if operation_id not in expected_operation_ids:
            continue
        expected_key = next(
            expected_key
            for expected_key, expected_id in expected.items()
            if expected_id == operation_id
        )
        if key != expected_key:
            errors.append(
                f"migrated contract operation {operation_id} is bound to "
                f"{key[0]} {key[1]}, expected {expected_key[0]} {expected_key[1]}"
            )

    for key, operation_id in sorted(expected.items()):
        if key in flask_routes and key not in contract_routes:
            errors.append(
                f"migrated Flask route has no contract: {key[0]} {key[1]} "
                f"(expected {operation_id})"
            )

    if errors:
        print("native sol contract-route coverage failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("native sol contract-route coverage ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
