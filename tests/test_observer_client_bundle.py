# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from solstone.convey.contract import (
    observer_bundle,
    observer_bundle_compatibility,
    observer_bundle_export,
    observer_bundle_recording,
    observer_bundle_verification,
)

EXPECTED_OPERATION_IDS = [
    "callosum.rootEvents",
    "chat.openSolChatRequest",
    "link.pair",
    "observer.callosumStream",
    "observer.ingestEvent",
    "observer.ingestSegments",
    "observer.ingestUpload",
    "observer.register",
]

EXPECTED_COMPONENTS = [
    "CallosumEvent",
    "Error",
    "SegmentFile",
    "SegmentItem",
    "SegmentsEnvelope",
]

EXPECTED_CONSUMER_IDENTIFIERS = [
    "solstone-android",
    "solstone-browser",
    "solstone-linux",
    "solstone-macos",
    "solstone-swift",
    "solstone-tmux",
    "solstone-windows",
]

WINDOWS_REV = "19c972c4fea775176cea6421ac8b87f3bb20ab42"
LINUX_REV = "1c679db1ce6f9a65db70c5aae0ca2fad677416ef"
BROWSER_REV = "998c1095cd8f766dd188bece5ad6527444f8dfac"

EXPECTED_AUDITED_COMMITS = [
    {"commit": WINDOWS_REV, "consumer": "solstone-windows"},
    {"commit": LINUX_REV, "consumer": "solstone-linux"},
    {"commit": BROWSER_REV, "consumer": "solstone-browser"},
]

EXPECTED_SEARCHED_FILES = [
    ("solstone-browser", BROWSER_REV, "extension/journal.js"),
    ("solstone-linux", LINUX_REV, "crates/solstone-linux/src/chat_bridge.rs"),
    ("solstone-linux", LINUX_REV, "crates/solstone-linux/src/upload.rs"),
    ("solstone-windows", WINDOWS_REV, "crates/observer-pl/src/lib.rs"),
    ("solstone-windows", WINDOWS_REV, "crates/observer-pl/src/wire.rs"),
    (
        "solstone-windows",
        WINDOWS_REV,
        "crates/pl-transport-win/src/journal_bridge.rs",
    ),
]

EXPECTED_DIRECT_PATHS = [
    ("solstone-browser", "/app/observer/register", "bundled"),
    ("solstone-browser", "/app/observer/ingest", "bundled"),
    ("solstone-browser", "/app/observer/ingest/event", "bundled"),
    ("solstone-browser", "/app/observer/ingest/segments/{day}", "bundled"),
    (
        "solstone-browser",
        "/enroll/device",
        "relay_session_control_excluded_from_journal_projection",
    ),
    ("solstone-linux", "/app/observer/register", "bundled"),
    ("solstone-linux", "/app/observer/ingest", "bundled"),
    ("solstone-linux", "/app/observer/ingest/event", "bundled"),
    ("solstone-linux", "/app/observer/ingest/segments/{day}", "bundled"),
    ("solstone-linux", "/app/observer/callosum", "bundled"),
    ("solstone-linux", "/api/chat/sol_chat_request/open", "bundled"),
    (
        "solstone-linux",
        "/app/chat/{day}#event-{index}",
        "browser_navigation_excluded_from_api_projection",
    ),
    ("solstone-linux", "/api/sol_voice", "consumer_drift_adoption_blocker"),
    ("solstone-windows", "/app/network/pair", "bundled"),
    ("solstone-windows", "/app/observer/register", "bundled"),
    ("solstone-windows", "/app/observer/ingest", "bundled"),
    ("solstone-windows", "/app/observer/ingest/event", "bundled"),
    ("solstone-windows", "/app/observer/ingest/segments/{day}", "bundled"),
    ("solstone-windows", "/sse/events", "bundled"),
]

EXPECTED_GENERATOR_INPUTS = [
    (
        "bundle.projection_builder",
        "solstone/convey/contract/observer_bundle.py",
        "projection_builder",
    ),
    (
        "bundle.recording",
        "solstone/convey/contract/observer_bundle_recording.py",
        "fixture_vector_builder",
    ),
    (
        "extension.observer_sse_error_frame",
        "solstone/apps/observer/contract.py",
        "code_adjacent_extension",
    ),
    (
        "extension.root_chat_native_subset",
        "solstone/convey/root_contract.py",
        "code_adjacent_extension",
    ),
    ("fragment.chat", "solstone/convey/chat_contract.py", "code_adjacent_fragment"),
    ("fragment.link", "solstone/apps/network/contract.py", "code_adjacent_fragment"),
    (
        "fragment.observer",
        "solstone/apps/observer/contract.py",
        "code_adjacent_fragment",
    ),
    ("fragment.root", "solstone/convey/root_contract.py", "code_adjacent_fragment"),
    ("openapi.assembler", "solstone/convey/contract/assemble.py", "openapi_source"),
    ("producer.chat_routes", "solstone/convey/chat.py", "producer"),
    (
        "producer.chat_sol_initiated_copy",
        "solstone/convey/sol_initiated/copy.py",
        "producer",
    ),
    (
        "producer.chat_sol_initiated_events",
        "solstone/convey/sol_initiated/events.py",
        "producer",
    ),
    ("producer.chat_stream", "solstone/convey/chat_stream.py", "producer"),
    ("producer.observer_routes", "solstone/apps/observer/routes.py", "producer"),
    ("producer.observer_utils", "solstone/apps/observer/utils.py", "producer"),
    ("producer.protocol", "solstone/observe/protocol.py", "producer"),
    ("producer.root_sse", "solstone/convey/root.py", "producer"),
    ("reason_codes", "solstone/convey/reasons.py", "vocabulary_source"),
]

EXPECTED_GENERATOR_INPUT_IDS = {
    "bundle.projection_builder",
    "bundle.recording",
    "extension.observer_sse_error_frame",
    "extension.root_chat_native_subset",
    "fragment.chat",
    "fragment.link",
    "fragment.observer",
    "fragment.root",
    "openapi.assembler",
    "producer.chat_routes",
    "producer.chat_sol_initiated_copy",
    "producer.chat_sol_initiated_events",
    "producer.chat_stream",
    "producer.observer_routes",
    "producer.observer_utils",
    "producer.protocol",
    "producer.root_sse",
    "reason_codes",
}

EXPECTED_BUNDLE_PAYLOAD_SHA256 = {
    observer_bundle.CONSUMER_AUDIT_REL: (
        "f3562062aeb971c9dc95ae5d14333566b28431758bcd232c33c093757df7bc18"
    ),
    observer_bundle.FIXTURES_REL: (
        "9749a50daba9b4a270da045d350bc5edb7a42c9723fa0bf420c8fb8a4a0415f8"
    ),
    observer_bundle.PROJECTION_REL: (
        "28c055279ab7d80c809a43c5f710ccebc4da643fec3f8e6beae48823c17c46c5"
    ),
    observer_bundle.VECTORS_REL: (
        "7a5132c57b61e2a615a22719abc77e40b708d4a6636c45690cc522dc26c36dec"
    ),
}


def _generator_input_class_cases() -> list[tuple[str, str, str]]:
    cases: dict[str, tuple[str, str, str]] = {}
    for input_id, path, role in EXPECTED_GENERATOR_INPUTS:
        cases.setdefault(role, (role, input_id, path))
    return sorted(cases.values())


EXPECTED_FIXTURE_IDS = [
    "declared.observer.ingestSegments.custody_unknown_rejected",
    "declared.observer.ingestSegments.envelope_total_mismatch",
    "declared.observer.ingestUpload.status_unknown_rejected",
    "example.callosum.rootEvents.response.200.text-event-stream.default",
    "example.chat.openSolChatRequest.request.body.application-json.default",
    "example.chat.openSolChatRequest.response.200.application-json.default",
    "example.link.pair.request.body.application-json.default",
    "example.link.pair.response.200.application-json.default",
    "example.observer.callosumStream.response.200.text-event-stream.default",
    "example.observer.ingestEvent.request.body.application-json.default",
    "example.observer.ingestEvent.response.200.application-json.default",
    "example.observer.ingestSegments.response.200.application-json.legacy",
    "example.observer.ingestSegments.response.200.application-json.v2",
    "example.observer.ingestUpload.request.body.multipart-form-data.default",
    "example.observer.ingestUpload.response.200.application-json.duplicate",
    "example.observer.ingestUpload.response.200.application-json.normal",
    "example.observer.register.request.body.application-json.default",
    "example.observer.register.response.200.application-json.default",
    "recorded.auth.bearer.segments",
    "recorded.auth.handle.segments",
    "recorded.chat.openSolChatRequest.missing",
    "recorded.chat.openSolChatRequest.ok",
    "recorded.ingestUpload.collision",
    "recorded.ingestUpload.conflict",
    "recorded.ingestUpload.duplicate",
    "recorded.ingestUpload.failed",
    "recorded.ingestUpload.ok",
    "recorded.segments.custody_statuses",
    "recorded.segments.legacy.absent_header",
    "recorded.segments.legacy.unparseable_header",
    "recorded.segments.submitted_name_omitted",
    "recorded.segments.v2.envelope",
    "recorded.sse.observer.data",
    "recorded.sse.observer.error",
    "recorded.sse.observer.heartbeat",
    "recorded.sse.root.data_unknown_event",
    "recorded.sse.root.heartbeat",
]

EXPECTED_VECTOR_IDS = [
    "callosum.rootEvents.sse.data_unknown_event",
    "callosum.rootEvents.sse.heartbeat",
    "chat.openSolChatRequest.missing_required_field",
    "chat.openSolChatRequest.ok",
    "observer.auth.bearer",
    "observer.auth.handle",
    "observer.callosumStream.sse.data",
    "observer.callosumStream.sse.error",
    "observer.callosumStream.sse.heartbeat",
    "observer.ingestSegments.custody_statuses",
    "observer.ingestSegments.custody_unknown_rejected",
    "observer.ingestSegments.envelope_total_mismatch",
    "observer.ingestSegments.legacy_array.absent_header",
    "observer.ingestSegments.legacy_array.unparseable_header",
    "observer.ingestSegments.submitted_name_fallback",
    "observer.ingestSegments.v2_envelope",
    "observer.ingestUpload.status.collision",
    "observer.ingestUpload.status.conflict",
    "observer.ingestUpload.status.duplicate",
    "observer.ingestUpload.status.failed",
    "observer.ingestUpload.status.ok",
    "observer.ingestUpload.status_unknown_rejected",
]

