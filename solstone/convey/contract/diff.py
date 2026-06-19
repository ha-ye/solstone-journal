# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Contract-level OpenAPI comparison helpers."""

from __future__ import annotations

from collections.abc import Iterable

_HTTP_METHODS = {
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "trace",
}


def classify_changes(current: dict, committed: dict) -> list[str]:
    """Return human-readable breaking changes between two OpenAPI documents."""

    breaking: list[str] = []
    current_ops = _operation_map(current)
    committed_ops = _operation_map(committed)

    for path, method in sorted(set(committed_ops) - set(current_ops)):
        breaking.append(f"{method.upper()} {path}: removed endpoint")

    current_operation_ids = _operation_ids(current_ops.values())
    committed_operation_ids = _operation_ids(committed_ops.values())
    for operation_id in sorted(committed_operation_ids - current_operation_ids):
        breaking.append(f"{operation_id}: removed or renamed operationId")

    for key in sorted(set(committed_ops) & set(current_ops)):
        current_op = current_ops[key]
        committed_op = committed_ops[key]
        label = _operation_label(committed_op, *key)

        breaking.extend(
            _removed_request_fields(label, current_op, committed_op),
        )
        breaking.extend(
            _new_required_request_fields(label, current_op, committed_op),
        )
        breaking.extend(
            _removed_parameters(label, current_op, committed_op),
        )
        breaking.extend(
            _removed_response_fields(label, current_op, committed_op),
        )
        breaking.extend(
            _removed_reason_codes(label, current_op, committed_op),
        )

    return breaking


def undeclared_top_level_fields(allowed: set[str], actual: dict) -> list[str]:
    """Return top-level response keys that are not declared in the contract."""

    return sorted(set(actual) - allowed)


def _operation_map(document: dict) -> dict[tuple[str, str], dict]:
    operations: dict[tuple[str, str], dict] = {}
    paths = document.get("paths", {})
    if not isinstance(paths, dict):
        return operations
    for path, methods in paths.items():
        if not isinstance(path, str) or not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            method_name = str(method).lower()
            if method_name not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            operations[(path, method_name)] = operation
    return operations


def _operation_ids(operations: Iterable[dict]) -> set[str]:
    return {
        operation_id
        for operation in operations
        if isinstance(operation_id := operation.get("operationId"), str)
    }


def _operation_label(operation: dict, path: str, method: str) -> str:
    operation_id = operation.get("operationId")
    if isinstance(operation_id, str) and operation_id:
        return operation_id
    return f"{method.upper()} {path}"


def _single_content_schema(container: dict) -> dict:
    content = container.get("content", {})
    if not isinstance(content, dict) or not content:
        return {}
    media = next(iter(content.values()))
    if not isinstance(media, dict):
        return {}
    schema = media.get("schema", {})
    return schema if isinstance(schema, dict) else {}


def _request_schema(operation: dict) -> dict:
    request_body = operation.get("requestBody", {})
    if not isinstance(request_body, dict):
        return {}
    return _single_content_schema(request_body)


def _schema_properties(schema: dict) -> set[str] | None:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    return {name for name in properties if isinstance(name, str)}


def _schema_required(schema: dict) -> set[str]:
    required = schema.get("required", [])
    if not isinstance(required, list):
        return set()
    return {name for name in required if isinstance(name, str)}


def _removed_request_fields(
    label: str,
    current_op: dict,
    committed_op: dict,
) -> list[str]:
    committed_properties = _schema_properties(_request_schema(committed_op))
    if committed_properties is None:
        return []
    current_properties = _schema_properties(_request_schema(current_op)) or set()
    return [
        f"{label}: removed request field '{field}'"
        for field in sorted(committed_properties - current_properties)
    ]


def _new_required_request_fields(
    label: str,
    current_op: dict,
    committed_op: dict,
) -> list[str]:
    current_required = _schema_required(_request_schema(current_op))
    committed_required = _schema_required(_request_schema(committed_op))
    return [
        f"{label}: new required request field '{field}'"
        for field in sorted(current_required - committed_required)
    ]


def _parameter_keys(operation: dict) -> set[tuple[str, str]]:
    parameters = operation.get("parameters", [])
    if not isinstance(parameters, list):
        return set()
    keys: set[tuple[str, str]] = set()
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        name = parameter.get("name")
        location = parameter.get("in")
        if isinstance(name, str) and isinstance(location, str):
            keys.add((name, location))
    return keys


def _removed_parameters(
    label: str,
    current_op: dict,
    committed_op: dict,
) -> list[str]:
    removed = _parameter_keys(committed_op) - _parameter_keys(current_op)
    return [
        f"{label}: removed parameter '{name}' in {location}"
        for name, location in sorted(removed)
    ]


def _response_schema(response: dict) -> dict:
    if not isinstance(response, dict):
        return {}
    return _single_content_schema(response)


def _removed_response_fields(
    label: str,
    current_op: dict,
    committed_op: dict,
) -> list[str]:
    breaking: list[str] = []
    current_responses = current_op.get("responses", {})
    committed_responses = committed_op.get("responses", {})
    if not isinstance(current_responses, dict) or not isinstance(
        committed_responses, dict
    ):
        return breaking
    for status, committed_response in sorted(committed_responses.items()):
        if not isinstance(committed_response, dict):
            continue
        committed_properties = _schema_properties(
            _response_schema(committed_response),
        )
        if committed_properties is None:
            continue
        current_response = current_responses.get(status, {})
        current_properties = (
            _schema_properties(_response_schema(current_response)) or set()
        )
        for field in sorted(committed_properties - current_properties):
            breaking.append(f"{label}: removed response field '{field}'")
    return breaking


def _reason_codes(response: dict) -> set[str]:
    raw_codes = response.get("x-reason-codes", []) if isinstance(response, dict) else []
    if not isinstance(raw_codes, list):
        return set()
    return {code for code in raw_codes if isinstance(code, str)}


def _sse_reason_codes(response: dict) -> set[str]:
    if not isinstance(response, dict):
        return set()
    frame = response.get("x-sse-error-frame", {})
    if not isinstance(frame, dict):
        return set()
    raw_codes = frame.get("x-reason-codes", [])
    if not isinstance(raw_codes, list):
        return set()
    return {code for code in raw_codes if isinstance(code, str)}


def _removed_reason_codes(
    label: str,
    current_op: dict,
    committed_op: dict,
) -> list[str]:
    breaking: list[str] = []
    current_responses = current_op.get("responses", {})
    committed_responses = committed_op.get("responses", {})
    if not isinstance(current_responses, dict) or not isinstance(
        committed_responses, dict
    ):
        return breaking

    for status, committed_response in sorted(committed_responses.items()):
        if not isinstance(committed_response, dict):
            continue
        current_response = current_responses.get(status, {})
        for code in sorted(
            _reason_codes(committed_response) - _reason_codes(current_response)
        ):
            breaking.append(
                f"{label}: removed referenced reason code '{code}' from response {status}"
            )
        for code in sorted(
            _sse_reason_codes(committed_response) - _sse_reason_codes(current_response)
        ):
            breaking.append(
                f"{label}: removed SSE error-frame reason code '{code}' "
                f"from response {status}"
            )
    return breaking


__all__ = ["classify_changes", "undeclared_top_level_fields"]
