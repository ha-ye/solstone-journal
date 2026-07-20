# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""History and SemVer compatibility checks for the observer-client bundle."""

from __future__ import annotations

import copy
import os
import subprocess
from pathlib import Path
from typing import Any

from solstone.convey.contract.observer_bundle import (
    BUNDLE_REL_DIR,
    BUNDLE_SEMVER,
    MANIFEST_NAME,
    BundleCompatibilityError,
    BundleSnapshot,
    BundleVerificationError,
    ObserverBundleError,
    _repo_root,
    build_bundle_files,
    compare_semver,
    parse_semver,
    render_json,
)
from solstone.convey.contract.observer_bundle_verification import (
    _json_from_bytes,
    _operation_locations,
    _required_str,
    _resolve_json_pointer,
    _validate_bundle_snapshot,
)

_SEVERITY_RANK = {"patch": 0, "minor": 1, "major": 2}
_BUNDLE_TREE_ABSENT = "absent"
_BUNDLE_TREE_PRESENT = "present"


def check_bundle_compatibility(
    root: Path | None = None,
    candidate_files: dict[Path, str] | None = None,
    *,
    enforce_current_contract: bool = True,
) -> list[str]:
    """Return compatibility/history failures for an in-memory candidate bundle."""

    repo_root = _repo_root(root)
    try:
        generated = (
            candidate_files
            if candidate_files is not None
            else build_bundle_files(repo_root)
        )
        candidate = _snapshot_from_generated_files(
            generated,
            enforce_current_contract=enforce_current_contract,
        )
        _check_bundle_history_compatibility(repo_root, candidate)
    except ObserverBundleError as exc:
        return [str(exc)]
    return []


def _snapshot_from_generated_files(
    files: dict[Path, str],
    *,
    enforce_current_contract: bool,
) -> BundleSnapshot:
    bundle_files: dict[str, bytes] = {}
    for repo_rel_path, text in files.items():
        try:
            bundle_rel = repo_rel_path.relative_to(BUNDLE_REL_DIR).as_posix()
        except ValueError as exc:
            raise BundleVerificationError(
                f"generated file is outside bundle directory: {repo_rel_path}"
            ) from exc
        bundle_files[bundle_rel] = text.encode("utf-8")
    return _validate_bundle_snapshot(
        bundle_files,
        source="candidate",
        enforce_current_contract=enforce_current_contract,
    )


def _check_bundle_history_compatibility(root: Path, candidate: BundleSnapshot) -> None:
    _ensure_complete_git_history(root)
    commits = _earlier_history_commits(root)
    candidate_version = _required_str(candidate.manifest, "bundle_semver", "candidate")
    parse_semver(candidate_version)
    found_bundle_history = False
    bundle_commit_count = 0
    identical_commit_count = 0
    for commit in commits:
        tree_state = _historical_bundle_tree_state(root, commit)
        if tree_state == _BUNDLE_TREE_ABSENT:
            continue
        found_bundle_history = True
        bundle_commit_count += 1
        historical = _load_historical_snapshot(root, commit)
        if historical.files == candidate.files:
            identical_commit_count += 1
            continue
        severity, semantic = _classify_bundle_change(historical, candidate)
        _enforce_bundle_version(
            _required_str(historical.manifest, "bundle_semver", commit),
            candidate_version,
            severity,
            semantic=semantic,
        )
        return
    if not found_bundle_history:
        if candidate_version != BUNDLE_SEMVER:
            raise BundleCompatibilityError(
                "observer client contract history check failed: genuine first "
                f"bundle must use version {BUNDLE_SEMVER}, got {candidate_version}. "
                "Recovery: set bundle_semver to 1.0.0 before publishing the first bundle."
            )
        return
    if bundle_commit_count == identical_commit_count == 1 and (
        candidate_version != BUNDLE_SEMVER
    ):
        raise BundleCompatibilityError(
            "observer client contract history check failed: genuine first "
            f"bundle must use version {BUNDLE_SEMVER}, got {candidate_version}. "
            "Recovery: set bundle_semver to 1.0.0 before publishing the first bundle."
        )