EXPECTED_VECTOR_DECISIONS = {
    "callosum.rootEvents.sse.data_unknown_event": {
        "action": "pass_through",
        "frame_kind": "data",
        "kind": "sse_frame",
        "unknown_event_behavior": "preserve",
    },
    "callosum.rootEvents.sse.heartbeat": {
        "action": "ignore_keepalive",
        "frame_kind": "heartbeat",
        "kind": "sse_frame",
    },
    "chat.openSolChatRequest.missing_required_field": {
        "accepted": False,
        "kind": "chat_open_request",
        "missing_field_behavior": "absent_malformed_empty_or_blank_rejected",
        "reason_code": "missing_required_field",
    },
    "chat.openSolChatRequest.ok": {
        "accepted": True,
        "kind": "chat_open_request",
        "missing_field_behavior": "non_empty_trimmed_request_id_required",
        "result": "ok_true",
    },
    "observer.auth.bearer": {
        "accepted": True,
        "auth_form": "authorization_bearer",
        "kind": "auth_header_form",
        "precedence": "x_solstone_observer_preferred_when_both_present",
    },
    "observer.auth.handle": {
        "accepted": True,
        "auth_form": "x_solstone_observer",
        "kind": "auth_header_form",
        "precedence": "x_solstone_observer_preferred_when_both_present",
    },
    "observer.callosumStream.sse.data": {
        "action": "dispatch_callosum_event",
        "frame_kind": "data",
        "kind": "sse_frame",
        "unknown_event_behavior": "preserve",
    },
    "observer.callosumStream.sse.error": {
        "action": "surface_error_and_close",
        "frame_kind": "error",
        "kind": "sse_frame",
        "reason_code": "pl_revoked",
    },
    "observer.callosumStream.sse.heartbeat": {
        "action": "ignore_keepalive",
        "frame_kind": "heartbeat",
        "kind": "sse_frame",
    },
    "observer.ingestSegments.custody_statuses": {
        "holding_by_status": {
            "missing": "not_held",
            "present": "held",
            "processed": "held",
        },
        "kind": "custody_status",
        "unknown_status": "reject",
    },
    "observer.ingestSegments.custody_unknown_rejected": {
        "kind": "custody_unknown",
        "status": "unknown",
        "unknown_status": "reject",
    },
    "observer.ingestSegments.envelope_total_mismatch": {
        "expected": "total_equals_items_length",
        "kind": "envelope_integrity",
        "valid": False,
    },
    "observer.ingestSegments.legacy_array.absent_header": {
        "absent_or_unparseable_uses": 1,
        "header": "absent",
        "kind": "protocol_variant",
        "parsed_version": 1,
        "response_variant": "legacy_array",
    },
    "observer.ingestSegments.legacy_array.unparseable_header": {
        "absent_or_unparseable_uses": 1,
        "header": "unparseable",
        "kind": "protocol_variant",
        "parsed_version": 1,
        "response_variant": "legacy_array",
    },
    "observer.ingestSegments.submitted_name_fallback": {
        "fallback": "name",
        "kind": "submitted_name_fallback",
        "submitted_name_present": False,
    },
    "observer.ingestSegments.v2_envelope": {
        "current_protocol_version": 2,
        "header": "2",
        "kind": "protocol_variant",
        "parsed_version": 2,
        "response_variant": "v2_envelope",
    },
    "observer.ingestUpload.status.collision": {
        "accepted": True,
        "client_action": "adopt_remapped_segment",
        "http_status": 200,
        "kind": "ingest_status",
        "original_key_source": "segment_original",
        "status": "collision",
        "stored_key_precedence": ["segment", "segment_original"],
        "stored_key_source": "segment",
    },
    "observer.ingestUpload.status.conflict": {
        "accepted": False,
        "client_action": "preserve_local_and_surface_conflict",
        "http_status": 409,
        "kind": "ingest_status",
        "status": "conflict",
        "stored_key_precedence": ["existing_segment"],
        "stored_key_source": "existing_segment",
    },
    "observer.ingestUpload.status.duplicate": {
        "accepted": True,
        "client_action": "adopt_existing_segment_without_reupload",
        "http_status": 200,
        "kind": "ingest_status",
        "status": "duplicate",
        "stored_key_precedence": ["existing_segment"],
        "stored_key_source": "existing_segment",
    },
    "observer.ingestUpload.status.failed": {
        "accepted": False,
        "client_action": "preserve_local_and_surface_failure",
        "http_status": 422,
        "kind": "ingest_status",
        "status": "failed",
        "stored_key_precedence": [],
        "stored_key_source": None,
    },
    "observer.ingestUpload.status.ok": {
        "accepted": True,
        "client_action": "adopt_segment",
        "http_status": 200,
        "kind": "ingest_status",
        "status": "ok",
        "stored_key_precedence": ["segment"],
        "stored_key_source": "segment",
    },
    "observer.ingestUpload.status_unknown_rejected": {
        "kind": "closed_vocabulary_unknown",
        "status": "unknown",
        "unknown_value_behavior": "reject",
        "vocabulary": "observer.ingestUpload.status",
    },
}

EXPECTED_VECTOR_DECISION_SHA256 = {
    "callosum.rootEvents.sse.data_unknown_event": "b36f4a7f0f94ffa27897f8a70a32914b7386d0178405e4d9fae4cce9215df86a",
    "callosum.rootEvents.sse.heartbeat": "30131cdbf14bbe80b70c7e3b79d477ae7e3bb360469a98bc28921f71305adc24",
    "chat.openSolChatRequest.missing_required_field": "d1a585a7a21f44a4894977232e07741649ce131a53514c3a34d936809409fdba",
    "chat.openSolChatRequest.ok": "571eefe44444b1dd7c766b038ac885869f658cbd9532662bfbc9e274d85e998c",
    "observer.auth.bearer": "073b9272eb1bb6c3e0dda28ce8e0140b8bf9a8de267c71ffb7589e3bc3b517e1",
    "observer.auth.handle": "f420458236bbfcb5de22bc59ba571fbd2af80003051041dddff48976c4185254",
    "observer.callosumStream.sse.data": "ab4e96dde967e6996ecb9ccf233305792c36810daf53fa96bfe55abf88f1df18",
    "observer.callosumStream.sse.error": "af8ff7fc30d2d1e1cac259dcc5a46563b829ac25c122b2623cab7e2e6c12a4bf",
    "observer.callosumStream.sse.heartbeat": "30131cdbf14bbe80b70c7e3b79d477ae7e3bb360469a98bc28921f71305adc24",
    "observer.ingestSegments.custody_statuses": "119d005c83fdb0423d7397b6c27c1d824770b05f7fd8dbfad1816d8421d76734",
    "observer.ingestSegments.custody_unknown_rejected": "9e2a54a0547a99c748a5595d394d448a007ab91953a11d0fce5a60b88f1e95b3",
    "observer.ingestSegments.envelope_total_mismatch": "ee9ba7e548ad8f8b82bc9d235e1a751051f6f35d70f5a6f600787fc2d18e3bfa",
    "observer.ingestSegments.legacy_array.absent_header": "78bf9f4d341eea6f6b7fd352280544b7a1943d3a3b28b5441681e35dc763d7e6",
    "observer.ingestSegments.legacy_array.unparseable_header": "ad5e59be2cc5b929ba8263f0447d3c604c97a6b803eee7d93d6ddd193e63beeb",
    "observer.ingestSegments.submitted_name_fallback": "282af8b4034979150e2c2ef0c4996b0aa56e49d7bfade871f94a776c7ea036e1",
    "observer.ingestSegments.v2_envelope": "4709563c5cd128eded1749a8f39bb1c61722c061d74b2bbddefe3f3f9facddb4",
    "observer.ingestUpload.status.collision": "aaa35703ffcb3ceea229ea9f6fbe6c79d63da647acbbeeec0b9848995c48935f",
    "observer.ingestUpload.status.conflict": "913ca78ed69b5becf2ac53d286ac026e5b8e3ed1653001e9c67bc4853337214e",
    "observer.ingestUpload.status.duplicate": "0fbff5b541a5536cb1db9cb94700b63dd2209d8cd1127c0df989108553007ff1",
    "observer.ingestUpload.status.failed": "2bec6626de450ac7da53425bb40f927c337636e8c5c174a7fff379039cd0ffdb",
    "observer.ingestUpload.status.ok": "2aa9574f1e1227ef56a70ae733d7f1a171ae7a4b7db714bb5b5b4e7cbe9d52f4",
    "observer.ingestUpload.status_unknown_rejected": "758f122cfbd6e48d03b0c41ebcb609525040c70e07ac2c123dd0ed9e392d9b35",
}


@pytest.fixture(scope="module")
def bundle_files() -> dict[Path, str]:
    return observer_bundle.build_bundle_files()


def _json_file(files: dict[Path, str], path: Path) -> dict[str, Any]:
    return json.loads(files[path])


def _projection_operation_ids(projection: dict[str, Any]) -> list[str]:
    operation_ids: list[str] = []
    for methods in projection["paths"].values():
        for operation in methods.values():
            operation_ids.append(operation["operationId"])
    return sorted(operation_ids)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clone_bundle_files(files: dict[Path, str]) -> dict[Path, str]:
    return {path: text for path, text in files.items()}


def _manifest(files: dict[Path, str]) -> dict[str, Any]:
    return json.loads(files[observer_bundle.MANIFEST_REL])


def _set_manifest(files: dict[Path, str], manifest: dict[str, Any]) -> None:
    files[observer_bundle.MANIFEST_REL] = observer_bundle.render_json(manifest)


def _payload(files: dict[Path, str], rel_path: Path) -> dict[str, Any]:
    return json.loads(files[rel_path])


def _set_payload(
    files: dict[Path, str], rel_path: Path, payload: dict[str, Any]
) -> None:
    files[rel_path] = observer_bundle.render_json(payload)
    _refresh_manifest_inventory(files)


def _set_bundle_version(files: dict[Path, str], version: str) -> None:
    manifest = _manifest(files)
    manifest["bundle_semver"] = version
    _set_manifest(files, manifest)


def _next_minor_version() -> str:
    major, minor, _patch = observer_bundle.parse_semver(observer_bundle.BUNDLE_SEMVER)
    return f"{major}.{minor + 1}.0"


def _next_major_version() -> str:
    major, _minor, _patch = observer_bundle.parse_semver(observer_bundle.BUNDLE_SEMVER)
    return f"{major + 1}.0.0"


def _refresh_manifest_inventory(files: dict[Path, str]) -> None:
    manifest = _manifest(files)
    payload_paths = sorted(
        path for path in files if path != observer_bundle.MANIFEST_REL
    )
    manifest["files"] = [
        {
            "path": path.relative_to(observer_bundle.BUNDLE_REL_DIR).as_posix(),
            "sha256": _sha256_text(files[path]),
        }
        for path in payload_paths
    ]
    _set_manifest(files, manifest)


