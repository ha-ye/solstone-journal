# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import io
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from solstone.apps.link.contract import OPERATIONS as LINK_OPERATIONS
from solstone.apps.observer.contract import OPERATIONS as OBSERVER_OPERATIONS
from solstone.convey import create_app
from solstone.convey.contract.assemble import build_document
from solstone.convey.contract.diff import (
    classify_changes,
    undeclared_top_level_fields,
)
from solstone.convey.push_contract import OPERATIONS as PUSH_OPERATIONS
from solstone.convey.secure_listener.identity import ConveyIdentity
from tests._baseline_harness import (
    isolated_app_env,
    mark_setup_complete,
    prepare_isolated_journal,
)

CONTRACTED_PATHS = {
    "/api/push/register",
    "/app/link/api/status",
    "/app/link/local-endpoints",
    "/app/link/pair",
    "/app/link/pair-start",
    "/app/link/unpair",
    "/app/observer/callosum",
    "/app/observer/ingest",
    "/app/observer/ingest/event",
    "/app/observer/ingest/manifest",
    "/app/observer/ingest/manifest/{day}",
    "/app/observer/ingest/segments/{day}",
    "/app/observer/register",
}

REGISTER_OBSERVER_PAYLOAD = {
    "platform": "linux",
    "hostname": "contract-host",
    "stream_type": "desktop",
    "version": "1",
}

PUSH_FINGERPRINT = "sha256:" + ("a" * 64)


@pytest.fixture
def contract_app(tmp_path: Path):
    journal = prepare_isolated_journal(tmp_path / "journal")
    mark_setup_complete(journal)
    with isolated_app_env(journal):
        app = create_app(journal=str(journal.resolve()))
        app.config["TESTING"] = True
        yield app, app.test_client(), journal


def _all_operations():
    return [*LINK_OPERATIONS, *OBSERVER_OPERATIONS, *PUSH_OPERATIONS]


def _operation(document: dict[str, Any], operation_id: str) -> dict[str, Any]:
    for path_item in document["paths"].values():
        for operation in path_item.values():
            if operation.get("operationId") == operation_id:
                return operation
    raise AssertionError(f"operation not found: {operation_id}")


def _response_schema(
    document: dict[str, Any],
    operation_id: str,
    status: int,
) -> dict[str, Any]:
    response = _operation(document, operation_id)["responses"][str(status)]
    content = response.get("content", {})
    if not content:
        return {}
    media = next(iter(content.values()))
    return media.get("schema", {})


def _declared_response_fields(
    document: dict[str, Any],
    operation_id: str,
    status: int = 200,
) -> set[str]:
    schema = _response_schema(document, operation_id, status)
    return set(schema.get("properties", {}))


def _global_reason_codes(document: dict[str, Any]) -> set[str]:
    reason_code = document["components"]["schemas"]["Error"]["properties"][
        "reason_code"
    ]
    return set(reason_code["enum"])


def _assert_structured_error(body: dict[str, Any], document: dict[str, Any]) -> None:
    assert {"error", "reason_code", "detail"}.issubset(body)
    assert body["reason_code"] in _global_reason_codes(document)


def _register_observer(client) -> str:
    response = client.post("/app/observer/register", json=REGISTER_OBSERVER_PAYLOAD)
    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert isinstance(body, dict)
    return str(body["key"])


def _push_identity() -> ConveyIdentity:
    return ConveyIdentity(
        mode="dl",
        fingerprint=PUSH_FINGERPRINT,
        device_label="Owner phone",
        paired_at="2026-06-18T00:00:00Z",
        session_id="contract-test",
    )


def test_all_fragment_routes_resolve(contract_app):
    app, _client, _journal = contract_app
    assert build_document()["paths"]

    for operation in _all_operations():
        matches = [
            rule
            for rule in app.url_map.iter_rules()
            if rule.rule == operation.rule and operation.method.upper() in rule.methods
        ]
        assert matches, f"{operation.method} {operation.rule} did not resolve"


def test_observer_auth_both_header_forms(contract_app):
    _app, client, _journal = contract_app
    document = build_document()
    allowed = _declared_response_fields(document, "observer.ingestManifest")
    key = _register_observer(client)

    responses = [
        client.get(
            "/app/observer/ingest/manifest",
            headers={"Authorization": f"Bearer {key}"},
        ),
        client.get(
            "/app/observer/ingest/manifest",
            headers={"X-Solstone-Observer": key},
        ),
    ]

    for response in responses:
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, dict)
        assert undeclared_top_level_fields(allowed, body) == []


