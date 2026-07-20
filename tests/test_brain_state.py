# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from solstone.think.journal_io import atomic_replace
from solstone.think.providers import brain_state as brain_state_module
from solstone.think.providers.brain_state import (
    BRAIN_AGGREGATE_STATES,
    BRAIN_COMPONENT_STATUSES,
    BRAIN_REASON_CODES,
    BRAIN_REASON_TO_AGGREGATE,
    CHECKING_TTL,
    DEFAULT_READY_EVIDENCE_TTL,
    BrainProbeOutcome,
    BrainStateConflictError,
    BrainStateValidationError,
    abandon_brain_refresh,
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
BUNDLED_RUNTIME_FINGERPRINT = "b" * 64


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


def _bundled_config() -> dict[str, Any]:
    return {
        "providers": {"active": {"provider": "local", "model": "local/bundled"}},
        "env": {},
    }


def _runtime_inspection(
    *,
    status: str = "ok",
    phase: str = "ready",
    desired: str | None = BUNDLED_RUNTIME_FINGERPRINT,
    record_present: bool = True,
) -> dict[str, Any]:
    record = (
        {
            "schema_version": 1,
            "provider": "local",
            "revision": 1,
            "phase": phase,
            "reason_code": None,
            "detail": {},
            "desired_fingerprint_sha256": desired,
            "incarnation": None,
            "generation": 1,
            "attempt": 0,
            "process": None,
            "updated_at": NOW.isoformat(),
            "display_deadline_at": None,
            "owner": None,
        }
        if record_present
        else None
    )
    return {
        "status": status,
        "provider": "local",
        "record_kind": "health",
        "path": "/tmp/runtime/local.json",
        "record": record,
        "reason_code": None,
        "error": None,
    }


def _patch_bundled_runtime(
    monkeypatch: pytest.MonkeyPatch,
    desired: str = BUNDLED_RUNTIME_FINGERPRINT,
) -> None:
    monkeypatch.setattr(
        brain_state_module, "_bundled_runtime_fingerprint_sha", lambda: desired
    )


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


def _read_raw_record(journal: Path) -> dict[str, Any]:
    return json.loads(
        brain_state_path(journal_path=journal).read_text(encoding="utf-8")
    )


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


def test_checking_cross_field_invariant_rejects_mismatches(tmp_path: Path) -> None:
    _write_config(tmp_path, _cloud_config())
    permit = begin_brain_refresh(NOW, journal_path=tmp_path)
    assert permit is not None
    try:
        raw = _read_raw_record(tmp_path)
        raw["checking"] = None
        with pytest.raises(BrainStateValidationError):
            validate_brain_state_record(raw)

        raw = _read_raw_record(tmp_path)
        raw["aggregate_state"] = "unknown"
        raw["reason_code"] = "runtime_not_ready"
        with pytest.raises(BrainStateValidationError):
            validate_brain_state_record(raw)
    finally:
        permit.release()


def test_runtime_failure_ingress_rejects_checking_reason(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        record_brain_runtime_failure("checking_active", NOW, journal_path=tmp_path)


def test_diagnostic_string_must_be_declared_enum(tmp_path: Path) -> None:
    with pytest.raises(BrainStateValidationError):
        record_brain_runtime_failure(
            "runtime_failed",
            NOW,
            diagnostic={"phase": "sk-secret-credential"},
            journal_path=tmp_path,
        )


def test_bundled_runtime_health_ready_allows_ready_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bundled_runtime(monkeypatch)
    config = _bundled_config()
    _write_ready_record(tmp_path, config)
    record = validate_brain_state_record(_read_raw_record(tmp_path))
    key = brain_fingerprint_key_path(journal_path=tmp_path).read_bytes()

    projection = project_brain_state(
        record,
        NOW,
        config=config,
        hmac_key=key,
        refresh_permit_active=False,
        runtime_health=_runtime_inspection(),
    )

    assert projection["aggregate_state"] == "ready"
    assert projection["reason_code"] is None


def test_inspect_brain_state_injects_bundled_runtime_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bundled_runtime(monkeypatch)
    config = _bundled_config()
    _write_ready_record(tmp_path, config)
    calls: list[tuple[str, Path | None]] = []

    def fake_inspect_runtime_health(
        provider: str, *, journal_path: str | Path | None = None
    ) -> dict[str, Any]:
        calls.append(
            (provider, Path(journal_path) if journal_path is not None else None)
        )
        return _runtime_inspection()

    monkeypatch.setattr(
        brain_state_module, "inspect_runtime_health", fake_inspect_runtime_health
    )

    inspection = inspect_brain_state(NOW, journal_path=tmp_path)

    assert calls == [("local", tmp_path)]
    assert inspection["projection"]["aggregate_state"] == "ready"


def test_bundled_runtime_health_non_ready_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bundled_runtime(monkeypatch)
    config = _bundled_config()
    _write_ready_record(tmp_path, config)
    record = validate_brain_state_record(_read_raw_record(tmp_path))
    key = brain_fingerprint_key_path(journal_path=tmp_path).read_bytes()
    cases = [
        (
            _runtime_inspection(status="corrupt", record_present=False),
            "runtime_state_unavailable",
        ),
        (
            _runtime_inspection(status="unavailable", record_present=False),
            "runtime_state_unavailable",
        ),
        (
            _runtime_inspection(phase="stopped", desired=None),
            "runtime_not_ready",
        ),
        (
            _runtime_inspection(phase="ready-proof-unavailable"),
            "runtime_ready_proof_unavailable",
        ),
        (
            _runtime_inspection(phase="starting"),
            "runtime_not_ready",
        ),
        (
            _runtime_inspection(phase="ready", desired="c" * 64),
            "runtime_state_unavailable",
        ),
    ]

    for runtime_health, expected_reason in cases:
        projection = project_brain_state(
            record,
            NOW,
            config=config,
            hmac_key=key,
            refresh_permit_active=False,
            runtime_health=runtime_health,
        )
        assert projection["aggregate_state"] != "ready"
        assert projection["reason_code"] == expected_reason


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
        runtime_health=None,
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


def test_accepted_mutations_increment_revision_exactly_once(tmp_path: Path) -> None:
    _write_config(tmp_path, _cloud_config())

    permit = begin_brain_refresh(NOW, journal_path=tmp_path)
    assert permit is not None
    record = _read_raw_record(tmp_path)
    assert record["revision"] == 1

    finished = finish_brain_refresh(
        permit, _ready_outcome(), NOW, journal_path=tmp_path
    )
    assert finished["revision"] == 2

    failure = record_brain_runtime_failure(
        "runtime_failed",
        NOW,
        diagnostic={"phase": "failed"},
        journal_path=tmp_path,
    )
    assert failure["revision"] == 3

    newer = begin_brain_refresh(NOW + timedelta(seconds=1), journal_path=tmp_path)
    assert newer is not None
    record = _read_raw_record(tmp_path)
    assert record["revision"] == 4

    abandoned = abandon_brain_refresh(
        newer,
        "runtime_not_ready",
        NOW + timedelta(seconds=2),
        diagnostic={"phase": "starting"},
        journal_path=tmp_path,
    )
    assert abandoned["revision"] == 5


def test_rejected_finalization_does_not_increment_revision(tmp_path: Path) -> None:
    _write_config(tmp_path, _cloud_config())
    permit = begin_brain_refresh(NOW, journal_path=tmp_path)
    assert permit is not None
    before = _read_raw_record(tmp_path)["revision"]
    permit.run_id = "not-the-recorded-run"

    with pytest.raises(BrainStateConflictError):
        finish_brain_refresh(permit, _ready_outcome(), NOW, journal_path=tmp_path)

    assert _read_raw_record(tmp_path)["revision"] == before


def test_finalization_rejects_mismatched_run_id(tmp_path: Path) -> None:
    _write_config(tmp_path, _cloud_config())
    permit = begin_brain_refresh(NOW, journal_path=tmp_path)
    assert permit is not None
    permit.run_id = "different-run"

    with pytest.raises(BrainStateConflictError):
        finish_brain_refresh(permit, _ready_outcome(), NOW, journal_path=tmp_path)


def test_finalization_rejects_mismatched_revision(tmp_path: Path) -> None:
    _write_config(tmp_path, _cloud_config())
    permit = begin_brain_refresh(NOW, journal_path=tmp_path)
    assert permit is not None
    permit.checking_revision += 1

    with pytest.raises(BrainStateConflictError):
        finish_brain_refresh(permit, _ready_outcome(), NOW, journal_path=tmp_path)


def test_finalization_rejects_mismatched_fingerprint(tmp_path: Path) -> None:
    _write_config(tmp_path, _cloud_config())
    permit = begin_brain_refresh(NOW, journal_path=tmp_path)
    assert permit is not None
    permit.fingerprint_sha256 = "f" * 64

    with pytest.raises(BrainStateConflictError):
        finish_brain_refresh(permit, _ready_outcome(), NOW, journal_path=tmp_path)


def test_run_identity_unique_and_expiry_is_exact_ten_minutes(tmp_path: Path) -> None:
    _write_config(tmp_path, _cloud_config())
    first = begin_brain_refresh(NOW, journal_path=tmp_path)
    assert first is not None
    first.release()
    second = begin_brain_refresh(NOW + timedelta(seconds=1), journal_path=tmp_path)
    assert second is not None
    try:
        assert first.run_id != second.run_id
        assert first.expires_at == first.started_at + CHECKING_TTL
        assert second.expires_at == second.started_at + CHECKING_TTL
    finally:
        second.release()


def test_brain_atomic_failure_preserves_prior_record_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_ready_record(tmp_path, _cloud_config())
    state_path = brain_state_path(journal_path=tmp_path)
    health_dir = state_path.parent
    prior_bytes = state_path.read_bytes()
    prior_revision = _read_raw_record(tmp_path)["revision"]
    real_replace = brain_state_module.atomic_replace.__globals__["os"].replace

    def fail_brain_replace(src: str, dst: str) -> None:
        if Path(dst).name == "brain.json":
            raise OSError("brain replace failed")
        real_replace(src, dst)

    monkeypatch.setattr(
        "solstone.think.journal_io.atomic.os.replace", fail_brain_replace
    )

    with pytest.raises(OSError):
        record_brain_runtime_failure(
            "runtime_failed",
            NOW,
            diagnostic={"phase": "failed"},
            journal_path=tmp_path,
        )

    assert state_path.read_bytes() == prior_bytes
    assert _read_raw_record(tmp_path)["revision"] == prior_revision
    assert list(health_dir.glob(".tmp_*")) == []


def test_failed_checking_commit_performs_zero_provider_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_ready_record(tmp_path, _cloud_config())
    real_replace = brain_state_module.atomic_replace.__globals__["os"].replace
    provider_calls: list[str] = []

    def fail_brain_replace(src: str, dst: str) -> None:
        if Path(dst).name == "brain.json":
            raise OSError("brain replace failed")
        real_replace(src, dst)

    def provider_work() -> None:
        provider_calls.append("called")

    monkeypatch.setattr(
        "solstone.think.journal_io.atomic.os.replace", fail_brain_replace
    )

    with pytest.raises(OSError):
        permit = begin_brain_refresh(NOW + timedelta(seconds=1), journal_path=tmp_path)
        if permit is not None:
            provider_work()
            permit.release()

    assert provider_calls == []
    assert list((tmp_path / "health").glob(".tmp_*")) == []


def test_future_timestamp_never_projects_ready(tmp_path: Path) -> None:
    _write_ready_record(tmp_path, _cloud_config())
    raw = _read_raw_record(tmp_path)
    assert raw["evidence"]["configuration"] is not None
    raw["evidence"]["configuration"]["observed_at"] = (
        NOW + timedelta(minutes=1)
    ).isoformat()
    atomic_replace(
        brain_state_path(journal_path=tmp_path),
        json.dumps(raw),
        mode=0o600,
    )

    projection = inspect_brain_state(NOW, journal_path=tmp_path)["projection"]

    assert projection["aggregate_state"] != "ready"
    assert projection["reason_code"] == "clock_skew_detected"


def test_naive_timestamp_never_projects_ready(tmp_path: Path) -> None:
    _write_ready_record(tmp_path, _cloud_config())
    raw = _read_raw_record(tmp_path)
    assert raw["evidence"]["configuration"] is not None
    raw["evidence"]["configuration"]["observed_at"] = "2026-01-02T03:04:05"
    atomic_replace(
        brain_state_path(journal_path=tmp_path),
        json.dumps(raw),
        mode=0o600,
    )

    inspection = inspect_brain_state(NOW, journal_path=tmp_path)

    assert inspection["projection"]["aggregate_state"] != "ready"
    assert inspection["reason_code"] == "record_malformed"


def test_internally_inconsistent_timestamp_never_projects_ready(
    tmp_path: Path,
) -> None:
    _write_ready_record(tmp_path, _cloud_config())
    raw = _read_raw_record(tmp_path)
    assert raw["evidence"]["configuration"] is not None
    raw["evidence"]["configuration"]["expires_at"] = (
        NOW - timedelta(hours=1)
    ).isoformat()
    atomic_replace(
        brain_state_path(journal_path=tmp_path),
        json.dumps(raw),
        mode=0o600,
    )

    projection = inspect_brain_state(NOW, journal_path=tmp_path)["projection"]

    assert projection["aggregate_state"] != "ready"
    assert projection["reason_code"] == "evidence_expired"


def test_clock_rollback_never_projects_ready(tmp_path: Path) -> None:
    _write_ready_record(tmp_path, _cloud_config())

    projection = inspect_brain_state(NOW - timedelta(seconds=1), journal_path=tmp_path)[
        "projection"
    ]

    assert projection["aggregate_state"] != "ready"
    assert projection["reason_code"] == "clock_skew_detected"


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