def _load_historical_snapshot(root: Path, commit: str) -> BundleSnapshot:
    try:
        files = _git_bundle_tree_files(root, commit)
        return _validate_bundle_snapshot(
            files,
            source=f"history:{commit}",
            enforce_current_contract=False,
        )
    except (BundleVerificationError, ValueError, subprocess.CalledProcessError) as exc:
        raise BundleCompatibilityError(
            "observer client contract history check failed: historical bundle at "
            f"{commit} is corrupt: {exc}. Recovery: restore or regenerate a valid "
            "bundle at that baseline before changing the contract."
        ) from exc


def _ensure_complete_git_history(root: Path) -> None:
    shallow = _git_lines(root, ["rev-parse", "--is-shallow-repository"])
    if shallow == ["true"]:
        raise BundleCompatibilityError(
            "observer client contract history check failed: repository history is "
            "shallow. Recovery: fetch complete history before checking bundle compatibility."
        )
    grafts_path = _git_lines(root, ["rev-parse", "--git-path", "info/grafts"])
    if (
        grafts_path
        and (root / grafts_path[0]).exists()
        and (root / grafts_path[0]).stat().st_size
    ):
        raise BundleCompatibilityError(
            "observer client contract history check failed: repository uses grafts. "
            "Recovery: remove grafts and check against complete canonical history."
        )
    replacements = _git_lines(
        root,
        ["for-each-ref", "--format=%(refname)", "refs/replace"],
    )
    if replacements:
        raise BundleCompatibilityError(
            "observer client contract history check failed: repository uses replacement "
            "objects. Recovery: remove replacement refs and check canonical history."
        )
    try:
        _git_bytes(root, ["rev-list", "--objects", "--missing=error", "HEAD"])
        _git_bytes(root, ["fsck", "--full", "--no-dangling"])
    except BundleCompatibilityError as exc:
        raise BundleCompatibilityError(
            "observer client contract history check failed: git object database is "
            "corrupt or incomplete. Recovery: restore missing objects or fetch a "
            "complete, uncorrupted history before checking bundle compatibility."
        ) from exc


def _earlier_history_commits(root: Path) -> list[str]:
    head = _git_lines(root, ["rev-parse", "--verify", "HEAD"])
    if not head:
        raise BundleCompatibilityError(
            "observer client contract history check failed: HEAD is missing. "
            "Recovery: run inside a complete Git checkout."
        )
    try:
        return _git_lines(root, ["rev-list", "HEAD"])
    except BundleCompatibilityError as exc:
        raise BundleCompatibilityError(
            "observer client contract history check failed: missing ancestry. "
            "Recovery: fetch complete history before checking bundle compatibility."
        ) from exc


def _historical_bundle_tree_state(root: Path, commit: str) -> str:
    _git_bytes(root, ["cat-file", "-e", f"{commit}^{{commit}}"])
    tree_output = _git_bytes(
        root,
        ["ls-tree", "-z", "--full-tree", commit, "--", BUNDLE_REL_DIR.as_posix()],
    )
    if tree_output == b"":
        return _BUNDLE_TREE_ABSENT
    return _BUNDLE_TREE_PRESENT


def _git_bundle_tree_files(root: Path, commit: str) -> dict[str, bytes]:
    tree_output = _git_bytes(
        root,
        ["ls-tree", "-r", "-z", "--full-tree", commit, BUNDLE_REL_DIR.as_posix()],
    )
    if not tree_output:
        raise BundleVerificationError("bundle tree is empty")
    files: dict[str, bytes] = {}
    for entry in tree_output.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, repo_path_bytes = entry.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split()
            repo_path = repo_path_bytes.decode("utf-8")
        except ValueError as exc:
            raise BundleVerificationError("bundle tree entry is malformed") from exc
        if object_type != "blob" or not mode.startswith("100"):
            raise BundleVerificationError(
                f"bundle tree contains non-regular object: {repo_path}"
            )
        rel_path = Path(repo_path).relative_to(BUNDLE_REL_DIR).as_posix()
        files[rel_path] = _git_bytes(root, ["show", object_id])
    if MANIFEST_NAME not in files:
        raise BundleVerificationError("manifest object is missing")
    return files


