# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Verify observer-client contract bundles and consumer-audit coverage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from solstone.convey.contract.observer_bundle import (
    _SHA256_RE,
    _SOURCE_INPUTS,
    _WINDOWS_RESERVED_BASENAMES,
    AUDITED_CONSUMER_REVISIONS,
    BUNDLE_REL_DIR,
    BUNDLE_SCHEMA_IDENTITY,
    CONSUMER_IDENTIFIERS,
    GENERATOR_IDENTITY,
    MANIFEST_NAME,
    OBSERVER_CLIENT_OPERATION_IDS,
    SCHEMA_DIALECT_URI,
    WINDOWS_LINUX_ROLLOUT_TARGETS,
    BundleSnapshot,
    BundleVerificationError,
    ObserverBundleError,
    _directory_entry_snapshot,
    _entry_snapshot_identity,
    _is_relative_to,
    _iter_operations,
    _open_dir_at_no_follow,
    _open_parent_dir_no_follow,
    _read_regular_at_no_follow,
    _repo_root,
    _sha256_path,
    _sha256_text,
    _stat_identity,
    parse_semver,
    render_json,
    validate_projection_refs,
)
from solstone.observe import protocol

_CURRENT_VECTOR_DECISIONS: dict[str, dict[str, Any]] = {
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


def verify_bundle_directory(bundle_dir: Path) -> BundleSnapshot:
    """Verify a bundle directory's manifest, file inventory, and references."""

    bundle_root = Path(bundle_dir)
    files = _read_bundle_directory_files(bundle_root)
    return _validate_bundle_snapshot(
        files,
        source=str(bundle_root),
        enforce_current_contract=True,
    )


def verify_committed_bundle(root: Path | None = None) -> BundleSnapshot:
    """Verify the committed bundle and its current source-input digests."""

    repo_root = _repo_root(root)
    snapshot = verify_bundle_directory(repo_root / BUNDLE_REL_DIR)
    _verify_generator_input_records(repo_root, snapshot.manifest)
    return snapshot


def check_consumer_audit_coverage(root: Path | None = None) -> list[str]:
    """Return Windows/Linux consumer-audit coverage failures."""

    repo_root = _repo_root(root)
    try:
        snapshot = verify_bundle_directory(repo_root / BUNDLE_REL_DIR)
        _validate_consumer_audit_coverage(snapshot)
    except ObserverBundleError as exc:
        return [str(exc)]
    return []


def _read_bundle_directory_files(bundle_root: Path) -> dict[str, bytes]:
    parent_fd, leaf_name = _open_parent_dir_no_follow(bundle_root)
    try:
        try:
            bundle_stat = os.stat(leaf_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise BundleVerificationError(
                f"bundle directory does not exist: {bundle_root}"
            ) from exc
        if stat.S_ISLNK(bundle_stat.st_mode):
            raise BundleVerificationError(
                f"bundle directory is a symlink: {bundle_root}"
            )
        if not stat.S_ISDIR(bundle_stat.st_mode):
            raise BundleVerificationError(
                f"bundle path is not a directory: {bundle_root}"
            )
        bundle_fd = _open_dir_at_no_follow(parent_fd, leaf_name, bundle_stat)
    finally:
        os.close(parent_fd)
    try:
        return _read_bundle_fd_files(
            bundle_fd,
            source=str(bundle_root),
            rel_parts=(),
            expected_stat=os.fstat(bundle_fd),
        )
    finally:
        os.close(bundle_fd)


def verify_bundle_fd(bundle_fd: int, source: str) -> BundleSnapshot:
    """Verify a bundle directory already opened by the caller."""

    files = _read_bundle_fd_files(
        bundle_fd,
        source=source,
        rel_parts=(),
        expected_stat=os.fstat(bundle_fd),
    )
    return _validate_bundle_snapshot(
        files,
        source=source,
        enforce_current_contract=True,
    )


def _read_bundle_fd_files(
    dir_fd: int,
    *,
    source: str,
    rel_parts: tuple[bytes, ...],
    expected_stat: os.stat_result,
) -> dict[str, bytes]:
    before_dir = os.fstat(dir_fd)
    if _stat_identity(before_dir) != _stat_identity(expected_stat):
        raise BundleVerificationError(f"{source}: bundle directory changed before read")
    before_entries = _directory_entry_snapshot(dir_fd)
    files: dict[str, bytes] = {}
    for name, entry_stat in before_entries:
        entry_parts = (*rel_parts, name)
        rel_path = _bundle_rel_parts_to_posix(entry_parts)
        mode = entry_stat.st_mode
        if stat.S_ISLNK(mode):
            raise BundleVerificationError(f"bundle path is a symlink: {rel_path}")
        if stat.S_ISDIR(mode):
            child_fd = _open_dir_at_no_follow(dir_fd, name, entry_stat)
            try:
                files.update(
                    _read_bundle_fd_files(
                        child_fd,
                        source=source,
                        rel_parts=entry_parts,
                        expected_stat=os.fstat(child_fd),
                    )
                )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(mode):
            raise BundleVerificationError(
                f"bundle path is not a regular file: {rel_path}"
            )
        _validate_manifest_relative_path(rel_path)
        files[rel_path] = _read_regular_at_no_follow(dir_fd, name, entry_stat)
    after_entries = _directory_entry_snapshot(dir_fd)
    if _entry_snapshot_identity(after_entries) != _entry_snapshot_identity(
        before_entries
    ):
        raise BundleVerificationError(
            f"{source}: bundle directory entries changed during read"
        )
    if _stat_identity(os.fstat(dir_fd)) != _stat_identity(before_dir):
        raise BundleVerificationError(f"{source}: bundle directory changed during read")
    return files


def _bundle_rel_parts_to_posix(parts: tuple[bytes, ...]) -> str:
    return "/".join(os.fsdecode(part) for part in parts)


def _validate_bundle_snapshot(
    files: dict[str, bytes],
    *,
    source: str,
    enforce_current_contract: bool,
) -> BundleSnapshot:
    if MANIFEST_NAME not in files:
        raise BundleVerificationError(f"{source}: missing manifest.json")
    manifest = _json_from_bytes(files[MANIFEST_NAME], f"{source}:manifest.json")
    if not isinstance(manifest, dict):
        raise BundleVerificationError(f"{source}: manifest.json is not an object")

    parse_semver(_required_str(manifest, "bundle_semver", source))
    file_paths = _validate_manifest_file_records(manifest.get("files"), source)
    expected_file_sequence = [MANIFEST_NAME, *file_paths]
    if sorted(files) != sorted(expected_file_sequence):
        missing = sorted(set(expected_file_sequence) - set(files))
        extra = sorted(set(files) - set(expected_file_sequence))
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unlisted " + ", ".join(extra))
        raise BundleVerificationError(
            f"{source}: bundle file set mismatch: {'; '.join(detail)}"
        )

    for record in manifest["files"]:
        rel_path = record["path"]
        expected_sha = record["sha256"]
        actual_sha = hashlib.sha256(files[rel_path]).hexdigest()
        if actual_sha != expected_sha:
            raise BundleVerificationError(
                f"{source}: digest mismatch for {rel_path}: "
                f"expected {expected_sha}, got {actual_sha}"
            )

    _verify_generator_inputs_do_not_self_reference(manifest)
    projection = _validate_projection_payload(manifest, files, source)
    _validate_fixture_vector_references(
        files,
        source,
        enforce_current_contract=enforce_current_contract,
    )
    snapshot = BundleSnapshot(
        manifest=manifest,
        files={path: files[path] for path in sorted(expected_file_sequence)},
    )
    if enforce_current_contract:
        _validate_current_contract_requirements(snapshot, projection, source)
    return snapshot


def _validate_manifest_file_records(records: object, source: str) -> list[str]:
    if not isinstance(records, list):
        raise BundleVerificationError(f"{source}: manifest.files is not a list")
    paths: list[str] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise BundleVerificationError(
                f"{source}: manifest.files[{index}] is not an object"
            )
        path = record.get("path")
        digest = record.get("sha256")
        if not isinstance(path, str):
            raise BundleVerificationError(
                f"{source}: manifest.files[{index}].path is not a string"
            )
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise BundleVerificationError(
                f"{source}: manifest.files[{index}].sha256 is not a SHA-256 digest"
            )
        paths.append(path)
    _validate_manifest_relative_paths(paths)
    if paths != sorted(paths):
        raise BundleVerificationError(f"{source}: manifest.files is not sorted by path")
    return paths


def _validate_manifest_relative_paths(paths: Iterable[str]) -> None:
    seen: set[str] = set()
    seen_casefolded: dict[str, str] = {}
    for path in paths:
        normalized = _validate_manifest_relative_path(path)
        if normalized in seen:
            raise BundleVerificationError(f"duplicate bundle path: {path}")
        seen.add(normalized)
        folded = normalized.casefold()
        if folded in seen_casefolded:
            raise BundleVerificationError(
                f"case-fold-colliding bundle paths: {seen_casefolded[folded]} and {path}"
            )
        seen_casefolded[folded] = normalized


def _validate_manifest_relative_path(path: str) -> str:
    if path == "":
        raise BundleVerificationError("empty bundle path")
    if "\\" in path:
        raise BundleVerificationError(f"bundle path contains backslash: {path}")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise BundleVerificationError(
            f"bundle path contains control character: {path!r}"
        )
    if re.search(r"(^|/)[A-Za-z]:", path):
        raise BundleVerificationError(
            f"bundle path contains Windows drive prefix: {path}"
        )
    if ":" in path:
        raise BundleVerificationError(f"bundle path contains colon: {path}")
    if any(char in path for char in "*?[]"):
        raise BundleVerificationError(
            f"bundle path contains wildcard character: {path}"
        )
    raw_parts = path.split("/")
    if any(part == "" for part in raw_parts):
        raise BundleVerificationError(f"bundle path has empty component: {path}")
    if any(part in {".", ".."} for part in raw_parts):
        raise BundleVerificationError(f"bundle path has unsafe component: {path}")
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute():
        raise BundleVerificationError(f"bundle path is absolute: {path}")
    parts = pure_path.parts
    if not parts:
        raise BundleVerificationError(f"bundle path has empty component: {path}")
    for part in parts:
        if part.endswith("."):
            raise BundleVerificationError(
                f"bundle path component has trailing dot: {path}"
            )
        if part.endswith(" "):
            raise BundleVerificationError(
                f"bundle path component has trailing space: {path}"
            )
        basename = part.split(".", 1)[0].upper()
        if basename in _WINDOWS_RESERVED_BASENAMES:
            raise BundleVerificationError(
                f"bundle path uses Windows-reserved device name: {path}"
            )
    return pure_path.as_posix()


def _json_from_bytes(payload: bytes, label: str) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleVerificationError(f"{label}: invalid JSON") from exc


def _required_str(payload: dict[str, Any], field: str, source: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise BundleVerificationError(f"{source}: manifest.{field} is required")
    return value


def _validate_projection_payload(
    manifest: dict[str, Any], files: dict[str, bytes], source: str
) -> dict[str, Any]:
    projection_path = manifest.get("projection_path")
    if not isinstance(projection_path, str):
        raise BundleVerificationError(f"{source}: manifest.projection_path is required")
    _validate_manifest_relative_path(projection_path)
    if projection_path not in files:
        raise BundleVerificationError(f"{source}: projection file is missing")
    projection = _json_from_bytes(files[projection_path], f"{source}:{projection_path}")
    if not isinstance(projection, dict):
        raise BundleVerificationError(f"{source}: projection document is not an object")
    try:
        validate_projection_refs(projection)
    except ObserverBundleError as exc:
        raise BundleVerificationError(f"{source}: {exc}") from exc

    manifest_operations = manifest.get("operation_ids")
    if not isinstance(manifest_operations, list) or not all(
        isinstance(item, str) for item in manifest_operations
    ):
        raise BundleVerificationError(f"{source}: manifest.operation_ids is invalid")
    projected_operations = sorted(
        operation["operationId"]
        for _path, _method, operation in _iter_operations(projection)
    )
    if projected_operations != manifest_operations:
        raise BundleVerificationError(
            f"{source}: projection operation IDs do not match manifest.operation_ids"
        )
    manifest_components = manifest.get("component_closure")
    if not isinstance(manifest_components, list) or not all(
        isinstance(item, str) for item in manifest_components
    ):
        raise BundleVerificationError(
            f"{source}: manifest.component_closure is invalid"
        )
    projected_components = sorted(projection.get("components", {}).get("schemas", {}))
    if projected_components != manifest_components:
        raise BundleVerificationError(
            f"{source}: projection components do not match manifest.component_closure"
        )
    return projection


def _validate_current_contract_requirements(
    snapshot: BundleSnapshot,
    projection: dict[str, Any],
    source: str,
) -> None:
    manifest = snapshot.manifest
    if manifest.get("generator_identity") != GENERATOR_IDENTITY:
        raise BundleVerificationError(f"{source}: unexpected generator_identity")
    if manifest.get("bundle_schema_identity") != BUNDLE_SCHEMA_IDENTITY:
        raise BundleVerificationError(f"{source}: unexpected bundle_schema_identity")
    if manifest.get("schema_dialect_uri") != SCHEMA_DIALECT_URI:
        raise BundleVerificationError(f"{source}: unexpected schema_dialect_uri")
    if manifest.get("openapi_document_version") != projection.get("info", {}).get(
        "version"
    ):
        raise BundleVerificationError(
            f"{source}: manifest.openapi_document_version must match projection info.version"
        )
    if manifest.get("openapi_spec_version") != projection.get("openapi"):
        raise BundleVerificationError(
            f"{source}: manifest.openapi_spec_version must match projection openapi"
        )
    if manifest.get("observer_protocol_version") != protocol.OBSERVER_PROTOCOL_VERSION:
        raise BundleVerificationError(f"{source}: unexpected observer_protocol_version")
    if manifest.get("supported_response_variants") != [1, 2]:
        raise BundleVerificationError(
            f"{source}: unexpected supported_response_variants"
        )
    if manifest.get("operation_ids") != list(OBSERVER_CLIENT_OPERATION_IDS):
        raise BundleVerificationError(f"{source}: unexpected operation_ids")
    if manifest.get("consumer_identifiers") != CONSUMER_IDENTIFIERS:
        raise BundleVerificationError(f"{source}: unexpected consumer_identifiers")
    if manifest.get("audited_consumer_revisions") != AUDITED_CONSUMER_REVISIONS:
        raise BundleVerificationError(
            f"{source}: unexpected audited_consumer_revisions"
        )
    if manifest.get("windows_linux_rollout_targets") != WINDOWS_LINUX_ROLLOUT_TARGETS:
        raise BundleVerificationError(
            f"{source}: unexpected windows_linux_rollout_targets"
        )
    if manifest.get("component_closure") != [
        "CallosumEvent",
        "Error",
        "SegmentFile",
        "SegmentItem",
        "SegmentsEnvelope",
    ]:
        raise BundleVerificationError(f"{source}: unexpected component_closure")

    operation_locations = _operation_locations(projection)
    expected_locations = {
        "callosum.rootEvents": ("/sse/events", "get"),
        "chat.openSolChatRequest": ("/api/chat/sol_chat_request/open", "post"),
        "link.pair": ("/app/network/pair", "post"),
        "observer.callosumStream": ("/app/observer/callosum", "get"),
        "observer.ingestEvent": ("/app/observer/ingest/event", "post"),
        "observer.ingestSegments": ("/app/observer/ingest/segments/{day}", "get"),
        "observer.ingestUpload": ("/app/observer/ingest", "post"),
        "observer.register": ("/app/observer/register", "post"),
    }
    for operation_id, expected in expected_locations.items():
        location = operation_locations.get(operation_id)
        if location is None or location[:2] != expected:
            raise BundleVerificationError(
                f"{source}: unexpected location for {operation_id}"
            )

    chat_operation = _operation_by_id(projection, "chat.openSolChatRequest")
    request_schema = chat_operation["requestBody"]["content"]["application/json"][
        "schema"
    ]
    if request_schema.get("required") != ["request_id"]:
        raise BundleVerificationError(
            f"{source}: chat.openSolChatRequest request_id must be required"
        )
    request_id = request_schema.get("properties", {}).get("request_id")
    if request_id != {"minLength": 1, "pattern": "\\S", "type": "string"}:
        raise BundleVerificationError(
            f"{source}: chat.openSolChatRequest request_id must be a non-blank string"
        )
    success_schema = chat_operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    if success_schema.get("required") != ["ok"]:
        raise BundleVerificationError(
            f"{source}: chat.openSolChatRequest ok must be required"
        )
    if success_schema.get("properties", {}).get("ok") != {"type": "boolean"}:
        raise BundleVerificationError(
            f"{source}: chat.openSolChatRequest ok must be boolean"
        )
    if chat_operation["responses"]["400"].get("x-reason-codes") != [
        "missing_required_field"
    ]:
        raise BundleVerificationError(
            f"{source}: chat.openSolChatRequest 400 reason must be missing_required_field"
        )
    if chat_operation["responses"]["403"].get("x-reason-codes") != ["pl_revoked"]:
        raise BundleVerificationError(
            f"{source}: chat.openSolChatRequest 403 reason must be pl_revoked"
        )

    upload_schema = _response_json_schema(
        projection,
        "observer.ingestUpload",
        "200",
    )
    upload_status = upload_schema.get("properties", {}).get("status", {})
    if upload_status.get("enum") != [
        "ok",
        "duplicate",
        "collision",
        "conflict",
        "failed",
    ]:
        raise BundleVerificationError(f"{source}: ingest upload status enum drifted")
    upload_vocab = upload_status.get("x-vocabulary", {})
    if (
        upload_vocab.get("classification") != "closed"
        or upload_vocab.get("unknown_value_behavior") != "reject"
    ):
        raise BundleVerificationError(
            f"{source}: ingest upload status unknown-value behavior drifted"
        )

    segment_file = projection["components"]["schemas"]["SegmentFile"]
    custody_status = segment_file["properties"]["status"]
    if custody_status.get("enum") != ["present", "missing", "processed"]:
        raise BundleVerificationError(f"{source}: SegmentFile.status enum drifted")
    custody_vocab = custody_status.get("x-vocabulary", {})
    if (
        custody_vocab.get("classification") != "closed"
        or custody_vocab.get("unknown_value_behavior") != "reject"
    ):
        raise BundleVerificationError(
            f"{source}: SegmentFile.status unknown-value behavior drifted"
        )

    segments_operation = _operation_by_id(projection, "observer.ingestSegments")
    protocol_vocab = segments_operation["responses"]["200"].get("x-vocabularies", {})
    if protocol_vocab.get("X-Solstone-Protocol-Version", {}).get("current") != 2:
        raise BundleVerificationError(
            f"{source}: X-Solstone-Protocol-Version current value drifted"
        )

    root_operation = _operation_by_id(projection, "callosum.rootEvents")
    chat_events = root_operation["responses"]["200"].get("x-chat-events", {})
    if chat_events.get("classification") != "extensible":
        raise BundleVerificationError(
            f"{source}: root SSE chat events must be extensible"
        )
    if chat_events.get("unknown_value_behavior") != "preserve":
        raise BundleVerificationError(
            f"{source}: root SSE chat events must preserve unknown values"
        )

    observer_sse = _operation_by_id(projection, "observer.callosumStream")
    observer_frames = observer_sse["responses"]["200"].get("x-sse-frame-kinds", {})
    if observer_frames.get("values") != ["data", "error", "heartbeat"]:
        raise BundleVerificationError(f"{source}: observer SSE frame kinds drifted")


def _response_json_schema(
    projection: dict[str, Any], operation_id: str, status: str
) -> dict[str, Any]:
    operation = _operation_by_id(projection, operation_id)
    return operation["responses"][status]["content"]["application/json"]["schema"]


def _validate_fixture_vector_references(
    files: dict[str, bytes],
    source: str,
    *,
    enforce_current_contract: bool,
) -> None:
    fixtures = _json_from_bytes(
        files["fixtures/wire-behavior.json"],
        f"{source}:fixtures/wire-behavior.json",
    )
    vectors = _json_from_bytes(files["vectors.json"], f"{source}:vectors.json")
    if not isinstance(fixtures, dict) or not isinstance(vectors, dict):
        raise BundleVerificationError(f"{source}: fixtures/vectors must be objects")
    if fixtures.get("schema") != "solstone.observer-client-contract-fixtures.v1":
        raise BundleVerificationError(f"{source}: unknown fixture schema")
    if vectors.get("schema") != "solstone.observer-client-contract-vectors.v1":
        raise BundleVerificationError(f"{source}: unknown vector schema")
    fixture_items = fixtures.get("fixtures")
    vector_items = vectors.get("vectors")
    if not isinstance(fixture_items, list) or not isinstance(vector_items, list):
        raise BundleVerificationError(f"{source}: fixtures/vectors arrays are required")
    fixture_ids: list[str] = []
    fixtures_by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(fixture_items):
        if not isinstance(item, dict):
            raise BundleVerificationError(
                f"{source}: fixture entry {index} is not an object"
            )
        fixture_id = item.get("id")
        if not isinstance(fixture_id, str) or not fixture_id:
            raise BundleVerificationError(f"{source}: fixture entry {index} missing id")
        if fixture_id in fixtures_by_id:
            raise BundleVerificationError(
                f"{source}: duplicate fixture id {fixture_id}"
            )
        fixture_ids.append(fixture_id)
        fixtures_by_id[fixture_id] = item
    if fixture_ids != sorted(fixture_ids):
        raise BundleVerificationError(f"{source}: fixture IDs are not sorted")

    vector_ids: list[str] = []
    seen_vector_ids: set[str] = set()
    for vector in vector_items:
        if not isinstance(vector, dict):
            raise BundleVerificationError(f"{source}: vector entry is not an object")
        vector_id = vector.get("id")
        fixture_id = vector.get("fixture_id")
        pointers = vector.get("pointers")
        if not isinstance(vector_id, str) or not vector_id:
            raise BundleVerificationError(f"{source}: vector missing id")
        if vector_id in seen_vector_ids:
            raise BundleVerificationError(f"{source}: duplicate vector id {vector_id}")
        vector_ids.append(vector_id)
        seen_vector_ids.add(vector_id)
        if not isinstance(fixture_id, str) or fixture_id not in fixtures_by_id:
            raise BundleVerificationError(
                f"{source}: vector {vector_id} references missing fixture {fixture_id}"
            )
        if not isinstance(pointers, list) or not all(
            isinstance(pointer, str) for pointer in pointers
        ):
            raise BundleVerificationError(
                f"{source}: vector {vector_id} pointers invalid"
            )
        payload = fixtures_by_id[fixture_id].get("payload")
        pointer_hashes = vector.get("pointer_hashes")
        if pointer_hashes is not None and not isinstance(pointer_hashes, dict):
            raise BundleVerificationError(
                f"{source}: vector {vector_id} pointer_hashes invalid"
            )
        for pointer in pointers:
            value = _resolve_json_pointer(payload, pointer)
            if isinstance(pointer_hashes, dict):
                expected_hash = pointer_hashes.get(pointer)
                actual_hash = _sha256_text(render_json(value))
                if expected_hash != actual_hash:
                    raise BundleVerificationError(
                        f"{source}: vector {vector_id} hash mismatch at {pointer}"
                    )
        _validate_vector_decision(
            vector,
            source,
            enforce_current_contract=enforce_current_contract,
        )
    if vector_ids != sorted(vector_ids):
        raise BundleVerificationError(f"{source}: vector IDs are not sorted")


def _validate_vector_decision(
    vector: dict[str, Any],
    source: str,
    *,
    enforce_current_contract: bool,
) -> None:
    vector_id = str(vector.get("id"))
    decision = vector.get("decision")
    if not isinstance(decision, dict):
        raise BundleVerificationError(f"{source}: vector {vector_id} missing decision")
    _validate_released_vector_decision_shape(vector_id, decision, source)
    if not enforce_current_contract:
        return
    expected = _CURRENT_VECTOR_DECISIONS.get(vector_id)
    if expected is None:
        raise BundleVerificationError(
            f"{source}: vector {vector_id} is not in the current policy table"
        )
    if decision != expected:
        raise BundleVerificationError(f"{source}: vector {vector_id} decision mismatch")


def _validate_released_vector_decision_shape(
    vector_id: str, decision: dict[str, Any], source: str
) -> None:
    kind = decision.get("kind")
    if not isinstance(kind, str) or not kind:
        raise BundleVerificationError(
            f"{source}: vector {vector_id} has invalid decision kind"
        )
    if kind == "independent_behavior":
        _require_string(decision, "change_scope", source, vector_id)
        _require_string(decision, "description", source, vector_id)
        return
    if kind == "ingest_status":
        _require_string(decision, "status", source, vector_id)
        _require_bool(decision, "accepted", source, vector_id)
        _require_int(decision, "http_status", source, vector_id)
        _require_string(decision, "client_action", source, vector_id)
        _require_string_list(decision, "stored_key_precedence", source, vector_id)
        if decision.get("stored_key_source") is not None:
            _require_string(decision, "stored_key_source", source, vector_id)
        if "original_key_source" in decision:
            _require_string(decision, "original_key_source", source, vector_id)
        return
    if kind == "sse_frame":
        _require_string(decision, "frame_kind", source, vector_id)
        _require_string(decision, "action", source, vector_id)
        for optional in ("unknown_event_behavior", "reason_code"):
            if optional in decision:
                _require_string(decision, optional, source, vector_id)
        return
    if kind == "protocol_variant":
        _require_string(decision, "header", source, vector_id)
        _require_int(decision, "parsed_version", source, vector_id)
        _require_string(decision, "response_variant", source, vector_id)
        for optional in ("absent_or_unparseable_uses", "current_protocol_version"):
            if optional in decision:
                _require_int(decision, optional, source, vector_id)
        return
    if kind == "custody_status":
        status_map = decision.get("holding_by_status")
        if not isinstance(status_map, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in status_map.items()
        ):
            raise BundleVerificationError(
                f"{source}: vector {vector_id} custody status map invalid"
            )
        _require_string(decision, "unknown_status", source, vector_id)
        return
    if kind in {"custody_unknown", "closed_vocabulary_unknown"}:
        _require_string(decision, "status", source, vector_id)
        if "unknown_status" in decision:
            _require_string(decision, "unknown_status", source, vector_id)
        if "unknown_value_behavior" in decision:
            _require_string(decision, "unknown_value_behavior", source, vector_id)
        if "vocabulary" in decision:
            _require_string(decision, "vocabulary", source, vector_id)
        return
    if kind == "auth_header_form":
        _require_string(decision, "auth_form", source, vector_id)
        _require_bool(decision, "accepted", source, vector_id)
        _require_string(decision, "precedence", source, vector_id)
        return
    if kind == "submitted_name_fallback":
        _require_string(decision, "fallback", source, vector_id)
        _require_bool(decision, "submitted_name_present", source, vector_id)
        return
    if kind == "chat_open_request":
        _require_bool(decision, "accepted", source, vector_id)
        _require_string(decision, "missing_field_behavior", source, vector_id)
        if decision.get("accepted"):
            _require_string(decision, "result", source, vector_id)
        else:
            _require_string(decision, "reason_code", source, vector_id)
        return
    if kind == "envelope_integrity":
        _require_string(decision, "expected", source, vector_id)
        _require_bool(decision, "valid", source, vector_id)
        return
    raise BundleVerificationError(
        f"{source}: vector {vector_id} has unknown decision kind {kind}"
    )


def _require_string(
    decision: dict[str, Any], field: str, source: str, vector_id: str
) -> str:
    value = decision.get(field)
    if not isinstance(value, str) or not value:
        raise BundleVerificationError(
            f"{source}: vector {vector_id} decision.{field} must be a string"
        )
    return value


def _require_bool(
    decision: dict[str, Any], field: str, source: str, vector_id: str
) -> bool:
    value = decision.get(field)
    if not isinstance(value, bool):
        raise BundleVerificationError(
            f"{source}: vector {vector_id} decision.{field} must be a boolean"
        )
    return value


def _require_int(
    decision: dict[str, Any], field: str, source: str, vector_id: str
) -> int:
    value = decision.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise BundleVerificationError(
            f"{source}: vector {vector_id} decision.{field} must be an integer"
        )
    return value


def _require_string_list(
    decision: dict[str, Any], field: str, source: str, vector_id: str
) -> list[str]:
    value = decision.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BundleVerificationError(
            f"{source}: vector {vector_id} decision.{field} must be a string list"
        )
    return value


def _resolve_json_pointer(payload: Any, pointer: str) -> Any:
    if pointer == "":
        return payload
    if not pointer.startswith("/"):
        raise BundleVerificationError(f"invalid JSON pointer: {pointer}")
    current = payload
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError) as exc:
                raise BundleVerificationError(
                    f"JSON pointer does not resolve: {pointer}"
                ) from exc
        elif isinstance(current, dict):
            if token not in current:
                raise BundleVerificationError(
                    f"JSON pointer does not resolve: {pointer}"
                )
            current = current[token]
        else:
            raise BundleVerificationError(f"JSON pointer does not resolve: {pointer}")
    return current


def _verify_generator_inputs_do_not_self_reference(manifest: dict[str, Any]) -> None:
    records = manifest.get("generator_inputs")
    if not isinstance(records, list):
        raise BundleVerificationError("manifest.generator_inputs is not a list")
    bundle_prefix = BUNDLE_REL_DIR.as_posix() + "/"
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise BundleVerificationError(
                f"manifest.generator_inputs[{index}] is not an object"
            )
        path = record.get("path")
        if not isinstance(path, str) or not path:
            raise BundleVerificationError(
                f"manifest.generator_inputs[{index}].path is not a string"
            )
        if path == BUNDLE_REL_DIR.as_posix() or path.startswith(bundle_prefix):
            raise BundleVerificationError(
                f"generator input points inside generated bundle: {path}"
            )


def _verify_generator_input_records(root: Path, manifest: dict[str, Any]) -> None:
    _verify_generator_inputs_do_not_self_reference(manifest)
    records = manifest.get("generator_inputs")
    if not isinstance(records, list):
        raise BundleVerificationError("manifest.generator_inputs is not a list")
    expected = sorted(
        (input_id, rel_path.as_posix(), role)
        for input_id, rel_path, role in _SOURCE_INPUTS
    )
    actual: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        if not isinstance(record, dict):
            raise BundleVerificationError("manifest.generator_inputs entry is invalid")
        input_id = record.get("id")
        path = record.get("path")
        role = record.get("role")
        digest = record.get("sha256")
        if not all(isinstance(item, str) and item for item in (input_id, path, role)):
            raise BundleVerificationError(
                "manifest.generator_inputs entry is incomplete"
            )
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise BundleVerificationError(
                f"manifest.generator_inputs[{input_id}].sha256 is invalid"
            )
        triple = (input_id, path, role)
        if triple in seen:
            raise BundleVerificationError(
                "duplicate generator input record: "
                f"id={input_id} path={path} role={role}"
            )
        seen.add(triple)
        actual.append(triple)
        source_path = root / path
        try:
            resolved_path = source_path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise BundleVerificationError(
                f"generator input path does not exist: {path}"
            ) from exc
        bundle_root = (root / BUNDLE_REL_DIR).resolve()
        if _is_relative_to(resolved_path, bundle_root):
            raise BundleVerificationError(
                f"generator input points inside generated bundle: {path}"
            )
        source_stat = source_path.lstat()
        if not (
            stat.S_ISLNK(source_stat.st_mode)
            or stat.S_ISREG(source_stat.st_mode)
            or stat.S_ISDIR(source_stat.st_mode)
        ):
            raise BundleVerificationError(
                f"generator input is not a regular file or directory: {path}"
            )
        actual_digest = _sha256_path(source_path)
        if actual_digest != digest:
            raise BundleVerificationError(
                f"generator input digest mismatch for {input_id}: "
                f"expected {digest}, got {actual_digest}"
            )
    if actual != expected:
        actual_set = set(actual)
        expected_set = set(expected)
        if actual_set == expected_set:
            raise BundleVerificationError(
                "manifest.generator_inputs is not sorted by id/path/role"
            )
        missing = sorted(expected_set - actual_set)
        extra = sorted(actual_set - expected_set)
        raise BundleVerificationError(
            "manifest.generator_inputs do not match expected source sequence: "
            f"missing={missing}; extra={extra}"
        )


def _validate_consumer_audit_coverage(snapshot: BundleSnapshot) -> None:
    audit = _json_from_bytes(
        snapshot.files["consumer-audit.json"],
        "consumer-audit.json",
    )
    if not isinstance(audit, dict):
        raise BundleVerificationError("consumer-audit.json is not an object")
    direct_paths = audit.get("direct_paths")
    searched_files = audit.get("searched_files")
    findings = audit.get("settings_drift_findings")
    if not isinstance(direct_paths, list) or not isinstance(searched_files, list):
        raise BundleVerificationError(
            "consumer audit missing searched/direct path data"
        )
    if not isinstance(findings, list):
        raise BundleVerificationError("consumer audit missing settings drift findings")

    known_consumers = set(snapshot.manifest.get("consumer_identifiers", []))
    projection = _json_from_bytes(
        snapshot.files[snapshot.manifest["projection_path"]],
        "projection.openapi.json",
    )
    projected_paths = set(projection.get("paths", {}))
    rollout_consumers = {
        item.get("consumer_identifier")
        for item in snapshot.manifest.get("windows_linux_rollout_targets", [])
        if isinstance(item, dict)
    }
    if rollout_consumers != {"solstone-linux", "solstone-windows"}:
        raise BundleVerificationError(
            "windows_linux_rollout_targets must be solstone-linux and solstone-windows"
        )

    direct_by_consumer: dict[str, list[dict[str, Any]]] = {}
    for item in direct_paths:
        if not isinstance(item, dict):
            raise BundleVerificationError("consumer audit direct path entry is invalid")
        consumer = item.get("consumer")
        path = item.get("path")
        classification = item.get("classification")
        rationale = item.get("rationale")
        if consumer not in known_consumers:
            raise BundleVerificationError(
                f"unknown consumer in audit direct path: {consumer}"
            )
        if not isinstance(path, str) or not path:
            raise BundleVerificationError("consumer audit direct path missing path")
        if not isinstance(classification, str) or not classification:
            raise BundleVerificationError(
                "consumer audit direct path missing classification"
            )
        if classification == "catch_all_exclusion":
            raise BundleVerificationError(
                "consumer audit uses forbidden catch-all exclusion"
            )
        if not isinstance(rationale, str) or not rationale:
            raise BundleVerificationError(
                "consumer audit direct path missing rationale"
            )
        if classification == "bundled" and path not in projected_paths:
            raise BundleVerificationError(
                f"bundled consumer path is absent from projection: {consumer} {path}"
            )
        direct_by_consumer.setdefault(str(consumer), []).append(item)

    for consumer in rollout_consumers:
        bundled = [
            item
            for item in direct_by_consumer.get(str(consumer), [])
            if item.get("classification") == "bundled"
        ]
        if not bundled:
            raise BundleVerificationError(
                f"rollout consumer has no bundled paths: {consumer}"
            )

    finding_ids = {
        item.get("id")
        for item in findings
        if isinstance(item, dict) and item.get("status") == "adoption_blocker"
    }
    linux_blockers = next(
        item.get("adoption_blocker_ids", [])
        for item in snapshot.manifest["windows_linux_rollout_targets"]
        if item.get("consumer_identifier") == "solstone-linux"
    )
    missing_blockers = sorted(set(linux_blockers) - finding_ids)
    if missing_blockers:
        raise BundleVerificationError(
            "linux rollout blockers missing from consumer audit: "
            + ", ".join(missing_blockers)
        )


def _operation_locations(document: dict[str, Any]) -> dict[str, tuple[str, str, Any]]:
    locations: dict[str, tuple[str, str, Any]] = {}
    for path, method, operation in _iter_operations(document):
        locations[operation["operationId"]] = (path, method, operation)
    return locations


def _operation_by_id(projection: dict[str, Any], operation_id: str) -> dict[str, Any]:
    for _path, _method, operation in _iter_operations(projection):
        if operation.get("operationId") == operation_id:
            return operation
    raise BundleVerificationError(f"projection missing operation {operation_id}")
