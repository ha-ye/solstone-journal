# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hmac
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from solstone.think.journal_io import atomic_replace
from solstone.think.providers.brain_state import (
    BRAIN_AGGREGATE_STATES,
    BRAIN_COMPONENT_STATUSES,
    BRAIN_REASON_CODES,
    BRAIN_REASON_TO_AGGREGATE,
    DEFAULT_READY_EVIDENCE_TTL,
    BrainProbeOutcome,
    BrainStateConflictError,
    BrainStateValidationError,
    begin_brain_refresh,
    brain_fingerprint_key_path,
    brain_state_path,
    build_active_brain_fingerprint,
    finish_brain_refresh,
    inspect_brain_state,
    project_brain_state,
    record_brain_runtime_failure,
    validate_brain_state_record,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _write_config(journal: Path, config: dict[str, Any]) -> None:
    path = journal / "config" / "journal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding="utf-8")


def _cloud_config(key: str = "config-secret", model: str = "gpt-5") -> dict[str, Any]:
    return {
        "providers": {"active": {"provider": "openai", "model": model}},
        "env": {"OPENAI_API_KEY": key},
    }


def _local_endpoint_config() -> dict[str, Any]:
    return {
        "providers": {
            "active": {"provider": "local", "model": "local/custom"},
            "local": {
                "endpoint_url": "https://brain.example.test/v1",
                "served_model_id": "served-model",
                "credential": "endpoint-secret",
            },
        },
        "env": {},
    }


def _component(
    now: datetime = NOW, *, expires_at: datetime | None = None
) -> dict[str, Any]:
    return {
        "status": "ok",
        "observed_at": now.isoformat(),
        "expires_at": (expires_at or now + DEFAULT_READY_EVIDENCE_TTL).isoformat(),
    }


def _ready_outcome(now: datetime = NOW) -> BrainProbeOutcome:
    return {
        "configuration": _component(now),
        "lane_prerequisites": _component(now),
        "generate": _component(now),
        "cogitate": _component(now),
    }


def _deep_values(value: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.append(key)
            found.extend(_deep_values(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_deep_values(child))
    else:
        found.append(value)
    return found


def _write_ready_record(journal: Path, config: dict[str, Any]) -> None:
    _write_config(journal, config)
    permit = begin_brain_refresh(NOW, journal_path=journal)
    assert permit is not None
    finish_brain_refresh(permit, _ready_outcome(), NOW, journal_path=journal)


def test_vocabularies_and_reason_mapping_are_closed() -> None:
    assert BRAIN_AGGREGATE_STATES == {
        "ready",
        "checking",
        "blocked",
        "unhealthy",
        "unknown",
    }
    assert BRAIN_COMPONENT_STATUSES == {
        "ok",
        "blocked",
        "failed",
        "unknown",
        "not_attempted",
    }
    assert not (BRAIN_REASON_CODES & BRAIN_AGGREGATE_STATES)
    assert not (BRAIN_REASON_CODES & BRAIN_COMPONENT_STATUSES)
    assert set(BRAIN_REASON_TO_AGGREGATE) == BRAIN_REASON_CODES
    assert set(BRAIN_REASON_TO_AGGREGATE.values()) <= BRAIN_AGGREGATE_STATES


def test_closed_schema_rejects_lanes_matrix(tmp_path: Path) -> None:
    _write_ready_record(tmp_path, _cloud_config())
    raw = json.loads(
        brain_state_path(journal_path=tmp_path).read_text(encoding="utf-8")
    )
    raw["lanes"] = {}

    with pytest.raises(BrainStateValidationError):
        validate_brain_state_record(raw)


def test_none_lane_is_blocked_thinking_engine_not_chosen(tmp_path: Path) -> None:
    _write_config(tmp_path, {"providers": {"active": {"provider": "none"}}, "env": {}})

    assert begin_brain_refresh(NOW, journal_path=tmp_path) is None
    projection = inspect_brain_state(NOW, journal_path=tmp_path)["projection"]

    assert projection["active_lane"] == "none"
    assert projection["aggregate_state"] == "blocked"
    assert projection["reason_code"] == "thinking_engine_not_chosen"


def test_fingerprint_uses_config_env_not_process_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = b"k" * 32
    config = _cloud_config(key="from-config")
    monkeypatch.setenv("OPENAI_API_KEY", "from-process")

    first = build_active_brain_fingerprint(config, hmac_key=key)
    monkeypatch.setenv("OPENAI_API_KEY", "changed-process")
    second = build_active_brain_fingerprint(config, hmac_key=key)
    changed_config = _cloud_config(key="changed-config")
    third = build_active_brain_fingerprint(changed_config, hmac_key=key)

    assert first["fingerprint_sha256"] == second["fingerprint_sha256"]
    assert first["fingerprint_sha256"] != third["fingerprint_sha256"]


def test_ready_evidence_expires_at_boundary_is_not_ready(tmp_path: Path) -> None:
    _write_config(tmp_path, _cloud_config())
    permit = begin_brain_refresh(NOW, journal_path=tmp_path)
    assert permit is not None
    outcome = _ready_outcome(NOW)
    for component in outcome.values():
        assert component is not None
        component["expires_at"] = NOW.isoformat()

    finish_brain_refresh(permit, outcome, NOW, journal_path=tmp_path)
    projection = inspect_brain_state(NOW, journal_path=tmp_path)["projection"]

    assert projection["aggregate_state"] == "unknown"
    assert projection["reason_code"] == "evidence_expired"


def test_ok_evidence_requires_expires_at() -> None:
    outcome = _ready_outcome()
    assert outcome["configuration"] is not None
    del outcome["configuration"]["expires_at"]

    with pytest.raises(BrainStateValidationError):
        validate_brain_state_record(
            {
                "schema_version": 1,
                "revision": 1,
                "aggregate_state": "ready",
                "reason_code": None,
                "active_lane": "byo-cloud",
                "active_provider": "openai",
                "active_model": "gpt-5",
                "fingerprint_sha256": "a" * 64,
                "checking": None,
                "evidence": outcome,
                "runtime_failure_marker": None,
                "diagnostic": {},
                "updated_at": NOW.isoformat(),
            }
        )


def test_config_change_invalidates_prior_ready_without_refresh(tmp_path: Path) -> None:
    _write_ready_record(tmp_path, _cloud_config(key="first"))
    record = inspect_brain_state(NOW, journal_path=tmp_path)["record"]
    assert record is not None
    key = brain_fingerprint_key_path(journal_path=tmp_path).read_bytes()

    projection = project_brain_state(
        record,
        NOW,
        config=_cloud_config(key="second"),
        hmac_key=key,
        refresh_permit_active=False,
    )

    assert projection["aggregate_state"] == "unknown"
    assert projection["reason_code"] == "stale_result_ignored"


def test_key_replacement_invalidates_prior_ready_record(tmp_path: Path) -> None:
    _write_ready_record(tmp_path, _cloud_config())
    inspection = inspect_brain_state(NOW, journal_path=tmp_path)
    assert inspection["projection"]["aggregate_state"] == "ready"

    key_path = brain_fingerprint_key_path(journal_path=tmp_path)
    key_path.unlink()
    atomic_replace(key_path, b"z" * 32, mode=0o600)
    projection = inspect_brain_state(NOW, journal_path=tmp_path)["projection"]

    assert projection["aggregate_state"] == "unknown"
    assert projection["reason_code"] == "stale_result_ignored"


def test_mode_repair_uses_atomic_replace_for_brain_and_key(tmp_path: Path) -> None:
    _write_ready_record(tmp_path, _cloud_config())
    key_path = brain_fingerprint_key_path(journal_path=tmp_path)
    state_path = brain_state_path(journal_path=tmp_path)
    key_path.chmod(0o644)
    state_path.chmod(0o644)

    permit = begin_brain_refresh(NOW, journal_path=tmp_path)
    assert permit is not None
    permit.release()

    assert (key_path.stat().st_mode & 0o777) == 0o600
    assert (state_path.stat().st_mode & 0o777) == 0o600


def test_runtime_failure_survives_older_refresh_finalization(tmp_path: Path) -> None:
    _write_config(tmp_path, _cloud_config())
    permit = begin_brain_refresh(NOW, journal_path=tmp_path)
    assert permit is not None

    failure = record_brain_runtime_failure(
        "runtime_failed",
        NOW,
        diagnostic={"phase": "failed"},
        journal_path=tmp_path,
    )
    assert failure["aggregate_state"] == "unhealthy"

    with pytest.raises(BrainStateConflictError):
        finish_brain_refresh(permit, _ready_outcome(), NOW, journal_path=tmp_path)

    projection = inspect_brain_state(NOW, journal_path=tmp_path)["projection"]
    assert projection["aggregate_state"] == "unhealthy"
    assert projection["reason_code"] == "runtime_failed"

    newer = begin_brain_refresh(NOW, journal_path=tmp_path)
    assert newer is not None
    finish_brain_refresh(newer, _ready_outcome(), NOW, journal_path=tmp_path)
    assert (
        inspect_brain_state(NOW, journal_path=tmp_path)["projection"]["aggregate_state"]
        == "ready"
    )


def test_secret_material_never_persists_in_brain_record(tmp_path: Path) -> None:
    config = _local_endpoint_config()
    _write_ready_record(tmp_path, config)

    key = brain_fingerprint_key_path(journal_path=tmp_path).read_bytes()
    credential = b"endpoint-secret"
    hmac_digest = hmac.digest(key, credential, "sha256").hex()
    raw = json.loads(
        brain_state_path(journal_path=tmp_path).read_text(encoding="utf-8")
    )
    values = {str(value) for value in _deep_values(raw)}

    assert "endpoint-secret" not in values
    assert key.hex() not in values
    assert hmac_digest not in values
    assert "https://brain.example.test/v1" not in values
    assert "https://brain.example.test" not in values