def test_segments_protocol_version_shape(contract_app):
    _app, client, _journal = contract_app
    key = _register_observer(client)

    legacy = client.get(
        "/app/observer/ingest/segments/20250103",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert legacy.status_code == 200
    assert isinstance(legacy.get_json(), list)

    current = client.get(
        "/app/observer/ingest/segments/20250103",
        headers={
            "Authorization": f"Bearer {key}",
            "X-Solstone-Protocol-Version": "2",
        },
    )
    assert current.status_code == 200
    body = current.get_json()
    assert isinstance(body, dict)
    assert {"items", "total", "protocol_version"}.issubset(body)


def test_multipart_and_json_parsing(contract_app):
    _app, client, _journal = contract_app
    document = build_document()
    key = _register_observer(client)

    upload = client.post(
        "/app/observer/ingest",
        headers={"X-Solstone-Observer": key},
        data={
            "day": "20250103",
            "segment": "120000_300",
            "files": (io.BytesIO(b"contract upload"), "audio.flac"),
        },
        content_type="multipart/form-data",
    )
    assert upload.status_code != 415
    upload_body = upload.get_json()
    assert isinstance(upload_body, dict)
    if upload.status_code == 200:
        assert {"status", "segment", "files", "bytes"}.issubset(upload_body)
        allowed = _declared_response_fields(document, "observer.ingestUpload")
        assert undeclared_top_level_fields(allowed, upload_body) == []
    else:
        _assert_structured_error(upload_body, document)

    push = client.post(
        "/api/push/register",
        json={
            "device_token": "A" * 64,
            "bundle_id": "org.solpbc.solstone-swift",
            "environment": "development",
            "platform": "ios",
        },
        environ_overrides={"pl.identity": _push_identity()},
    )
    assert push.status_code == 200
    push_body = push.get_json()
    assert isinstance(push_body, dict)
    assert push_body == {"registered": True, "device_count": 1}


def test_structured_error_shape(contract_app):
    _app, client, _journal = contract_app
    document = build_document()

    response = client.get("/app/observer/ingest/manifest")

    assert response.status_code == 401
    body = response.get_json()
    assert isinstance(body, dict)
    _assert_structured_error(body, document)


def test_named_response_no_drift(contract_app):
    _app, client, _journal = contract_app
    document = build_document()
    allowed = _declared_response_fields(document, "link.status")

    response = client.get("/app/link/api/status")

    assert response.status_code == 200
    body = response.get_json()
    assert isinstance(body, dict)
    assert undeclared_top_level_fields(allowed, body) == []
    assert allowed <= set(body)


def test_no_r0_routes_in_artifact():
    document = build_document()

    assert "/api/config/convey" not in document["paths"]
    assert "/api/system/status" not in document["paths"]
    assert "/api/chat/session" not in document["paths"]
    assert set(document["paths"]) == CONTRACTED_PATHS
    assert len(document["paths"]) == 13


def test_all_referenced_reason_codes_are_global():
    document = build_document()
    global_codes = _global_reason_codes(document)
    referenced_codes: set[str] = set()

    for path_item in document["paths"].values():
        for operation in path_item.values():
            for response in operation.get("responses", {}).values():
                referenced_codes.update(response.get("x-reason-codes", []))
                sse_error_frame = response.get("x-sse-error-frame", {})
                referenced_codes.update(sse_error_frame.get("x-reason-codes", []))

    assert referenced_codes - global_codes == set()


def test_scenario_removed_named_field_is_breaking():
    committed = build_document()
    current = deepcopy(committed)
    properties = _response_schema(current, "observer.register", 200)["properties"]
    properties.pop("prefix")

    breaking = classify_changes(current, committed)

    assert any(
        "observer.register: removed response field 'prefix'" in item
        for item in breaking
    )


def test_scenario_added_optional_field_is_silent():
    committed = build_document()
    current = deepcopy(committed)
    properties = _response_schema(current, "observer.register", 200)["properties"]
    properties["optional_future"] = {"type": "string"}

    assert classify_changes(current, committed) == []


def test_scenario_new_required_request_field_is_breaking():
    committed = build_document()
    current = deepcopy(committed)
    schema = _operation(current, "observer.register")["requestBody"]["content"][
        "application/json"
    ]["schema"]
    schema["properties"]["new_required"] = {"type": "string"}
    schema["required"].append("new_required")

    breaking = classify_changes(current, committed)

    assert any(
        "observer.register: new required request field 'new_required'" in item
        for item in breaking
    )


def test_scenario_removed_referenced_reason_code_is_breaking():
    committed = build_document()
    current = deepcopy(committed)
    response = _operation(current, "link.status")["responses"]["403"]
    response["x-reason-codes"].remove("pl_revoked")

    breaking = classify_changes(current, committed)

    assert any(
        "link.status: removed referenced reason code 'pl_revoked'" in item
        for item in breaking
    )


def test_scenario_global_enum_addition_is_not_breaking():
    committed = build_document()
    current = deepcopy(committed)
    enum = current["components"]["schemas"]["Error"]["properties"]["reason_code"][
        "enum"
    ]
    enum.append("future_unreferenced_code")

    assert classify_changes(current, committed) == []


def test_scenario_undeclared_field_then_declared():
    assert undeclared_top_level_fields({"a", "b"}, {"a": 1, "c": 2}) == ["c"]
    assert undeclared_top_level_fields({"a", "b", "c"}, {"a": 1, "c": 2}) == []
