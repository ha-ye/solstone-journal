# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Sole writer for sandbox profile lifecycle intent."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solstone.think.journal_io import write_json
from solstone.think.sandbox_profile import manifest

INTENT_REL = Path("health") / "sandbox-profile" / "intent.json"

INTENT_PREPARED = "prepared"
INTENT_APPLY_STARTED = "apply_started"
INTENT_APPLIED = "applied"
INTENT_DISABLE_STARTED = "disable_started"
INTENT_DISABLED = "disabled"
INTENT_FAILED = "failed"


class IntentError(RuntimeError):
    pass


class IntentRunMismatch(IntentError):
    def __init__(self, existing_run_id: str) -> None:
        super().__init__("sandbox profile intent belongs to a different run")
        self.existing_run_id = existing_run_id


def intent_path(journal_path: str | Path) -> Path:
    return Path(journal_path) / INTENT_REL


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _base_capability(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "intent_state": INTENT_PREPARED,
        "prepared_at": None,
        "apply_started_at": None,
        "applied_at": None,
        "disable_started_at": None,
        "disabled_at": None,
        "residuals": [],
    }


def _default_intent(run_id: str) -> dict[str, Any]:
    now = _now_iso()
    capabilities = []
    for name in manifest.CAPABILITY_ORDER:
        item = _base_capability(name)
        item["prepared_at"] = now
        capabilities.append(item)
    return {
        "kind": manifest.INTENT_KIND,
        "contract_version": manifest.CONTRACT_VERSION,
        "run_id": run_id,
        "profile": manifest.PROFILE,
        "status": INTENT_PREPARED,
        "created_at": now,
        "updated_at": now,
        "capabilities": capabilities,
        "observed_at_apply": {},
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(path, payload, indent=2, mode=0o600)


def load_intent(journal_path: str | Path) -> dict[str, Any] | None:
    path = intent_path(journal_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise IntentError("sandbox profile intent is unreadable") from exc
    if not isinstance(payload, dict):
        raise IntentError("sandbox profile intent is malformed")
    return payload


def require_intent(journal_path: str | Path, run_id: str) -> dict[str, Any]:
    payload = load_intent(journal_path)
    if payload is None:
        raise FileNotFoundError("sandbox profile intent is missing")
    return _validate_intent_payload(payload, run_id)


def _validate_intent_payload(payload: dict[str, Any], run_id: str) -> dict[str, Any]:
    existing = payload.get("run_id")
    if existing != run_id:
        raise IntentRunMismatch(str(existing) if isinstance(existing, str) else "")
    if payload.get("kind") != manifest.INTENT_KIND:
        raise IntentError("sandbox profile intent kind is unsupported")
    if payload.get("contract_version") != manifest.CONTRACT_VERSION:
        raise IntentError("sandbox profile intent contract is unsupported")
    if payload.get("profile") != manifest.PROFILE:
        raise IntentError("sandbox profile intent profile is unsupported")
    return payload


def ensure_prepared(journal_path: str | Path, run_id: str) -> dict[str, Any]:
    existing = load_intent(journal_path)
    if existing is not None:
        return _validate_intent_payload(existing, run_id)
    payload = _default_intent(run_id)
    _write(intent_path(journal_path), payload)
    return payload


def _capability(payload: dict[str, Any], name: str) -> dict[str, Any]:
    caps = payload.get("capabilities")
    if not isinstance(caps, list):
        raise IntentError("sandbox profile intent capabilities are malformed")
    for item in caps:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    item = _base_capability(name)
    caps.append(item)
    return item


def update_capability(
    journal_path: str | Path,
    run_id: str,
    name: str,
    *,
    state: str,
    residuals: tuple[str, ...] = (),
    observed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = copy.deepcopy(require_intent(journal_path, run_id))
    now = _now_iso()
    cap = _capability(payload, name)
    cap["intent_state"] = state
    cap["residuals"] = list(residuals)
    if state == INTENT_APPLY_STARTED:
        cap["apply_started_at"] = cap.get("apply_started_at") or now
        payload["status"] = "applying"
    elif state == INTENT_APPLIED:
        cap["applied_at"] = cap.get("applied_at") or now
    elif state == INTENT_DISABLE_STARTED:
        cap["disable_started_at"] = cap.get("disable_started_at") or now
        payload["status"] = "disabling"
    elif state == INTENT_DISABLED:
        cap["disabled_at"] = cap.get("disabled_at") or now
    elif state == INTENT_FAILED:
        payload["status"] = INTENT_FAILED
    if observed is not None:
        observed_block = payload.get("observed_at_apply")
        if not isinstance(observed_block, dict):
            observed_block = {}
            payload["observed_at_apply"] = observed_block
        observed_block[name] = observed
    payload["updated_at"] = now
    _write(intent_path(journal_path), payload)
    return payload


def mark_disabled_if_complete(journal_path: str | Path, run_id: str) -> dict[str, Any]:
    payload = copy.deepcopy(require_intent(journal_path, run_id))
    now = _now_iso()
    payload["status"] = INTENT_DISABLED
    payload["updated_at"] = now
    for name in manifest.CAPABILITY_ORDER:
        cap = _capability(payload, name)
        if name == manifest.CAPABILITY_RUNTIME:
            continue
        cap["intent_state"] = INTENT_DISABLED
        cap["disabled_at"] = cap.get("disabled_at") or now
    _write(intent_path(journal_path), payload)
    return payload