def _classify_bundle_change(
    previous: BundleSnapshot, candidate: BundleSnapshot
) -> tuple[str, bool]:
    severity = "patch"
    semantic = False
    severity = _max_severity(
        severity,
        _classify_manifest_contract_change(previous.manifest, candidate.manifest),
    )

    previous_projection = _json_from_bytes(
        previous.files[previous.manifest["projection_path"]],
        "previous projection",
    )
    candidate_projection = _json_from_bytes(
        candidate.files[candidate.manifest["projection_path"]],
        "candidate projection",
    )
    severity = _max_severity(
        severity,
        _classify_projection_change(previous_projection, candidate_projection),
    )

    vocabulary_severity = _classify_vocabulary_change(
        previous.manifest.get("vocabularies", []),
        candidate.manifest.get("vocabularies", []),
    )
    severity = _max_severity(severity, vocabulary_severity)

    vector_severity, vector_semantic = _classify_vector_fixture_change(
        previous,
        candidate,
    )
    severity = _max_severity(severity, vector_severity)
    semantic = semantic or vector_semantic
    return severity, semantic


def _classify_manifest_contract_change(
    previous: dict[str, Any], candidate: dict[str, Any]
) -> str:
    severity = "patch"
    major_fields = {
        "bundle_schema_identity",
        "generator_identity",
        "observer_protocol_version",
        "openapi_document_version",
        "openapi_spec_version",
        "projection_path",
        "schema_dialect_uri",
        "supported_response_variants",
    }
    for field in major_fields:
        if previous.get(field) != candidate.get(field):
            severity = _max_severity(severity, "major")

    for field in (
        "operation_ids",
        "component_closure",
        "consumer_identifiers",
        "audited_consumer_revisions",
        "windows_linux_rollout_targets",
    ):
        old_values = previous.get(field)
        new_values = candidate.get(field)
        if old_values == new_values:
            continue
        if not isinstance(old_values, list) or not isinstance(new_values, list):
            severity = _max_severity(severity, "major")
            continue
        old_set = {render_json(item) for item in old_values}
        new_set = {render_json(item) for item in new_values}
        if old_set - new_set:
            severity = _max_severity(severity, "major")
        if new_set - old_set:
            severity = _max_severity(severity, "minor")
    return severity


def _classify_projection_change(previous: Any, candidate: Any) -> str:
    previous_projection = _strip_nonsemantic_openapi(previous)
    candidate_projection = _strip_nonsemantic_openapi(candidate)
    if previous_projection == candidate_projection:
        return "patch"

    severity = "patch"
    previous_operations = _operation_locations(previous_projection)
    candidate_operations = _operation_locations(candidate_projection)
    removed = set(previous_operations) - set(candidate_operations)
    added = set(candidate_operations) - set(previous_operations)
    if removed:
        severity = _max_severity(severity, "major")
    if added:
        severity = _max_severity(severity, "minor")
    for operation_id in sorted(set(previous_operations) & set(candidate_operations)):
        old_location = previous_operations[operation_id]
        new_location = candidate_operations[operation_id]
        if old_location[:2] != new_location[:2]:
            severity = _max_severity(severity, "major")
            continue
        old_operation = old_location[2]
        new_operation = new_location[2]
        if old_operation != new_operation:
            severity = _max_severity(
                severity,
                "minor" if _is_additive_only(old_operation, new_operation) else "major",
            )

    previous_components = previous_projection.get("components", {}).get("schemas", {})
    candidate_components = candidate_projection.get("components", {}).get("schemas", {})
    removed_components = set(previous_components) - set(candidate_components)
    added_components = set(candidate_components) - set(previous_components)
    if removed_components:
        severity = _max_severity(severity, "major")
    if added_components:
        severity = _max_severity(severity, "minor")
    for name in sorted(set(previous_components) & set(candidate_components)):
        if previous_components[name] == candidate_components[name]:
            continue
        severity = _max_severity(
            severity,
            "minor"
            if _is_additive_only(previous_components[name], candidate_components[name])
            else "major",
        )
    return severity