def _write_bundle_tree(root: Path, files: dict[Path, str]) -> Path:
    for rel_path, text in files.items():
        output = root / rel_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    return root / observer_bundle.BUNDLE_REL_DIR


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _git_output(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_git_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "observer-bundle-test@example.invalid")
    _git(repo, "config", "user.name", "Observer Bundle Test")
    (repo / ".keep").write_text("", encoding="utf-8")
    _git(repo, "add", ".keep")
    _git(repo, "commit", "-m", "initial")


def _commit_bundle(repo: Path, files: dict[Path, str], message: str) -> None:
    _write_bundle_tree(repo, files)
    _git(repo, "add", observer_bundle.BUNDLE_REL_DIR.as_posix())
    _git(repo, "commit", "-m", message)


def _loose_object_path(repo: Path, object_id: str) -> Path:
    path = repo / ".git" / "objects" / object_id[:2] / object_id[2:]
    assert path.exists()
    return path


def _assert_history_failure(
    repo: Path,
    files: dict[Path, str],
    text: str,
    *,
    enforce_current_contract: bool = True,
) -> None:
    failures = observer_bundle_compatibility.check_bundle_compatibility(
        repo,
        files,
        enforce_current_contract=enforce_current_contract,
    )
    assert failures
    assert text in failures[0]


def _copy_manifest_with_file_path(files: dict[Path, str], path: str) -> dict[Path, str]:
    mutated = _clone_bundle_files(files)
    manifest = _manifest(mutated)
    manifest["files"][0] = {**manifest["files"][0], "path": path}
    _set_manifest(mutated, manifest)
    return mutated


def _bundle_dir_with_files(tmp_path: Path, files: dict[Path, str]) -> Path:
    root = tmp_path / "repo"
    return _write_bundle_tree(root, files)


