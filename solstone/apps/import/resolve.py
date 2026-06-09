# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Import review/resolution write owner (relocated from call.py behind the HTTP boundary)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solstone.think.entities.core import EntityDict, entity_slug
from solstone.think.entities.journal import (
    has_journal_principal,
    journal_entity_path,
    load_all_journal_entities,
    save_journal_entity,
)
from solstone.think.entities.observations import load_observations, save_observations
from solstone.think.entities.relationships import (
    load_facet_relationship,
    save_facet_relationship,
)
from solstone.think.journal_config import write_journal_config
from solstone.think.journal_io import atomic_replace
from solstone.think.utils import get_config, get_journal

from .ingest import _append_decision, _categorize_field, _write_state_atomic


class ResolveNotFound(Exception):
    pass


class ResolveInvalid(Exception):
    pass


_ENTITY_FILE_TYPES = {
    "entity_relationship",
    "entity_observations",
    "detected_entities",
    "activity_records",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _set_nested(cfg: dict, dotted_key: str, value: Any) -> None:
    parts = dotted_key.split(".")
    current = cfg
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def merge_entity_fields(
    target: EntityDict, source: EntityDict
) -> tuple[EntityDict, list[str]]:
    merged: EntityDict = dict(target)
    pre_merge_snapshot = dict(merged)

    aka_by_lower: dict[str, str] = {}
    for values in (merged.get("aka", []), source.get("aka", [])):
        if not isinstance(values, list):
            continue
        for value in values:
            if not value:
                continue
            key = str(value).lower()
            if key not in aka_by_lower:
                aka_by_lower[key] = str(value)
    if aka_by_lower:
        merged["aka"] = sorted(aka_by_lower.values(), key=str.lower)

    merged_emails: list[str] = []
    seen_emails: set[str] = set()
    for values in (merged.get("emails", []), source.get("emails", [])):
        if not isinstance(values, list):
            continue
        for value in values:
            if not value:
                continue
            email = str(value)
            key = email.lower()
            if key in seen_emails:
                continue
            seen_emails.add(key)
            merged_emails.append(email)
    if merged_emails:
        merged["emails"] = merged_emails

    source_created = source.get("created_at")
    target_created = merged.get("created_at")
    if source_created is not None and target_created is not None:
        merged["created_at"] = min(source_created, target_created)
    elif source_created is not None:
        merged["created_at"] = source_created

    fields_changed = sorted(
        key
        for key in set(pre_merge_snapshot) | set(merged)
        if pre_merge_snapshot.get(key) != merged.get(key)
    )
    return merged, fields_changed


def _allocate_slug(name: str) -> str | None:
    base_slug = entity_slug(name)
    if not base_slug:
        return None

    for attempt in range(1, 102):
        candidate = base_slug if attempt == 1 else f"{base_slug}_{attempt}"
        if not journal_entity_path(candidate).exists():
            return candidate
    return None


def _log_resolution(
    log_path: Path,
    action: str,
    item_type: str,
    item_id: str,
    reason: str,
    **extra: Any,
) -> None:
    entry = {
        "ts": _now_iso(),
        "action": action,
        "item_type": item_type,
        "item_id": item_id,
        "reason": reason,
        "resolved_by": "talent",
    }
    entry.update(extra)
    _append_decision(log_path, entry)


def _load_entity_state(state_path: Path) -> dict[str, dict[str, Any]]:
    entity_state = _load_json(state_path, {})
    if not isinstance(entity_state, dict):
        entity_state = {}

    id_map = entity_state.get("id_map")
    received = entity_state.get("received")
    if not isinstance(id_map, dict) or not isinstance(received, dict):
        return {"id_map": {}, "received": {}}

    return {"id_map": dict(id_map), "received": dict(received)}


def _parse_jsonl_text(source_data: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line_number, line in enumerate(source_data.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at line {line_number}: {exc.msg}") from exc
        if not isinstance(item, dict):
            raise ValueError(
                f"Invalid JSONL at line {line_number}: item must be an object"
            )
        items.append(item)
    return items


def _append_jsonl_items(target_path: Path, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    from .facet_ingest import _serialize_jsonl

    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "ab") as handle:
        handle.write(_serialize_jsonl(items))


def _load_config_diff(diff_path: Path) -> dict[str, dict[str, Any]]:
    diff = _load_json(diff_path, {})
    if not isinstance(diff, dict):
        raise ResolveInvalid("Config diff is invalid.")
    return diff


def _write_config_diff(diff_path: Path, diff: dict[str, dict[str, Any]]) -> None:
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace(diff_path, json.dumps(diff, indent=2, ensure_ascii=False) + "\n")


def _resolve_config_field(state_dir: Path, field: str, action: str) -> None:
    diff_path = state_dir / "config" / "diff.json"
    if not diff_path.exists():
        raise ResolveNotFound("No staged config diff found.")

    diff = _load_config_diff(diff_path)
    if field not in diff:
        raise ResolveNotFound(f"Config field '{field}' is not staged.")

    if action not in {"apply", "keep"}:
        raise ResolveInvalid("Action must be 'apply' or 'keep'.")

    diff_entry = diff[field]
    if not isinstance(diff_entry, dict):
        raise ResolveInvalid(f"Config field '{field}' has invalid diff data.")

    if action == "apply":
        config = get_config()
        _set_nested(config, field, diff_entry.get("source"))
        write_journal_config(config)
        log_action = "config_field_applied"
        reason = "review_apply"
    else:
        log_action = "config_field_kept"
        reason = "review_keep"

    diff.pop(field)
    if diff:
        _write_config_diff(diff_path, diff)
    else:
        diff_path.unlink(missing_ok=True)
        (state_dir / "config" / "source_config.json").unlink(missing_ok=True)

    _log_resolution(
        state_dir / "config" / "log.jsonl",
        action=log_action,
        item_type="config",
        item_id=field,
        reason=reason,
        category=diff_entry.get("category", _categorize_field(field)),
        source=diff_entry.get("source"),
        target_previous=diff_entry.get("target"),
    )


def resolve_entity(
    state_dir: Path, source_id: str, action: str, target: str | None
) -> dict[str, str | None]:
    if action not in {"merge", "create", "skip"}:
        raise ResolveInvalid("Action must be 'merge', 'create', or 'skip'.")

    staged_path = state_dir / "entities" / "staged" / f"{source_id}.json"
    if not staged_path.exists():
        raise ResolveNotFound(f"Staged entity '{source_id}' not found.")

    payload = _load_json(staged_path, {})
    if not isinstance(payload, dict):
        raise ResolveInvalid(f"Staged entity '{source_id}' is invalid.")

    source_entity = payload.get("source_entity")
    if not isinstance(source_entity, dict):
        raise ResolveInvalid(f"Staged entity '{source_id}' is missing source_entity.")

    log_path = state_dir / "entities" / "log.jsonl"
    state_path = state_dir / "entities" / "state.json"
    entity_state = _load_entity_state(state_path)
    reason = str(payload.get("reason", ""))
    match_candidates = payload.get("match_candidates")
    match_tier = None
    if isinstance(match_candidates, list) and match_candidates:
        first_candidate = match_candidates[0]
        if isinstance(first_candidate, dict):
            match_tier = first_candidate.get("tier")

    if action == "merge":
        if not target:
            raise ResolveInvalid("--target is required for merge.")

        target_entities = load_all_journal_entities()
        target_entity = target_entities.get(target)
        if target_entity is None:
            raise ResolveNotFound(
                f"Target entity '{target}' not found. Use "
                "'list-staged --source SOURCE --area entities' to check "
                "match candidates, or use 'create' instead of 'merge'."
            )

        merged, fields_changed = merge_entity_fields(target_entity, source_entity)
        save_journal_entity(merged)
        entity_state["id_map"][source_id] = target
        _write_state_atomic(state_path, entity_state)
        staged_path.unlink()
        _log_resolution(
            log_path,
            action="resolved_merge",
            item_type="entity",
            item_id=source_id,
            reason=reason,
            source=source_entity,
            target=merged,
            fields_changed=fields_changed,
            match_tier=match_tier,
        )
        return {"target_id": target}

    if target is not None:
        raise ResolveInvalid("--target is only valid for merge.")

    if action == "create":
        created_entity = dict(source_entity)
        final_id = str(created_entity.get("id") or source_id)
        if reason == "id_collision" or journal_entity_path(final_id).exists():
            allocated = _allocate_slug(str(created_entity.get("name", "")))
            if allocated is None:
                raise ResolveInvalid(
                    f"Unable to allocate a slug for '{created_entity.get('name', '')}'."
                )
            final_id = allocated
        created_entity["id"] = final_id

        if reason == "principal_conflict" and has_journal_principal():
            created_entity["is_principal"] = False

        save_journal_entity(created_entity)
        entity_state["id_map"][source_id] = final_id
        _write_state_atomic(state_path, entity_state)
        staged_path.unlink()
        _log_resolution(
            log_path,
            action="resolved_create",
            item_type="entity",
            item_id=source_id,
            reason=reason,
            source=source_entity,
            target=created_entity,
            match_tier=match_tier,
            fields_changed=[],
        )
        return {"target_id": final_id}

    staged_path.unlink()
    _log_resolution(
        log_path,
        action="resolved_skip",
        item_type="entity",
        item_id=source_id,
        reason=reason,
        source=source_entity,
        target=None,
        match_tier=match_tier,
        fields_changed=[],
    )
    return {"target_id": None}


def resolve_staged_facet(
    state_dir: Path, staged_file: str, mode: str
) -> dict[str, Any]:
    staged_dir = state_dir / "facets" / "staged"
    staged_path = staged_dir / staged_file
    if not staged_path.exists():
        raise ResolveNotFound(f"Staged facet file '{staged_file}' not found.")

    payload = _load_json(staged_path, {})
    if not isinstance(payload, dict):
        raise ResolveInvalid(f"Staged facet file '{staged_file}' is invalid.")

    parts = Path(staged_file).parts
    if len(parts) < 3:
        raise ResolveInvalid(f"Staged facet file '{staged_file}' has an invalid path.")

    facet_name = parts[0]
    file_type = parts[1]
    reason = str(payload.get("reason", ""))
    log_path = state_dir / "facets" / "log.jsonl"

    if reason == "facet_json_conflict":
        item_id = f"{facet_name}/facet.json"
    else:
        item_id = f"{facet_name}/{payload.get('source_path', staged_file)}"

    if mode == "skip":
        staged_path.unlink()
        _log_resolution(
            log_path,
            action="resolved_skip",
            item_type=file_type,
            item_id=item_id,
            reason=reason,
            facet=facet_name,
            staged_path=str(staged_path),
        )
        return {}

    if reason == "unmapped_entity":
        if file_type not in _ENTITY_FILE_TYPES:
            raise ResolveInvalid(f"Unsupported staged facet file type '{file_type}'.")

        entities_state = _load_entity_state(state_dir / "entities" / "state.json")
        id_map = entities_state.get("id_map", {})
        source_entity_id = str(payload.get("source_entity_id", ""))
        if source_entity_id not in id_map:
            raise ResolveInvalid(
                f"Entity {source_entity_id} has no mapping yet. Run entity review first."
            )

        source_path = str(payload.get("source_path", ""))
        source_data = str(payload.get("source_data", ""))

        from .facet_ingest import _parse_path, _remap_entity_ids

        normalized_path, path_info = _parse_path(source_path, file_type)
        if file_type == "entity_relationship":
            parsed_data: Any = json.loads(source_data)
        else:
            parsed_data = _parse_jsonl_text(source_data)

        remapped_data, remapped_path_info = _remap_entity_ids(
            parsed_data, id_map, file_type, path_info
        )
        target_path = Path(get_journal()) / "facets" / facet_name / normalized_path

        if file_type == "entity_relationship":
            entity_id = remapped_path_info["entity_id"]
            source_relationship = dict(remapped_data)
            source_relationship["entity_id"] = entity_id
            target_relationship = load_facet_relationship(facet_name, entity_id) or {}
            merged_relationship = {**source_relationship, **target_relationship}
            save_facet_relationship(facet_name, entity_id, merged_relationship)
        elif file_type == "entity_observations":
            entity_id = remapped_path_info["entity_id"]
            target_observations = load_observations(facet_name, entity_id)
            seen = {
                (item.get("content", ""), item.get("observed_at"))
                for item in target_observations
            }
            merged_observations = list(target_observations)
            for item in remapped_data:
                key = (item.get("content", ""), item.get("observed_at"))
                if key in seen:
                    continue
                seen.add(key)
                merged_observations.append(item)
            save_observations(facet_name, entity_id, merged_observations)
        elif file_type in {"detected_entities", "activity_records"}:
            existing_items = (
                _parse_jsonl_text(target_path.read_text(encoding="utf-8"))
                if target_path.exists()
                else []
            )
            existing_ids = {item.get("id") for item in existing_items}
            new_items = [
                item for item in remapped_data if item.get("id") not in existing_ids
            ]
            _append_jsonl_items(target_path, new_items)
        else:
            raise ResolveInvalid(f"Unsupported staged facet file type '{file_type}'.")

        staged_path.unlink()
        _log_resolution(
            log_path,
            action="resolved_apply",
            item_type=file_type,
            item_id=item_id,
            reason=reason,
            facet=facet_name,
            staged_path=str(staged_path),
            target_path=str(target_path),
        )
        return {}

    if reason == "facet_json_conflict":
        target_path = Path(get_journal()) / "facets" / facet_name / "facet.json"
        atomic_replace(
            target_path,
            json.dumps(payload.get("source_content"), indent=2, ensure_ascii=False)
            + "\n",
        )
        staged_path.unlink()
        _log_resolution(
            log_path,
            action="resolved_apply",
            item_type=file_type,
            item_id=item_id,
            reason=reason,
            facet=facet_name,
            staged_path=str(staged_path),
            target_path=str(target_path),
        )
        return {}

    raise ResolveInvalid(f"Unsupported staged facet reason '{reason}'.")


def resolve_config(state_dir: Path, field: str, action: str) -> None:
    _resolve_config_field(state_dir, field, action)


def resolve_config_all(state_dir: Path, category: str) -> int:
    if category not in {"transferable", "preference"}:
        raise ResolveInvalid("Category must be 'transferable' or 'preference'.")

    diff_path = state_dir / "config" / "diff.json"
    if not diff_path.exists():
        raise ResolveNotFound("No staged config diff found.")

    diff = _load_config_diff(diff_path)
    fields = [
        field
        for field, diff_entry in diff.items()
        if isinstance(diff_entry, dict)
        and diff_entry.get("category", _categorize_field(field)) == category
    ]
    for field in list(fields):
        _resolve_config_field(state_dir, field, "apply")

    return len(fields)