def _classify_vocabulary_change(previous: object, candidate: object) -> str:
    if not isinstance(previous, list) or not isinstance(candidate, list):
        return "major"
    old_vocab = {
        item.get("id"): item
        for item in previous
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    new_vocab = {
        item.get("id"): item
        for item in candidate
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    severity = "patch"
    if set(old_vocab) - set(new_vocab):
        severity = _max_severity(severity, "major")
    if set(new_vocab) - set(old_vocab):
        severity = _max_severity(severity, "minor")
    for vocab_id in sorted(set(old_vocab) & set(new_vocab)):
        old_item = old_vocab[vocab_id]
        new_item = new_vocab[vocab_id]
        old_class = old_item.get("classification")
        new_class = new_item.get("classification")
        if _is_extensible_class(old_class) and not _is_extensible_class(new_class):
            severity = _max_severity(severity, "major")
        old_values = _vocabulary_values(old_item)
        new_values = _vocabulary_values(new_item)
        if old_values - new_values:
            severity = _max_severity(severity, "major")
        added_values = new_values - old_values
        if added_values:
            if old_class == "closed" or new_class == "closed":
                severity = _max_severity(severity, "major")
            elif new_item.get("unknown_value_behavior") == "preserve":
                severity = _max_severity(severity, "minor")
            else:
                severity = _max_severity(severity, "major")
    return severity


def _classify_vector_fixture_change(
    previous: BundleSnapshot, candidate: BundleSnapshot
) -> tuple[str, bool]:
    old_fixtures = _fixtures_by_id(previous)
    new_fixtures = _fixtures_by_id(candidate)
    old_vectors = _vectors_by_id(previous)
    new_vectors = _vectors_by_id(candidate)
    severity = "patch"
    semantic = False
    for vector_id in sorted(set(old_vectors) & set(new_vectors)):
        old_vector = old_vectors[vector_id]
        new_vector = new_vectors[vector_id]
        old_fixture = old_fixtures.get(old_vector.get("fixture_id"))
        new_fixture = new_fixtures.get(new_vector.get("fixture_id"))
        if old_fixture is None or new_fixture is None:
            severity = _max_severity(severity, "major")
            continue
        if _vector_contract_meaning(old_vector, old_fixture) != (
            _vector_contract_meaning(new_vector, new_fixture)
        ):
            severity = _max_severity(severity, "major")
            semantic = True
    if set(old_vectors) - set(new_vectors):
        severity = _max_severity(severity, "major")
    if set(new_vectors) - set(old_vectors):
        severity = _max_severity(severity, "minor")
    return severity, semantic


def _vector_contract_meaning(
    vector: dict[str, Any],
    fixture: dict[str, Any],
) -> dict[str, Any]:
    payload = fixture.get("payload")
    pointers = vector.get("pointers")
    if not isinstance(pointers, list):
        pointers = []
    return {
        "decision": copy.deepcopy(vector.get("decision")),
        "fixture_id": vector.get("fixture_id"),
        "frame_kind": vector.get("frame_kind"),
        "kind": vector.get("kind"),
        "observed_status": vector.get("observed_status"),
        "pointer_values": {
            pointer: _resolve_json_pointer(payload, pointer)
            for pointer in pointers
            if isinstance(pointer, str)
        },
        "pointers": list(pointers),
    }


def _enforce_bundle_version(
    previous_version: str,
    candidate_version: str,
    severity: str,
    *,
    semantic: bool,
) -> None:
    previous_parts = parse_semver(previous_version)
    candidate_parts = parse_semver(candidate_version)
    comparison = compare_semver(candidate_version, previous_version)
    if comparison == 0:
        raise BundleCompatibilityError(
            "observer client contract compatibility failed: bundle content changed "
            f"without a version bump from {previous_version}. Recovery: bump "
            "bundle_semver according to the detected change severity."
        )
    if comparison < 0:
        raise BundleCompatibilityError(
            "observer client contract compatibility failed: bundle_semver "
            f"downgraded from {previous_version} to {candidate_version}. "
            "Recovery: use a forward SemVer bump."
        )
    if severity == "major":
        if candidate_parts[0] <= previous_parts[0]:
            raise BundleCompatibilityError(
                "observer client contract compatibility failed: major change "
                f"requires a major version bump from {previous_version} to "
                f"{candidate_version}. Recovery: bump bundle_semver major."
            )
        return
    if severity == "minor" or semantic:
        if (
            candidate_parts[0] == previous_parts[0]
            and candidate_parts[1] <= previous_parts[1]
        ):
            reason = "semantic vector change" if semantic else "minor change"
            raise BundleCompatibilityError(
                "observer client contract compatibility failed: "
                f"{reason} requires a minor or major version bump from "
                f"{previous_version} to {candidate_version}. Recovery: bump "
                "bundle_semver minor or major."
            )


def _strip_nonsemantic_openapi(node: Any) -> Any:
    ignored = {
        "description",
        "example",
        "examples",
        "externalDocs",
        "info",
        "summary",
        "title",
        "x-generated",
        "x-generated-by",
        "x-chat-events",
        "x-reason-codes",
        "x-sse-frame-kinds",
        "x-vocabularies",
        "x-vocabulary",
    }
    if isinstance(node, dict):
        return {
            key: _strip_nonsemantic_openapi(value)
            for key, value in node.items()
            if key not in ignored
        }
    if isinstance(node, list):
        return [_strip_nonsemantic_openapi(item) for item in node]
    return node


def _is_additive_only(previous: Any, candidate: Any) -> bool:
    if isinstance(previous, dict) and isinstance(candidate, dict):
        for key, old_value in previous.items():
            if key not in candidate:
                return False
            if key == "required":
                if isinstance(candidate[key], list) and isinstance(old_value, list):
                    if set(candidate[key]) != set(old_value):
                        return False
                    continue
                if candidate[key] != old_value:
                    return False
                continue
            if key == "properties":
                if not isinstance(old_value, dict) or not isinstance(
                    candidate[key], dict
                ):
                    return False
                for property_name, property_schema in old_value.items():
                    if property_name not in candidate[key]:
                        return False
                    if not _is_additive_only(
                        property_schema, candidate[key][property_name]
                    ):
                        return False
                continue
            if not _is_additive_only(old_value, candidate[key]):
                return False
        return True
    if isinstance(previous, list) and isinstance(candidate, list):
        return previous == candidate
    return previous == candidate


def _is_extensible_class(classification: object) -> bool:
    return isinstance(classification, str) and classification.startswith("extensible")


def _vocabulary_values(vocabulary: dict[str, Any]) -> set[str]:
    values = vocabulary.get("values")
    if isinstance(values, list):
        return {str(value) for value in values}
    subset = vocabulary.get("native_client_interest_subset")
    if isinstance(subset, list):
        return {str(value) for value in subset}
    registry = vocabulary.get("known_registry")
    if isinstance(registry, dict):
        return {
            f"{tract}.{event}"
            for tract, events in registry.items()
            if isinstance(events, list)
            for event in events
        }
    return set()


def _fixtures_by_id(snapshot: BundleSnapshot) -> dict[str, dict[str, Any]]:
    payload = _json_from_bytes(
        snapshot.files["fixtures/wire-behavior.json"], "fixtures"
    )
    return {
        item["id"]: item
        for item in payload["fixtures"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _vectors_by_id(snapshot: BundleSnapshot) -> dict[str, dict[str, Any]]:
    payload = _json_from_bytes(snapshot.files["vectors.json"], "vectors")
    return {
        item["id"]: item
        for item in payload["vectors"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _max_severity(left: str, right: str) -> str:
    return left if _SEVERITY_RANK[left] >= _SEVERITY_RANK[right] else right


def _git_lines(root: Path, args: list[str]) -> list[str]:
    try:
        output = _git_bytes(root, args).decode("utf-8")
    except subprocess.CalledProcessError as exc:
        raise BundleCompatibilityError(
            "observer client contract history check failed: git "
            f"{' '.join(args)} failed. Recovery: run inside a Git checkout."
        ) from exc
    return [line for line in output.splitlines() if line]


def _git_bytes(root: Path, args: list[str]) -> bytes:
    env = {
        **os.environ,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            check=True,
            env=env,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise BundleCompatibilityError(
            "observer client contract history check failed: git "
            f"{' '.join(args)} failed. Recovery: run inside a complete, "
            "uncorrupted Git checkout."
        ) from exc