def _copy_manifest_source_inputs(repo: Path, manifest: dict[str, Any]) -> None:
    source_root = observer_bundle._repo_root(None)
    for item in manifest["generator_inputs"]:
        source = source_root / item["path"]
        if not source.exists():
            continue
        destination = repo / item["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())


def _description_patch_candidate(
    files: dict[Path, str], version: str
) -> dict[Path, str]:
    mutated = _clone_bundle_files(files)
    projection = _payload(mutated, observer_bundle.PROJECTION_REL)
    projection["info"]["description"] = "Description-only compatibility change."
    _set_payload(mutated, observer_bundle.PROJECTION_REL, projection)
    _set_bundle_version(mutated, version)
    return mutated


def _closed_status_add_candidate(
    files: dict[Path, str], version: str
) -> dict[Path, str]:
    mutated = _clone_bundle_files(files)
    projection = _payload(mutated, observer_bundle.PROJECTION_REL)
    status_schema = projection["paths"]["/app/observer/ingest"]["post"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]["properties"]["status"]
    status_schema["enum"].append("new")
    _set_payload(mutated, observer_bundle.PROJECTION_REL, projection)
    manifest = _manifest(mutated)
    for vocabulary in manifest["vocabularies"]:
        if vocabulary["id"] == "observer.ingestUpload.status":
            vocabulary["values"].append("new")
    _set_manifest(mutated, manifest)
    _set_bundle_version(mutated, version)
    return mutated


def _extensible_chat_add_candidate(
    files: dict[Path, str], version: str
) -> dict[Path, str]:
    mutated = _clone_bundle_files(files)
    projection = _payload(mutated, observer_bundle.PROJECTION_REL)
    projection["paths"]["/sse/events"]["get"]["responses"]["200"]["x-chat-events"][
        "kinds"
    ].append("future_kind")
    _set_payload(mutated, observer_bundle.PROJECTION_REL, projection)
    manifest = _manifest(mutated)
    for vocabulary in manifest["vocabularies"]:
        if vocabulary["id"] == "root.chat.native_interest_kinds":
            vocabulary["native_client_interest_subset"].append("future_kind")
    _set_manifest(mutated, manifest)
    _set_bundle_version(mutated, version)
    return mutated


def _operation_removed_candidate(
    files: dict[Path, str], version: str
) -> dict[Path, str]:
    mutated = _clone_bundle_files(files)
    projection = _payload(mutated, observer_bundle.PROJECTION_REL)
    del projection["paths"]["/app/observer/register"]["post"]
    _set_payload(mutated, observer_bundle.PROJECTION_REL, projection)
    manifest = _manifest(mutated)
    manifest["operation_ids"].remove("observer.register")
    _set_manifest(mutated, manifest)
    _set_bundle_version(mutated, version)
    return mutated


def _semantic_status_fixture_candidate(
    files: dict[Path, str], version: str
) -> dict[Path, str]:
    mutated = _clone_bundle_files(files)
    fixtures = _payload(mutated, observer_bundle.FIXTURES_REL)
    for fixture in fixtures["fixtures"]:
        if fixture["id"] == "recorded.ingestUpload.ok":
            fixture["payload"]["status"] = "duplicate"
    _set_payload(mutated, observer_bundle.FIXTURES_REL, fixtures)
    vectors = _payload(mutated, observer_bundle.VECTORS_REL)
    for vector in vectors["vectors"]:
        if vector["id"] == "observer.ingestUpload.status.ok":
            vector["pointer_hashes"]["/status"] = _sha256_text(
                observer_bundle.render_json("duplicate")
            )
            vector["decision"] = {
                "accepted": True,
                "client_action": "adopt_existing_segment_without_reupload",
                "http_status": 200,
                "kind": "ingest_status",
                "status": "duplicate",
                "stored_key_precedence": ["existing_segment"],
                "stored_key_source": "existing_segment",
            }
            break
    else:
        raise AssertionError("missing observer.ingestUpload.status.ok vector")
    _set_payload(mutated, observer_bundle.VECTORS_REL, vectors)
    _set_bundle_version(mutated, version)
    return mutated


def test_observer_client_bundle_projection_closes_refs() -> None:
    projection = observer_bundle.build_projection_document()

    assert list(observer_bundle.OBSERVER_CLIENT_OPERATION_IDS) == EXPECTED_OPERATION_IDS
    assert _projection_operation_ids(projection) == EXPECTED_OPERATION_IDS
    assert sorted(projection["components"]["schemas"]) == EXPECTED_COMPONENTS
    observer_bundle.validate_projection_refs(projection)


def test_observer_client_bundle_preserves_distinct_version_concepts() -> None:
    source = observer_bundle.build_document()
    source["openapi"] = "3.1.9"
    source["info"]["version"] = "9.8.7"

    projection = observer_bundle.build_projection_document(source)
    manifest = observer_bundle.build_manifest_payload(
        Path.cwd(),
        projection,
        {
            observer_bundle.PROJECTION_REL: observer_bundle.render_json(projection),
            observer_bundle.FIXTURES_REL: "{}\n",
            observer_bundle.VECTORS_REL: "{}\n",
            observer_bundle.CONSUMER_AUDIT_REL: "{}\n",
        },
    )

    assert manifest["bundle_semver"] == observer_bundle.BUNDLE_SEMVER
    assert projection["info"]["version"] == "9.8.7"
    assert manifest["openapi_document_version"] == "9.8.7"
    assert manifest["openapi_spec_version"] == "3.1.9"
    assert {
        manifest["bundle_semver"],
        manifest["openapi_document_version"],
        manifest["openapi_spec_version"],
    } == {observer_bundle.BUNDLE_SEMVER, "9.8.7", "3.1.9"}


def test_observer_client_bundle_manifest_file_inventory(
    bundle_files: dict[Path, str],
) -> None:
    manifest = _json_file(bundle_files, observer_bundle.MANIFEST_REL)
    expected_payload_paths = [
        "consumer-audit.json",
        "fixtures/wire-behavior.json",
        "projection.openapi.json",
        "vectors.json",
    ]

    assert manifest["bundle_semver"] == observer_bundle.BUNDLE_SEMVER
    assert manifest["openapi_document_version"] == "1.0.0"
    assert manifest["openapi_spec_version"] == "3.1.0"
    assert manifest["observer_protocol_version"] == 2
    assert manifest["supported_response_variants"] == [1, 2]
    assert manifest["generator_identity"] == (
        "solstone.convey.contract.observer_bundle.v1"
    )
    assert manifest["bundle_schema_identity"] == (
        "solstone.observer-client-contract-bundle.schema.v1"
    )
    assert (
        manifest["schema_dialect_uri"] == "https://json-schema.org/draft/2020-12/schema"
    )
    assert manifest["operation_ids"] == EXPECTED_OPERATION_IDS
    assert manifest["consumer_identifiers"] == EXPECTED_CONSUMER_IDENTIFIERS
    assert [
        (item["id"], item["path"], item["role"])
        for item in manifest["generator_inputs"]
    ] == EXPECTED_GENERATOR_INPUTS
    for item in manifest["generator_inputs"]:
        assert not item["path"].startswith(
            observer_bundle.BUNDLE_REL_DIR.as_posix() + "/"
        )
    assert manifest["windows_linux_rollout_targets"] == [
        {
            "adoption_blocker_ids": [
                "linux.sol_voice.path",
                "linux.sol_voice.linux_notify_send",
            ],
            "consumer_identifier": "solstone-linux",
        },
        {"adoption_blocker_ids": [], "consumer_identifier": "solstone-windows"},
    ]

    assert [item["path"] for item in manifest["files"]] == expected_payload_paths
    inventory = {item["path"]: item["sha256"] for item in manifest["files"]}
    assert "manifest.json" not in [item["path"] for item in manifest["files"]]
    for rel_path, text in bundle_files.items():
        if rel_path == observer_bundle.MANIFEST_REL:
            continue
        path = rel_path.relative_to(observer_bundle.BUNDLE_REL_DIR).as_posix()
        assert inventory[path] == _sha256_text(text)

    fixtures = _json_file(bundle_files, observer_bundle.FIXTURES_REL)
    vectors = _json_file(bundle_files, observer_bundle.VECTORS_REL)
    assert [item["id"] for item in fixtures["fixtures"]] == EXPECTED_FIXTURE_IDS
    assert [item["id"] for item in vectors["vectors"]] == EXPECTED_VECTOR_IDS
    for vector in vectors["vectors"]:
        assert set(vector["pointer_hashes"]) == set(vector["pointers"])


def test_observer_client_bundle_pins_exact_generator_inputs(
    bundle_files: dict[Path, str],
) -> None:
    manifest = _json_file(bundle_files, observer_bundle.MANIFEST_REL)
    repo_root = observer_bundle._repo_root(None)
    fixture_tree = Path("tests/fixtures/journal")

    assert {item["id"] for item in manifest["generator_inputs"]} == (
        EXPECTED_GENERATOR_INPUT_IDS
    )
    assert len(manifest["generator_inputs"]) == 18
    for item in manifest["generator_inputs"]:
        rel_path = Path(item["path"])
        assert rel_path != fixture_tree
        assert fixture_tree not in rel_path.parents
        assert not (repo_root / rel_path).is_dir()


def test_observer_client_bundle_pins_non_manifest_payload_hashes(
    bundle_files: dict[Path, str],
) -> None:
    repo_root = observer_bundle._repo_root(None)

    for rel_path, expected_sha in EXPECTED_BUNDLE_PAYLOAD_SHA256.items():
        assert _sha256_text((repo_root / rel_path).read_text(encoding="utf-8")) == (
            expected_sha
        )
        assert _sha256_text(bundle_files[rel_path]) == expected_sha


def test_observer_client_bundle_old_fixture_tree_absence_and_residue_do_not_affect_outputs(
    tmp_path: Path,
) -> None:
    source_inputs = {
        "generator_inputs": [
            {"path": rel_path}
            for _input_id, rel_path, _role in EXPECTED_GENERATOR_INPUTS
        ]
    }
    clean_root = tmp_path / "clean-repo"
    residue_root = tmp_path / "residue-repo"
    _copy_manifest_source_inputs(clean_root, source_inputs)
    _copy_manifest_source_inputs(residue_root, source_inputs)
    assert not (clean_root / "tests" / "fixtures" / "journal").exists()

    residue_journal = residue_root / "tests" / "fixtures" / "journal"
    (residue_journal / "health" / "locks").mkdir(parents=True)
    (residue_journal / "health" / "locks" / "entity-trust.lock").touch()
    (residue_journal / "indexer").mkdir(parents=True)
    (residue_journal / "indexer" / "journal.sqlite").write_bytes(
        b"ignored sqlite residue"
    )

    # The recording path cannot observe this synthetic root: build_bundle_files(root)
    # uses root only for manifest generator-input hashing, while recording spins the
    # Flask app from installed code against a fresh temp journal.
    clean_files = observer_bundle.build_bundle_files(clean_root)
    residue_files = observer_bundle.build_bundle_files(residue_root)

    for rel_path, expected_sha in EXPECTED_BUNDLE_PAYLOAD_SHA256.items():
        assert _sha256_text(clean_files[rel_path]) == expected_sha
        assert _sha256_text(residue_files[rel_path]) == expected_sha

    clean_input_bytes = observer_bundle.render_json(
        _manifest(clean_files)["generator_inputs"]
    ).encode("utf-8")
    residue_input_bytes = observer_bundle.render_json(
        _manifest(residue_files)["generator_inputs"]
    ).encode("utf-8")
    assert residue_input_bytes == clean_input_bytes


def test_observer_client_bundle_prepare_recording_journal_starts_empty(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "journal"

    journal = observer_bundle_recording._prepare_recording_journal(destination)

    entries = list(journal.rglob("*"))
    assert {
        path.relative_to(journal).as_posix()
        for path in entries
        if path != journal / "config"
    } == {"config/journal.json"}
    assert json.loads(
        (journal / "config" / "journal.json").read_text(encoding="utf-8")
    ) == {"setup": {"completed_at": 1700000000000}}
    assert all(not path.is_symlink() for path in entries)
    assert journal == destination.resolve()

    with pytest.raises(FileExistsError):
        observer_bundle_recording._prepare_recording_journal(destination)


def test_observer_client_bundle_consumer_audit_snapshot(
    bundle_files: dict[Path, str],
) -> None:
    audit = _json_file(bundle_files, observer_bundle.CONSUMER_AUDIT_REL)

    assert audit["audited_commits"] == EXPECTED_AUDITED_COMMITS
    assert [
        (item["consumer"], item["revision"], item["path"])
        for item in sorted(
            audit["searched_files"],
            key=lambda item: (item["consumer"], item["path"]),
        )
    ] == EXPECTED_SEARCHED_FILES

    direct_paths = [
        (item["consumer"], item["path"], item["classification"])
        for item in audit["direct_paths"]
    ]
    assert direct_paths == EXPECTED_DIRECT_PATHS
    assert {item["consumer"] for item in audit["direct_paths"]} <= set(
        EXPECTED_CONSUMER_IDENTIFIERS
    )
    for item in audit["direct_paths"]:
        assert item["rationale"]
        assert item["classification"] != "catch_all_exclusion"

    findings = {item["id"]: item for item in audit["settings_drift_findings"]}
    assert set(findings) == {
        "linux.sol_voice.path",
        "linux.sol_voice.linux_notify_send",
    }
    assert findings["linux.sol_voice.path"] == {
        "consumer": "solstone-linux",
        "id": "linux.sol_voice.path",
        "rationale": (
            "Linux reads /api/sol_voice, but the journal route is "
            "/app/settings/api/sol_voice."
        ),
        "status": "adoption_blocker",
        "verified_citations": [
            "solstone/apps/settings/routes.py:86-89",
            "solstone/apps/settings/routes.py:640-641",
        ],
    }
    assert findings["linux.sol_voice.linux_notify_send"] == {
        "consumer": "solstone-linux",
        "id": "linux.sol_voice.linux_notify_send",
        "rationale": (
            "Linux reads top-level linux_notify_send, but the journal "
            "response exposes system_notifications.linux."
        ),
        "status": "adoption_blocker",
        "verified_citations": [
            "solstone/convey/sol_initiated/settings.py:41-42",
            "solstone/convey/sol_initiated/settings.py:61-63",
            "solstone/convey/sol_initiated/settings.py:211-213",
            "solstone/apps/settings/tests/test_sol_voice_routes.py:58",
        ],
    }


def test_observer_client_bundle_vocabularies_come_from_projection_extensions(
    bundle_files: dict[Path, str],
) -> None:
    manifest = _json_file(bundle_files, observer_bundle.MANIFEST_REL)
    vocabularies = {item["id"]: item for item in manifest["vocabularies"]}

    assert vocabularies["observer.ingestUpload.status"]["values"] == [
        "ok",
        "duplicate",
        "collision",
        "conflict",
        "failed",
    ]
    assert vocabularies["observer.ingestUpload.status"]["source_pointer"].startswith(
        "/paths/"
    )
    assert vocabularies["SegmentFile.status"]["values"] == [
        "present",
        "missing",
        "processed",
    ]
    assert vocabularies["SegmentFile.status"]["source_pointer"] == (
        "/components/schemas/SegmentFile/properties/status"
    )
    assert vocabularies["observer.callosumStream.sse_frames"]["values"] == [
        "data",
        "error",
        "heartbeat",
    ]
    assert vocabularies["callosum.rootEvents.sse_frames"]["values"] == [
        "data",
        "heartbeat",
    ]
    assert vocabularies["root.chat.native_interest_kinds"]["classification"] == (
        "extensible"
    )
    assert vocabularies["root.chat.native_interest_kinds"]["stream_exhaustive"] is False
    assert vocabularies["root.chat.native_interest_kinds"][
        "native_client_interest_subset"
    ]
    assert vocabularies["callosum.tract_event"]["classification"] == "extensible"
    assert vocabularies["callosum.tract_event"]["known_registry"]
    for vocabulary in vocabularies.values():
        assert "source_pointer" in vocabulary


def test_observer_client_bundle_vectors_carry_typed_decisions(
    bundle_files: dict[Path, str],
) -> None:
    vectors = _json_file(bundle_files, observer_bundle.VECTORS_REL)["vectors"]
    vectors_by_id = {vector["id"]: vector for vector in vectors}

    assert {vector["decision"]["kind"] for vector in vectors} == {
        "auth_header_form",
        "chat_open_request",
        "closed_vocabulary_unknown",
        "custody_status",
        "custody_unknown",
        "envelope_integrity",
        "ingest_status",
        "protocol_variant",
        "sse_frame",
        "submitted_name_fallback",
    }
    for vector in vectors:
        assert isinstance(vector["decision"], dict)
        assert vector["decision"]["kind"]
    assert [vector["id"] for vector in vectors] == EXPECTED_VECTOR_IDS
    assert list(EXPECTED_VECTOR_DECISIONS) == EXPECTED_VECTOR_IDS
    for vector_id, expected_decision in EXPECTED_VECTOR_DECISIONS.items():
        actual_decision = vectors_by_id[vector_id]["decision"]
        expected_bytes = observer_bundle.render_json(expected_decision).encode("utf-8")
        assert actual_decision == expected_decision
        assert (
            hashlib.sha256(expected_bytes).hexdigest()
            == EXPECTED_VECTOR_DECISION_SHA256[vector_id]
        )
        assert (
            hashlib.sha256(
                observer_bundle.render_json(actual_decision).encode("utf-8")
            ).hexdigest()
            == EXPECTED_VECTOR_DECISION_SHA256[vector_id]
        )


def test_observer_client_bundle_vector_pins_reject_self_consistent_generator_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vector_id = "observer.ingestUpload.status.ok"
    real_decision = observer_bundle_recording._decision_for_response_vector

    def wrong_decision(
        candidate_vector_id: str,
        payload: object,
        observed_status: int,
    ) -> dict[str, Any]:
        decision = real_decision(candidate_vector_id, payload, observed_status)
        if candidate_vector_id == vector_id:
            decision = dict(decision)
            decision["client_action"] = "coherently_wrong_generator_action"
        return decision

    monkeypatch.setattr(
        observer_bundle_recording,
        "_decision_for_response_vector",
        wrong_decision,
    )

    vectors = _json_file(
        observer_bundle.build_bundle_files(), observer_bundle.VECTORS_REL
    )["vectors"]
    vector = next(item for item in vectors if item["id"] == vector_id)
    generator_derived_oracle = {
        vector_id: vector["decision"],
    }

    assert vector["decision"] == generator_derived_oracle[vector_id]
    with pytest.raises(AssertionError):
        assert vector["decision"] == EXPECTED_VECTOR_DECISIONS[vector_id]
    with pytest.raises(AssertionError):
        assert (
            hashlib.sha256(
                observer_bundle.render_json(vector["decision"]).encode("utf-8")
            ).hexdigest()
            == EXPECTED_VECTOR_DECISION_SHA256[vector_id]
        )


@pytest.mark.parametrize(
    ("vector_id", "mutate"),
    [
        (
            "observer.ingestUpload.status.ok",
            lambda decision: decision.__setitem__("accepted", False),
        ),
        (
            "observer.ingestUpload.status.duplicate",
            lambda decision: decision.__setitem__("stored_key_source", "segment"),
        ),
        (
            "observer.ingestSegments.submitted_name_fallback",
            lambda decision: decision.__setitem__("fallback", "submitted_name"),
        ),
        (
            "observer.ingestSegments.custody_statuses",
            lambda decision: decision["holding_by_status"].__setitem__(
                "missing", "held"
            ),
        ),
        (
            "observer.auth.handle",
            lambda decision: decision.__setitem__("auth_form", "authorization_bearer"),
        ),
        (
            "observer.ingestSegments.legacy_array.absent_header",
            lambda decision: decision.__setitem__("response_variant", "v2_envelope"),
        ),
        (
            "observer.callosumStream.sse.error",
            lambda decision: decision.__setitem__("action", "ignore_keepalive"),
        ),
        (
            "callosum.rootEvents.sse.data_unknown_event",
            lambda decision: decision.__setitem__("unknown_event_behavior", "reject"),
        ),
        (
            "chat.openSolChatRequest.missing_required_field",
            lambda decision: decision.__setitem__("accepted", True),
        ),
        (
            "observer.ingestSegments.envelope_total_mismatch",
            lambda decision: decision.__setitem__("valid", True),
        ),
        (
            "observer.ingestSegments.custody_unknown_rejected",
            lambda decision: decision.__setitem__("unknown_status", "preserve"),
        ),
        (
            "observer.ingestUpload.status_unknown_rejected",
            lambda decision: decision.__setitem__("unknown_value_behavior", "preserve"),
        ),
    ],
)
def test_observer_client_bundle_rejects_vector_decision_mutations(
    bundle_files: dict[Path, str],
    tmp_path: Path,
    vector_id: str,
    mutate,
) -> None:
    mutated = _clone_bundle_files(bundle_files)
    vectors = _payload(mutated, observer_bundle.VECTORS_REL)
    for vector in vectors["vectors"]:
        if vector["id"] == vector_id:
            mutate(vector["decision"])
            break
    else:
        raise AssertionError(f"missing vector {vector_id}")
    _set_payload(mutated, observer_bundle.VECTORS_REL, vectors)

    with pytest.raises(observer_bundle.BundleVerificationError, match="decision"):
        observer_bundle_verification.verify_bundle_directory(
            _bundle_dir_with_files(tmp_path, mutated)
        )


def test_observer_client_bundle_render_is_deterministic_to_empty_roots(
    tmp_path: Path,
) -> None:
    roots = [tmp_path / "one", tmp_path / "two"]
    rendered_sets: list[dict[str, bytes]] = []
    for root in roots:
        root.mkdir()
        files = observer_bundle.build_bundle_files()
        for rel_path, text in files.items():
            output = root / rel_path.relative_to(observer_bundle.BUNDLE_REL_DIR)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
        rendered_sets.append(
            {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }
        )

    assert rendered_sets[0] == rendered_sets[1]


def test_observer_client_bundle_rejects_generator_input_inside_bundle(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    mutated = _clone_bundle_files(bundle_files)
    manifest = _manifest(mutated)
    manifest["generator_inputs"].append(
        {
            "id": "bad.generated_output",
            "path": "docs/openapi/observer-client-contract/vectors.json",
            "role": "generated_output",
            "sha256": "0" * 64,
        }
    )
    _set_manifest(mutated, manifest)

    bundle_dir = _bundle_dir_with_files(tmp_path, mutated)
    with pytest.raises(observer_bundle.BundleVerificationError, match="inside"):
        observer_bundle_verification.verify_bundle_directory(bundle_dir)


def test_observer_client_bundle_projected_branch_shapes(
    bundle_files: dict[Path, str],
) -> None:
    projection = _payload(bundle_files, observer_bundle.PROJECTION_REL)
    operation_locations = {
        operation["operationId"]: (path, method)
        for path, methods in projection["paths"].items()
        for method, operation in methods.items()
    }
    assert operation_locations["chat.openSolChatRequest"] == (
        "/api/chat/sol_chat_request/open",
        "post",
    )
    chat = projection["paths"]["/api/chat/sol_chat_request/open"]["post"]
    request_schema = chat["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["required"] == ["request_id"]
    assert request_schema["properties"]["request_id"] == {
        "minLength": 1,
        "pattern": "\\S",
        "type": "string",
    }
    success_schema = chat["responses"]["200"]["content"]["application/json"]["schema"]
    assert success_schema["required"] == ["ok"]
    assert success_schema["properties"]["ok"] == {"type": "boolean"}
    assert chat["responses"]["400"]["x-reason-codes"] == ["missing_required_field"]
    assert chat["responses"]["403"]["x-reason-codes"] == ["pl_revoked"]

    ingest_status = projection["paths"]["/app/observer/ingest"]["post"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]["properties"]["status"]
    assert ingest_status["enum"] == [
        "ok",
        "duplicate",
        "collision",
        "conflict",
        "failed",
    ]
    assert ingest_status["x-vocabulary"]["unknown_value_behavior"] == "reject"
    segment_status = projection["components"]["schemas"]["SegmentFile"]["properties"][
        "status"
    ]
    assert segment_status["enum"] == ["present", "missing", "processed"]
    assert segment_status["x-vocabulary"]["unknown_value_behavior"] == "reject"
    protocol = projection["paths"]["/app/observer/ingest/segments/{day}"]["get"][
        "responses"
    ]["200"]["x-vocabularies"]["X-Solstone-Protocol-Version"]
    assert protocol["absent_or_unparseable"] == 1
    assert protocol["current"] == 2
    root_chat = projection["paths"]["/sse/events"]["get"]["responses"]["200"][
        "x-chat-events"
    ]
    assert root_chat["classification"] == "extensible"
    assert root_chat["unknown_value_behavior"] == "preserve"


@pytest.mark.parametrize(
    ("name", "mutator", "match"),
    [
        (
            "operation_removed",
            lambda projection: projection["paths"][
                "/api/chat/sol_chat_request/open"
            ].pop("post"),
            "operation IDs",
        ),
        (
            "operation_renamed",
            lambda projection: projection["paths"]["/api/chat/sol_chat_request/open"][
                "post"
            ].__setitem__("operationId", "chat.renamed"),
            "operation IDs",
        ),
        (
            "chat_request_required_removed",
            lambda projection: projection["paths"]["/api/chat/sol_chat_request/open"][
                "post"
            ]["requestBody"]["content"]["application/json"]["schema"].__setitem__(
                "required", []
            ),
            "request_id",
        ),
        (
            "chat_success_type_changed",
            lambda projection: projection["paths"]["/api/chat/sol_chat_request/open"][
                "post"
            ]["responses"]["200"]["content"]["application/json"]["schema"][
                "properties"
            ]["ok"].__setitem__("type", "string"),
            "ok must be boolean",
        ),
        (
            "chat_missing_field_reason_changed",
            lambda projection: projection["paths"]["/api/chat/sol_chat_request/open"][
                "post"
            ]["responses"]["400"].__setitem__("x-reason-codes", ["bad"]),
            "missing_required_field",
        ),
        (
            "ingest_status_removed",
            lambda projection: projection["paths"]["/app/observer/ingest"]["post"][
                "responses"
            ]["200"]["content"]["application/json"]["schema"]["properties"][
                "status"
            ].__setitem__("enum", ["ok", "duplicate", "collision", "conflict"]),
            "status enum",
        ),
        (
            "custody_status_removed",
            lambda projection: projection["components"]["schemas"]["SegmentFile"][
                "properties"
            ]["status"].__setitem__("enum", ["present", "missing"]),
            "SegmentFile.status",
        ),
        (
            "closed_unknown_behavior_changed",
            lambda projection: projection["paths"]["/app/observer/ingest"]["post"][
                "responses"
            ]["200"]["content"]["application/json"]["schema"]["properties"]["status"][
                "x-vocabulary"
            ].__setitem__("unknown_value_behavior", "preserve"),
            "unknown-value",
        ),
        (
            "extensible_unknown_behavior_changed",
            lambda projection: projection["paths"]["/sse/events"]["get"]["responses"][
                "200"
            ]["x-chat-events"].__setitem__("unknown_value_behavior", "reject"),
            "preserve unknown",
        ),
        (
            "protocol_current_changed",
            lambda projection: projection["paths"][
                "/app/observer/ingest/segments/{day}"
            ]["get"]["responses"]["200"]["x-vocabularies"][
                "X-Solstone-Protocol-Version"
            ].__setitem__("current", 1),
            "Protocol-Version",
        ),
        (
            "dangling_ref",
            lambda projection: projection["paths"]["/api/chat/sol_chat_request/open"][
                "post"
            ]["responses"]["200"]["content"]["application/json"].__setitem__(
                "schema", {"$ref": "#/components/schemas/Missing"}
            ),
            "dangling",
        ),
    ],
)
def test_observer_client_bundle_shape_mutations_fail_verification(
    bundle_files: dict[Path, str],
    tmp_path: Path,
    name: str,
    mutator,
    match: str,
) -> None:
    mutated = _clone_bundle_files(bundle_files)
    projection = _payload(mutated, observer_bundle.PROJECTION_REL)
    mutator(projection)
    _set_payload(mutated, observer_bundle.PROJECTION_REL, projection)

    bundle_dir = _bundle_dir_with_files(tmp_path / name, mutated)
    with pytest.raises(observer_bundle.BundleVerificationError, match=match):
        observer_bundle_verification.verify_bundle_directory(bundle_dir)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "",
        ".",
        "..",
        "payload.",
        "payload ",
        "CON",
        "CON.txt",
        "/absolute.json",
        "C:payload.json",
        "bad:name.json",
        "bad*name.json",
        "dir\\file.json",
        "bad\nfile.json",
        "dir//file.json",
        "dir/../file.json",
    ],
)
def test_observer_client_bundle_rejects_unsafe_manifest_paths(
    bundle_files: dict[Path, str],
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    mutated = _copy_manifest_with_file_path(bundle_files, unsafe_path)
    bundle_dir = _bundle_dir_with_files(tmp_path, mutated)
    with pytest.raises(observer_bundle.BundleVerificationError):
        observer_bundle_verification.verify_bundle_directory(bundle_dir)


def test_observer_client_bundle_rejects_duplicate_and_case_colliding_paths(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    for replacement in ("consumer-audit.json", "Consumer-Audit.json"):
        mutated = _clone_bundle_files(bundle_files)
        manifest = _manifest(mutated)
        manifest["files"][1] = {**manifest["files"][1], "path": replacement}
        _set_manifest(mutated, manifest)
        bundle_dir = _bundle_dir_with_files(tmp_path / replacement, mutated)
        with pytest.raises(observer_bundle.BundleVerificationError):
            observer_bundle_verification.verify_bundle_directory(bundle_dir)


def test_observer_client_bundle_rejects_symlink_nonregular_and_unlisted_files(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    for name, setup in (
        (
            "symlink",
            lambda bundle_dir: (bundle_dir / "link.json").symlink_to("manifest.json"),
        ),
        ("fifo", lambda bundle_dir: os.mkfifo(bundle_dir / "pipe.json")),
        (
            "unlisted",
            lambda bundle_dir: (bundle_dir / "extra.json").write_text(
                "{}\n",
                encoding="utf-8",
            ),
        ),
    ):
        bundle_dir = _bundle_dir_with_files(tmp_path / name, bundle_files)
        setup(bundle_dir)
        with pytest.raises(observer_bundle.BundleVerificationError):
            observer_bundle_verification.verify_bundle_directory(bundle_dir)


def test_observer_client_bundle_rejects_manifest_digest_and_fixture_byte_drift(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    digest_mutated = _clone_bundle_files(bundle_files)
    manifest = _manifest(digest_mutated)
    manifest["files"][0]["sha256"] = "0" * 64
    _set_manifest(digest_mutated, manifest)
    with pytest.raises(observer_bundle.BundleVerificationError, match="digest"):
        observer_bundle_verification.verify_bundle_directory(
            _bundle_dir_with_files(tmp_path / "digest", digest_mutated)
        )

    fixture_mutated = _clone_bundle_files(bundle_files)
    fixtures = _payload(fixture_mutated, observer_bundle.FIXTURES_REL)
    for fixture in fixtures["fixtures"]:
        if fixture["id"] == "recorded.ingestUpload.ok":
            fixture["payload"]["status"] = "duplicate"
            break
    else:
        raise AssertionError("missing recorded.ingestUpload.ok fixture")
    _set_payload(fixture_mutated, observer_bundle.FIXTURES_REL, fixtures)
    with pytest.raises(observer_bundle.BundleVerificationError, match="hash mismatch"):
        observer_bundle_verification.verify_bundle_directory(
            _bundle_dir_with_files(tmp_path / "fixture", fixture_mutated)
        )


@pytest.mark.parametrize(
    ("rel_path", "array_name", "match"),
    [
        (
            observer_bundle.FIXTURES_REL,
            "fixtures",
            "fixture IDs are not sorted",
        ),
        (observer_bundle.VECTORS_REL, "vectors", "vector IDs are not sorted"),
    ],
)
def test_observer_client_bundle_rejects_unsorted_fixture_and_vector_ids(
    bundle_files: dict[Path, str],
    tmp_path: Path,
    rel_path: Path,
    array_name: str,
    match: str,
) -> None:
    mutated = _clone_bundle_files(bundle_files)
    payload = _payload(mutated, rel_path)
    payload[array_name] = list(reversed(payload[array_name]))
    _set_payload(mutated, rel_path, payload)

    with pytest.raises(observer_bundle.BundleVerificationError, match=match):
        observer_bundle_verification.verify_bundle_directory(
            _bundle_dir_with_files(tmp_path / array_name, mutated)
        )


def test_observer_client_bundle_rejects_generator_input_drift(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    for name, mutate in (
        (
            "digest",
            lambda manifest: manifest["generator_inputs"][0].__setitem__(
                "sha256", "0" * 64
            ),
        ),
        ("missing", lambda manifest: manifest["generator_inputs"].pop(0)),
        (
            "extra",
            lambda manifest: manifest["generator_inputs"].append(
                {
                    "id": "extra.input",
                    "path": "README.md",
                    "role": "producer",
                    "sha256": "0" * 64,
                }
            ),
        ),
        (
            "renamed",
            lambda manifest: manifest["generator_inputs"][0].__setitem__(
                "id", "renamed.input"
            ),
        ),
    ):
        mutated = _clone_bundle_files(bundle_files)
        manifest = _manifest(mutated)
        mutate(manifest)
        _set_manifest(mutated, manifest)
        bundle_dir = _bundle_dir_with_files(tmp_path / name, mutated)
        _copy_manifest_source_inputs(bundle_dir.parents[2], manifest)
        with pytest.raises(observer_bundle.BundleVerificationError):
            observer_bundle_verification.verify_committed_bundle(bundle_dir.parents[2])


def test_observer_client_bundle_rejects_duplicate_generator_input_record(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    mutated = _clone_bundle_files(bundle_files)
    manifest = _manifest(mutated)
    manifest["generator_inputs"].insert(1, dict(manifest["generator_inputs"][0]))
    _set_manifest(mutated, manifest)
    bundle_dir = _bundle_dir_with_files(tmp_path, mutated)
    _copy_manifest_source_inputs(bundle_dir.parents[2], manifest)

    with pytest.raises(
        observer_bundle.BundleVerificationError,
        match="duplicate generator input record",
    ):
        observer_bundle_verification.verify_committed_bundle(bundle_dir.parents[2])


def test_observer_client_bundle_rejects_unsorted_generator_inputs(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    mutated = _clone_bundle_files(bundle_files)
    manifest = _manifest(mutated)
    manifest["generator_inputs"] = list(reversed(manifest["generator_inputs"]))
    _set_manifest(mutated, manifest)
    bundle_dir = _bundle_dir_with_files(tmp_path, mutated)
    _copy_manifest_source_inputs(bundle_dir.parents[2], manifest)

    with pytest.raises(
        observer_bundle.BundleVerificationError,
        match="generator_inputs is not sorted",
    ):
        observer_bundle_verification.verify_committed_bundle(bundle_dir.parents[2])


@pytest.mark.parametrize(
    "input_id",
    [item[0] for item in EXPECTED_GENERATOR_INPUTS],
)
def test_observer_client_bundle_rejects_each_generator_input_digest_drift(
    bundle_files: dict[Path, str],
    tmp_path: Path,
    input_id: str,
) -> None:
    mutated = _clone_bundle_files(bundle_files)
    manifest = _manifest(mutated)
    for item in manifest["generator_inputs"]:
        if item["id"] == input_id:
            item["sha256"] = "0" * 64
            break
    else:
        raise AssertionError(f"missing generator input {input_id}")
    _set_manifest(mutated, manifest)
    bundle_dir = _bundle_dir_with_files(tmp_path, mutated)
    _copy_manifest_source_inputs(bundle_dir.parents[2], manifest)

    with pytest.raises(observer_bundle.BundleVerificationError, match=input_id):
        observer_bundle_verification.verify_committed_bundle(bundle_dir.parents[2])


@pytest.mark.parametrize(
    ("role", "input_id", "rel_path"),
    _generator_input_class_cases(),
)
def test_observer_client_bundle_rejects_each_generator_input_class_source_mutation(
    bundle_files: dict[Path, str],
    tmp_path: Path,
    role: str,
    input_id: str,
    rel_path: str,
) -> None:
    mutated = _clone_bundle_files(bundle_files)
    manifest = _manifest(mutated)
    bundle_dir = _bundle_dir_with_files(tmp_path / role, mutated)
    repo = bundle_dir.parents[2]
    _copy_manifest_source_inputs(repo, manifest)

    source_path = repo / rel_path
    with source_path.open("ab") as handle:
        handle.write(f"\n# observer bundle source mutation: {role}\n".encode())

    with pytest.raises(observer_bundle.BundleVerificationError, match=input_id):
        observer_bundle_verification.verify_committed_bundle(repo)


def test_observer_client_bundle_tree_digest_does_not_follow_symlinked_dirs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    external_file = tmp_path / "external-file.txt"
    external_file.write_text("file v1\n", encoding="utf-8")
    external_dir = tmp_path / "external-dir"
    external_dir.mkdir()
    (external_dir / "payload.txt").write_text("dir v1\n", encoding="utf-8")
    retargeted_dir = tmp_path / "retargeted-dir"
    retargeted_dir.mkdir()
    (retargeted_dir / "payload.txt").write_text("other dir\n", encoding="utf-8")

    (root / "regular.txt").write_text("root\n", encoding="utf-8")
    linked_dir = root / "linked-dir"
    linked_file = root / "linked-file"
    linked_dir.symlink_to(external_dir, target_is_directory=True)
    linked_file.symlink_to(external_file)

    digest = observer_bundle._sha256_path(root)

    external_file.write_text("file v2\n", encoding="utf-8")
    (external_dir / "payload.txt").write_text("dir v2\n", encoding="utf-8")
    (external_dir / "added.txt").write_text("added\n", encoding="utf-8")
    assert observer_bundle._sha256_path(root) == digest

    linked_file.unlink()
    linked_file.symlink_to(tmp_path / "other-external-file.txt")
    assert observer_bundle._sha256_path(root) != digest
    linked_file.unlink()
    linked_file.symlink_to(external_file)

    linked_dir.unlink()
    linked_dir.symlink_to(retargeted_dir, target_is_directory=True)
    assert observer_bundle._sha256_path(root) != digest


def test_observer_client_bundle_tree_digest_rejects_symlink_ancestor(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    (real_parent / "input.txt").write_text("real\n", encoding="utf-8")
    link_parent = tmp_path / "link-parent"
    link_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(observer_bundle.ObserverBundleError):
        observer_bundle._sha256_path(link_parent / "input.txt")


def test_observer_client_bundle_tree_digest_rejects_directory_entry_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "input.txt").write_text("input\n", encoding="utf-8")
    real_read = observer_bundle._read_regular_at_no_follow
    mutated = False

    def read_and_mutate(parent_fd: int, name: bytes, expected_stat: os.stat_result):
        nonlocal mutated
        if not mutated:
            (root / "added.txt").write_text("added\n", encoding="utf-8")
            mutated = True
        return real_read(parent_fd, name, expected_stat)

    monkeypatch.setattr(
        observer_bundle,
        "_read_regular_at_no_follow",
        read_and_mutate,
    )

    with pytest.raises(observer_bundle.ObserverBundleError, match="entries changed"):
        observer_bundle._sha256_path(root)
    assert mutated is True


def test_observer_client_bundle_tree_digest_rejects_symlink_retarget_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    link = root / "link.txt"
    link.symlink_to(first)
    real_readlink = observer_bundle.os.readlink
    retargeted = False

    def readlink_and_retarget(name, *args, **kwargs):
        nonlocal retargeted
        if os.fsdecode(name) == "link.txt" and not retargeted:
            link.unlink()
            link.symlink_to(second)
            retargeted = True
        return real_readlink(name, *args, **kwargs)

    monkeypatch.setattr(observer_bundle.os, "readlink", readlink_and_retarget)

    with pytest.raises(observer_bundle.ObserverBundleError, match="symlink changed"):
        observer_bundle._sha256_path(root)
    assert retargeted is True


def test_observer_client_bundle_generator_inputs_digest_root_symlink_no_follow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    bundle_dir = root / observer_bundle.BUNDLE_REL_DIR
    bundle_dir.mkdir(parents=True)
    external = tmp_path / "external.txt"
    external.write_text("external v1\n", encoding="utf-8")
    link = root / "declared-input"
    link.symlink_to(external)
    monkeypatch.setattr(
        observer_bundle,
        "_SOURCE_INPUTS",
        (("test.root_symlink", Path("declared-input"), "test"),),
    )

    first = observer_bundle._generator_inputs(root)[0]["sha256"]
    external.write_text("external v2\n", encoding="utf-8")
    assert observer_bundle._generator_inputs(root)[0]["sha256"] == first

    link.unlink()
    internal = bundle_dir / "manifest.json"
    internal.write_text("{}\n", encoding="utf-8")
    link.symlink_to(internal)
    with pytest.raises(observer_bundle.ObserverBundleError, match="inside"):
        observer_bundle._generator_inputs(root)


def test_observer_client_bundle_file_digest_rejects_symlink_swap_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    victim = tmp_path / "declared-input.txt"
    victim.write_text("safe\n", encoding="utf-8")
    external = tmp_path / "external.txt"
    external.write_text("external\n", encoding="utf-8")
    real_open = observer_bundle.os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        path_name = os.fsdecode(path)
        if (
            path_name == victim.name
            and kwargs.get("dir_fd") is not None
            and not swapped
        ):
            victim.unlink()
            victim.symlink_to(external)
            swapped = True
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(observer_bundle.os, "open", swapping_open)

    with pytest.raises(observer_bundle.ObserverBundleError):
        observer_bundle._sha256_path(victim)
    assert swapped is True


def test_observer_client_bundle_verification_rejects_raced_symlink_payload(
    bundle_files: dict[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_dir = _bundle_dir_with_files(tmp_path, bundle_files)
    external_vectors = tmp_path / "external-vectors.json"
    external_vectors.write_bytes((bundle_dir / "vectors.json").read_bytes())
    real_read = observer_bundle_verification._read_regular_at_no_follow
    swapped = False

    def read_and_swap(parent_fd: int, name: bytes, expected_stat: os.stat_result):
        nonlocal swapped
        if os.fsdecode(name) == "vectors.json" and not swapped:
            vectors = bundle_dir / "vectors.json"
            vectors.unlink()
            vectors.symlink_to(external_vectors)
            swapped = True
        return real_read(parent_fd, name, expected_stat)

    monkeypatch.setattr(
        observer_bundle_verification,
        "_read_regular_at_no_follow",
        read_and_swap,
    )

    with pytest.raises(observer_bundle.ObserverBundleError):
        observer_bundle_verification.verify_bundle_directory(bundle_dir)
    assert swapped is True


def test_observer_client_bundle_publish_success_and_destination_refusals(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = _write_bundle_tree(repo, bundle_files)
    destination = repo / "published"

    assert (
        observer_bundle_export.publish_bundle_directory(source, destination, repo)
        == destination
    )
    assert sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    ) == [
        "consumer-audit.json",
        "fixtures/wire-behavior.json",
        "manifest.json",
        "projection.openapi.json",
        "vectors.json",
    ]

    with pytest.raises(
        observer_bundle_export.BundleExportRefused, match="already exists"
    ):
        observer_bundle_export.publish_bundle_directory(source, destination, repo)

    external = tmp_path / "external"
    assert (
        observer_bundle_export.publish_bundle_directory(source, external, repo)
        == external
    )


@pytest.mark.parametrize(
    "destination",
    [
        "C:bundle",
        "bad:name",
        "bad*name",
        "bad?name",
        "CON",
        "bundle.",
        "bundle ",
    ],
)
def test_observer_client_bundle_publish_rejects_unsafe_destination_text(
    bundle_files: dict[Path, str],
    tmp_path: Path,
    destination: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = _write_bundle_tree(repo, bundle_files)

    with pytest.raises(observer_bundle_export.BundleExportRefused):
        observer_bundle_export.publish_bundle_directory(source, Path(destination), repo)


def test_observer_client_bundle_publish_rejects_broken_destination_symlink(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = _write_bundle_tree(repo, bundle_files)
    destination = repo / "broken"
    destination.symlink_to("missing-target")

    with pytest.raises(
        observer_bundle_export.BundleExportRefused, match="already exists"
    ):
        observer_bundle_export.publish_bundle_directory(source, destination, repo)
    assert destination.is_symlink()


def test_observer_client_bundle_publish_rejects_nested_destination_symlink(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = _write_bundle_tree(repo, bundle_files)
    real_parent = repo / "real-parent"
    real_parent.mkdir()
    link_parent = repo / "link-parent"
    link_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(observer_bundle_export.BundleExportRefused, match="symlink"):
        observer_bundle_export.publish_bundle_directory(
            source,
            link_parent / "published",
            repo,
        )


def test_observer_client_bundle_publish_refuses_destination_appearing_at_rename(
    bundle_files: dict[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = _write_bundle_tree(repo, bundle_files)
    destination = repo / "published"
    preexisting = repo / "preexisting.txt"
    preexisting.write_text("original\n", encoding="utf-8")
    real_verify = observer_bundle_export._verify_bundle_stage
    raced_stat: os.stat_result | None = None

    def verify_and_create_destination(stage_dir: Path):
        nonlocal raced_stat
        destination.mkdir()
        (destination / "kept.txt").write_text("kept\n", encoding="utf-8")
        raced_stat = destination.stat()
        return real_verify(stage_dir)

    monkeypatch.setattr(
        observer_bundle_export,
        "_verify_bundle_stage",
        verify_and_create_destination,
    )
    with pytest.raises(observer_bundle_export.BundleExportRefused, match="appeared"):
        observer_bundle_export.publish_bundle_directory(source, destination, repo)

    assert destination.is_dir()
    assert raced_stat is not None
    current_stat = destination.stat()
    assert (current_stat.st_dev, current_stat.st_ino) == (
        raced_stat.st_dev,
        raced_stat.st_ino,
    )
    assert (destination / "kept.txt").read_text(encoding="utf-8") == "kept\n"
    assert preexisting.read_text(encoding="utf-8") == "original\n"
    assert not [
        path for path in repo.iterdir() if path.name.startswith(".published.staging.")
    ]


def test_observer_client_bundle_publish_rejects_parent_retarget_before_open(
    bundle_files: dict[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = _write_bundle_tree(repo, bundle_files)
    original_parent = repo / "parent"
    original_parent.mkdir()
    outside_parent = repo / "outside"
    outside_parent.mkdir()
    destination = original_parent / "published"
    real_open_parent = observer_bundle_export._open_destination_parent
    retargeted_parent = repo / "parent-original"

    def retarget_before_open(target: Path):
        original_parent.rename(retargeted_parent)
        original_parent.symlink_to(outside_parent, target_is_directory=True)
        return real_open_parent(target)

    monkeypatch.setattr(
        observer_bundle_export,
        "_open_destination_parent",
        retarget_before_open,
    )

    with pytest.raises(observer_bundle_export.BundleExportRefused):
        observer_bundle_export.publish_bundle_directory(source, destination, repo)

    assert not (outside_parent / "published").exists()
    assert not (retargeted_parent / "published").exists()


def test_observer_client_bundle_publish_cleanup_uses_original_parent_fd(
    bundle_files: dict[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = _write_bundle_tree(repo, bundle_files)
    original_parent = repo / "parent"
    original_parent.mkdir()
    outside_parent = repo / "outside"
    outside_parent.mkdir()
    destination = original_parent / "published"
    real_create_stage = observer_bundle_export._create_stage_dir
    stage_name: bytes | None = None

    def capture_stage(target: Path, parent_fd: int):
        nonlocal stage_name
        stage_name, stage_fd, stage_stat = real_create_stage(target, parent_fd)
        return stage_name, stage_fd, stage_stat

    def retarget_and_fail(_stage_fd: int):
        assert stage_name is not None
        saved_parent = repo / "parent-original"
        original_parent.rename(saved_parent)
        original_parent.symlink_to(outside_parent, target_is_directory=True)
        foreign_stage = outside_parent / os.fsdecode(stage_name)
        foreign_stage.mkdir()
        (foreign_stage / "foreign.txt").write_text("keep\n", encoding="utf-8")
        raise RuntimeError("retargeted stage failure")

    monkeypatch.setattr(observer_bundle_export, "_create_stage_dir", capture_stage)
    monkeypatch.setattr(
        observer_bundle_export, "_verify_bundle_stage", retarget_and_fail
    )

    with pytest.raises(RuntimeError, match="retargeted stage failure"):
        observer_bundle_export.publish_bundle_directory(source, destination, repo)

    assert stage_name is not None
    saved_parent = repo / "parent-original"
    assert not (saved_parent / os.fsdecode(stage_name)).exists()
    foreign_stage = outside_parent / os.fsdecode(stage_name)
    assert foreign_stage.is_dir()
    assert (foreign_stage / "foreign.txt").read_text(encoding="utf-8") == "keep\n"
    assert not (outside_parent / "published").exists()


@pytest.mark.parametrize(
    "seam_name",
    [
        "_populate_bundle_stage",
        "_verify_bundle_stage",
        "_finalize_bundle_publish",
    ],
)
def test_observer_client_bundle_publish_failure_injection_preserves_state(
    bundle_files: dict[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seam_name: str,
) -> None:
    repo = tmp_path / seam_name
    repo.mkdir()
    source = _write_bundle_tree(repo, bundle_files)
    destination = repo / "published"
    preexisting = repo / "preexisting.txt"
    preexisting.write_text("original\n", encoding="utf-8")

    def fail(*_args, **_kwargs):
        raise RuntimeError(seam_name)

    monkeypatch.setattr(observer_bundle_export, seam_name, fail)
    with pytest.raises(RuntimeError, match=seam_name):
        observer_bundle_export.publish_bundle_directory(source, destination, repo)

    assert not destination.exists()
    assert preexisting.read_text(encoding="utf-8") == "original\n"
    assert not [
        path for path in repo.iterdir() if path.name.startswith(".published.staging.")
    ]


def test_observer_client_bundle_history_genuine_first_bundle_requires_1_0_0(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    initial = _clone_bundle_files(bundle_files)
    _set_bundle_version(initial, "1.0.0")

    assert observer_bundle_compatibility.check_bundle_compatibility(repo, initial) == []
    bumped = _clone_bundle_files(initial)
    _set_bundle_version(bumped, "1.0.1")
    _assert_history_failure(repo, bumped, "genuine first bundle")


def test_observer_client_bundle_history_clean_first_bundle_at_1_0_1_fails(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    bumped = _clone_bundle_files(bundle_files)
    _set_bundle_version(bumped, "1.0.1")
    _commit_bundle(repo, bumped, "first bundle")

    _assert_history_failure(repo, bumped, "genuine first bundle")


def test_observer_client_bundle_history_identical_same_version_passes_dirty_and_clean(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    initial = _clone_bundle_files(bundle_files)
    _set_bundle_version(initial, "1.0.0")
    _commit_bundle(repo, initial, "initial bundle")
    _commit_bundle(repo, bundle_files, "bundle")

    assert (
        observer_bundle_compatibility.check_bundle_compatibility(repo, bundle_files)
        == []
    )
    (repo / observer_bundle.MANIFEST_REL).write_text("dirty\n", encoding="utf-8")
    assert (
        observer_bundle_compatibility.check_bundle_compatibility(repo, bundle_files)
        == []
    )


def test_observer_client_bundle_history_unrelated_identical_commit_passes_same_version(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    initial = _clone_bundle_files(bundle_files)
    _set_bundle_version(initial, "1.0.0")
    _commit_bundle(repo, initial, "initial bundle")
    _commit_bundle(repo, bundle_files, "bundle")
    (repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")
    _git(repo, "commit", "-m", "unrelated")

    assert (
        observer_bundle_compatibility.check_bundle_compatibility(repo, bundle_files)
        == []
    )


def test_observer_client_bundle_history_clean_tree_compares_nearest_distinct(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    initial = _clone_bundle_files(bundle_files)
    _set_bundle_version(initial, "1.0.0")
    _commit_bundle(repo, initial, "initial bundle")
    candidate = _description_patch_candidate(bundle_files, "1.0.1")
    _commit_bundle(repo, candidate, "patch bundle")

    assert (
        observer_bundle_compatibility.check_bundle_compatibility(repo, candidate) == []
    )


def test_observer_client_bundle_history_missing_baseline_fails_closed(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    corrupt = _clone_bundle_files(bundle_files)
    del corrupt[observer_bundle.PROJECTION_REL]
    _commit_bundle(repo, corrupt, "corrupt bundle")

    _assert_history_failure(repo, bundle_files, "historical bundle")


def test_observer_client_bundle_history_payload_only_corruption_fails_closed(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit_bundle(repo, bundle_files, "valid bundle")
    projection_path = repo / observer_bundle.PROJECTION_REL
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    projection["info"]["description"] = "corrupt without manifest digest update"
    projection_path.write_text(
        observer_bundle.render_json(projection), encoding="utf-8"
    )
    _git(repo, "add", observer_bundle.PROJECTION_REL.as_posix())
    _git(repo, "commit", "-m", "payload corruption")

    _assert_history_failure(repo, bundle_files, "corrupt")


@pytest.mark.parametrize("mutation", ["delete", "corrupt"])
def test_observer_client_bundle_history_corrupt_object_database_fails_closed(
    bundle_files: dict[Path, str],
    tmp_path: Path,
    mutation: str,
) -> None:
    repo = tmp_path / mutation
    _init_git_repo(repo)
    _commit_bundle(repo, bundle_files, "bundle")
    object_id = _git_output(repo, "rev-parse", f"HEAD:{observer_bundle.PROJECTION_REL}")
    object_path = _loose_object_path(repo, object_id)
    if mutation == "delete":
        object_path.unlink()
    else:
        object_path.chmod(0o600)
        object_path.write_bytes(b"corrupt object bytes\n")

    failures = observer_bundle_compatibility.check_bundle_compatibility(
        repo,
        bundle_files,
    )

    assert failures
    assert "corrupt or incomplete" in failures[0]
    assert "genuine first bundle" not in failures[0]


def test_observer_client_bundle_history_shallow_repository_fails(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _init_git_repo(source)
    _commit_bundle(source, bundle_files, "bundle")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth=1", f"file://{source}", str(shallow)],
        check=True,
        capture_output=True,
    )

    _assert_history_failure(shallow, bundle_files, "shallow")


def test_observer_client_bundle_history_equal_and_downgrade_failures(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    equal_repo = tmp_path / "equal"
    _init_git_repo(equal_repo)
    _commit_bundle(equal_repo, bundle_files, "bundle")
    _assert_history_failure(
        equal_repo,
        _description_patch_candidate(bundle_files, observer_bundle.BUNDLE_SEMVER),
        "without a version bump",
    )

    downgrade_repo = tmp_path / "downgrade"
    _init_git_repo(downgrade_repo)
    previous = _clone_bundle_files(bundle_files)
    _set_bundle_version(previous, _next_minor_version())
    _commit_bundle(downgrade_repo, previous, "minor bundle")
    _assert_history_failure(downgrade_repo, bundle_files, "downgraded")


@pytest.mark.parametrize(
    ("version", "expected_failure"),
    [
        ("1.0.1", "without a version bump"),
        ("2.0.0", None),
    ],
)
def test_observer_client_bundle_history_fixture_input_removal_requires_patch_bump(
    bundle_files: dict[Path, str],
    tmp_path: Path,
    version: str,
    expected_failure: str | None,
) -> None:
    source_root = observer_bundle._repo_root(None)
    baseline_revision = "d0c7318f3"
    baseline = {}
    for rel_path in sorted(bundle_files):
        baseline[rel_path] = subprocess.run(
            ["git", "show", f"{baseline_revision}:{rel_path.as_posix()}"],
            cwd=source_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    repo = tmp_path / version
    _init_git_repo(repo)
    _commit_bundle(repo, baseline, "1.0.1 bundle")
    candidate = _clone_bundle_files(bundle_files)
    _set_bundle_version(candidate, version)

    failures = observer_bundle_compatibility.check_bundle_compatibility(repo, candidate)

    if expected_failure is None:
        assert failures == []
    else:
        assert failures
        assert expected_failure in failures[0]


def test_observer_client_bundle_history_major_and_mixed_insufficient_bumps_fail(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    closed_repo = tmp_path / "closed"
    _init_git_repo(closed_repo)
    _commit_bundle(closed_repo, bundle_files, "bundle")
    _assert_history_failure(
        closed_repo,
        _closed_status_add_candidate(bundle_files, _next_minor_version()),
        "major change",
        enforce_current_contract=False,
    )

    mixed_repo = tmp_path / "mixed"
    _init_git_repo(mixed_repo)
    _commit_bundle(mixed_repo, bundle_files, "bundle")
    mixed = _operation_removed_candidate(bundle_files, _next_minor_version())
    projection = _payload(mixed, observer_bundle.PROJECTION_REL)
    projection["paths"]["/app/observer/ingest"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["properties"]["additive_field"] = {"type": "string"}
    _set_payload(mixed, observer_bundle.PROJECTION_REL, projection)
    _assert_history_failure(
        mixed_repo,
        mixed,
        "major change",
        enforce_current_contract=False,
    )


def test_observer_client_bundle_history_extensible_addition_accepts_minor(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit_bundle(repo, bundle_files, "bundle")

    assert (
        observer_bundle_compatibility.check_bundle_compatibility(
            repo,
            _extensible_chat_add_candidate(bundle_files, _next_minor_version()),
            enforce_current_contract=False,
        )
        == []
    )


def test_observer_client_bundle_history_vector_removed_requires_major(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit_bundle(repo, bundle_files, "bundle")
    removed = _clone_bundle_files(bundle_files)
    vectors = _payload(removed, observer_bundle.VECTORS_REL)
    vectors["vectors"] = [
        vector
        for vector in vectors["vectors"]
        if vector["id"] != "observer.auth.handle"
    ]
    _set_payload(removed, observer_bundle.VECTORS_REL, vectors)
    _set_bundle_version(removed, _next_minor_version())

    _assert_history_failure(repo, removed, "major change")


def test_observer_client_bundle_history_does_not_apply_current_vector_policy_to_baseline(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    baseline = _clone_bundle_files(bundle_files)
    vectors = _payload(baseline, observer_bundle.VECTORS_REL)
    for vector in vectors["vectors"]:
        if vector["id"] == "observer.auth.handle":
            vector["decision"] = {
                "accepted": True,
                "auth_form": "legacy_x_solstone_observer",
                "kind": "auth_header_form",
                "precedence": "x_solstone_observer_preferred_when_both_present",
            }
            break
    else:
        raise AssertionError("missing observer.auth.handle vector")
    _set_payload(baseline, observer_bundle.VECTORS_REL, vectors)
    _commit_bundle(repo, baseline, "legacy vector policy")
    candidate = _clone_bundle_files(bundle_files)
    _set_bundle_version(candidate, _next_major_version())

    assert (
        observer_bundle_compatibility.check_bundle_compatibility(
            repo,
            candidate,
            enforce_current_contract=False,
        )
        == []
    )


def test_observer_client_bundle_history_unknown_vector_schema_fails_closed(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    baseline = _clone_bundle_files(bundle_files)
    vectors = _payload(baseline, observer_bundle.VECTORS_REL)
    vectors["schema"] = "solstone.observer-client-contract-vectors.v999"
    _set_payload(baseline, observer_bundle.VECTORS_REL, vectors)
    _commit_bundle(repo, baseline, "unknown vector schema")

    _assert_history_failure(
        repo,
        bundle_files,
        "historical bundle",
        enforce_current_contract=False,
    )


def test_observer_client_bundle_history_new_independent_vector_accepts_minor(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit_bundle(repo, bundle_files, "bundle")
    added = _clone_bundle_files(bundle_files)
    fixtures = _payload(added, observer_bundle.FIXTURES_REL)
    fixtures["fixtures"].append(
        {
            "id": "declared.future.independent",
            "kind": "declared-negative",
            "payload": {"note": "future"},
            "provenance": {
                "direction": "response",
                "media_type": "application/json",
                "named_variant": "future",
                "operation_id": "observer.ingestSegments",
                "status": 200,
            },
            "schema_validation": {"validates": None, "reason": "Compatibility test"},
        }
    )
    fixtures["fixtures"] = sorted(fixtures["fixtures"], key=lambda item: item["id"])
    _set_payload(added, observer_bundle.FIXTURES_REL, fixtures)
    vectors = _payload(added, observer_bundle.VECTORS_REL)
    vectors["vectors"].append(
        {
            "decision": {
                "change_scope": "additive",
                "description": "Future independent client behavior.",
                "kind": "independent_behavior",
            },
            "fixture_id": "declared.future.independent",
            "id": "future.independent",
            "kind": "declared",
            "pointer_hashes": {
                "": _sha256_text(observer_bundle.render_json({"note": "future"}))
            },
            "pointers": [""],
        }
    )
    vectors["vectors"] = sorted(vectors["vectors"], key=lambda item: item["id"])
    _set_payload(added, observer_bundle.VECTORS_REL, vectors)
    _set_bundle_version(added, _next_minor_version())

    assert (
        observer_bundle_compatibility.check_bundle_compatibility(
            repo,
            added,
            enforce_current_contract=False,
        )
        == []
    )


def test_observer_client_bundle_history_semantic_vector_change_is_never_patch(
    bundle_files: dict[Path, str],
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit_bundle(repo, bundle_files, "bundle")

    _assert_history_failure(
        repo,
        _semantic_status_fixture_candidate(bundle_files, _next_minor_version()),
        "major change",
        enforce_current_contract=False,
    )
    assert (
        observer_bundle_compatibility.check_bundle_compatibility(
            repo,
            _semantic_status_fixture_candidate(bundle_files, _next_major_version()),
            enforce_current_contract=False,
        )
        == []
    )
