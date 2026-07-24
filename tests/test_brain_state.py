# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from solstone.think.journal_io import atomic_replace
from solstone.think.models import LOCAL_MODEL
from solstone.think.providers import brain_state as brain_state_module
from solstone.think.providers.brain_state import (
    BRAIN_AGGREGATE_STATES,
    BRAIN_COMPONENT_STATUSES,
    BRAIN_EVIDENCE_REASON_CODES,
    BRAIN_LANES,
    BRAIN_PROJECTION_ONLY_REASON_CODES,
    BRAIN_REASON_CODES,
    BRAIN_REASON_TO_AGGREGATE,
    CHECKING_TTL,
    DEFAULT_READY_EVIDENCE_TTL,
    BrainProbeOutcome,
    BrainStateConflictError,
    BrainStateExpectedFingerprintStaleError,
    BrainStateValidationError,
    abandon_brain_prerequisite_renewal,
    abandon_brain_refresh,
    begin_brain_prerequisite_renewal,
    begin_brain_refresh,
    brain_fingerprint_key_path,
    brain_state_path,
    build_active_brain_fingerprint,
    finish_brain_prerequisite_renewal,
    finish_brain_refresh,
    inspect_brain_state,
    project_brain_state,
    record_brain_runtime_failure,
    runtime_phase_reason,
    validate_brain_state_record,
)
from solstone.think.providers.runtime_health import runtime_health_path

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


def _spp_config(
    *,
    prior_model: str = "gemini-flash-latest",
    account_id: str = "acct-a",
) -> dict[str, Any]:
    credential = "endpoint-secret"
    endpoint_url = "https://brain.example.test/v1"
    served_model_id = "served-model"
    return {
        "providers": {
            "active": {"provider": "local", "model": LOCAL_MODEL},
            "local": {
                "endpoint_url": endpoint_url,
                "served_model_id": served_model_id,
                "credential": credential,
            },
        },
        "services": {
            "confidential": {
                "enabled_at": "2026-01-02T03:04:05+00:00",
                "account_id": account_id,
                "endpoint_url": endpoint_url,
                "served_model_id": served_model_id,
                "credential_created_at": "2026-01-02T03:00:00+00:00",
                "credential_fingerprint_sha256": hashlib.sha256(
                    credential.encode("utf-8")
                ).hexdigest(),
                "prior_active": {"provider": "google", "model": prior_model},
                "prior_local_endpoint": {
                    "endpoint_url": "https://old-local.example.test/v1",
                    "served_model_id": "old-served-model",
                    "credential": "old-secret",
                },
            }
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


def _write_runtime_health_record(
    journal: Path,
    *,
    desired: str | None = BUNDLED_RUNTIME_FINGERPRINT,
    phase: str = "ready",
) -> None:
    runtime_health = _runtime_inspection(phase=phase, desired=desired)
    record = runtime_health["record"]
    assert record is not None
    path = runtime_health_path("local", journal_path=journal)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace(path, json.dumps(record), mode=0o600)


def _current_fingerprint(journal: Path, config: dict[str, Any]) -> str:
    key = brain_fingerprint_key_path(journal_path=journal).read_bytes()
    result = build_active_brain_fingerprint(config, hmac_key=key)
    assert result["fingerprint_sha256"] is not None
    return result["fingerprint_sha256"]


def _health_snapshot(journal: Path) -> dict[str, tuple[int, bytes]]:
    root = journal / "health"
    if not root.exists():
        return {}
    snapshot: dict[str, tuple[int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[path.relative_to(root).as_posix()] = (
                path.stat().st_mtime_ns,
                path.read_bytes(),
            )
    return snapshot


def test_vocabularies_and_reason_mapping_are_closed() -> None:
    expected_reasons = {
        "brain_check_in_progress",
        "thinking_engine_not_chosen",
        "provider_key_missing",
        "endpoint_configuration_incomplete",
        "gpu_unavailable",
        "local_runtime_not_ready",
        "local_artifact_not_ready",
        "attestation_not_verified",
        "nvattest_install_in_progress",
        "nvattest_platform_unsupported",
        "nvattest_unavailable",
        "provider_key_invalid",
        "model_not_found",
        "provider_quota_exceeded",
        "provider_unavailable",
        "network_unreachable",
        "endpoint_unreachable",
        "endpoint_contract_failed",
        "chat_timeout",
        "provider_response_invalid",
        "cogitate_terminal_error",
        "attestation_rejected",
        "attestation_expired",
        "nvattest_install_failed",
        "nvattest_integrity_failed",
        "local_server_unhealthy",
        "configuration_invalid",
        "fingerprint_key_unavailable",
        "brain_record_missing",
        "brain_record_invalid",
        "brain_record_unavailable",
        "brain_record_stale",
        "brain_check_interrupted",
        "brain_config_changed",
        "brain_run_superseded",
        "probe_internal_error",
        "probe_output_starved",
        "local_runtime_state_invalid",
        "local_runtime_state_unavailable",
        "local_runtime_state_stale",
        "local_runtime_fingerprint_mismatch",
    }
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
    assert BRAIN_LANES == {"none", "bundled", "spp", "byo-cloud", "byo-endpoint"}
    assert BRAIN_REASON_CODES == expected_reasons
    assert not (BRAIN_REASON_CODES & BRAIN_AGGREGATE_STATES)
    assert not (BRAIN_REASON_CODES & BRAIN_COMPONENT_STATUSES)
    assert set(BRAIN_REASON_TO_AGGREGATE) == BRAIN_REASON_CODES
    assert set(BRAIN_REASON_TO_AGGREGATE.values()) <= BRAIN_AGGREGATE_STATES
    evidence_reasons = frozenset().union(*BRAIN_EVIDENCE_REASON_CODES.values())
    assert len(evidence_reasons) == 31
    assert len(BRAIN_PROJECTION_ONLY_REASON_CODES) == 10
    assert evidence_reasons | BRAIN_PROJECTION_ONLY_REASON_CODES == BRAIN_REASON_CODES
    assert not (evidence_reasons & BRAIN_PROJECTION_ONLY_REASON_CODES)


def test_closed_schema_rejects_lanes_matrix(tmp_path: Path) -> None:
    _write_ready_record(tmp_path, _cloud_config())
    raw = json.loads(
        brain_state_path(journal_path=tmp_path).read_text(encoding="utf-8")
    )
    raw["lanes"] = {}

    with pytest.raises(BrainStateValidationError):
        validate_brain_state_record(raw)

    raw = _read_raw_record(tmp_path)
    raw["active_lane"] = "unknown"
    with pytest.raises(BrainStateValidationError):
        validate_brain_state_record(raw)

    raw = _read_raw_record(tmp_path)
    raw["aggregate_state"] = "unhealthy"
    raw["reason_code"] = "runtime_failed"
    with pytest.raises(BrainStateValidationError):
        validate_brain_state_record(raw)


def test_none_lane_is_blocked_thinking_engine_not_chosen(tmp_path: Path) -> None:
    _write_config(tmp_path, {"providers": {"active": {"provider": "none"}}, "env": {}})

    assert begin_brain_refresh(NOW, journal_path=tmp_path) is None
    projection = inspect_brain_state(NOW, journal_path=tmp_path)["projection"]

    assert projection["active_lane"] == "none"
    assert projection["aggregate_state"] == "blocked"
    assert projection["reason_code"] == "thinking_engine_not_chosen"


def test_endpoint_configuration_incomplete_is_evidence_only() -> None:
    evidence = {
        "configuration": {
            "status": "blocked",
            "observed_at": NOW.isoformat(),
            "reason_code": "endpoint_configuration_incomplete",
        },
        "lane_prerequisites": None,
        "generate": None,
        "cogitate": None,
    }

    record = validate_brain_state_record(
        {
            "schema_version": 1,
            "revision": 1,
            "aggregate_state": "blocked",
            "reason_code": "endpoint_configuration_incomplete",
            "active_lane": "byo-endpoint",
            "active_provider": "local",
            "active_model": "local/custom",
            "fingerprint_sha256": "a" * 64,
            "checking": None,
            "evidence": evidence,
            "runtime_failure_marker": None,
            "diagnostic": {},
            "updated_at": NOW.isoformat(),
        }
    )

    assert record["reason_code"] == "endpoint_configuration_incomplete"


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
    assert projection["reason_code"] == "brain_record_stale"


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
        raw["reason_code"] = "local_runtime_not_ready"
        with pytest.raises(BrainStateValidationError):
            validate_brain_state_record(raw)
    finally:
        permit.release()


def test_runtime_failure_ingress_rejects_checking_reason(tmp_path: Path) -> None:
    result = record_brain_runtime_failure(
        "brain_check_in_progress",
        NOW,
        expected_fingerprint_sha256="a" * 64,
        component="lane_prerequisites",
        journal_path=tmp_path,
    )

    assert result["accepted"] is False
    assert result["rejected_reason"] == "reason_not_recordable"


def test_diagnostic_string_must_be_declared_enum(tmp_path: Path) -> None:
    result = record_brain_runtime_failure(
        "local_server_unhealthy",
        NOW,
        expected_fingerprint_sha256="a" * 64,
        component="lane_prerequisites",
        diagnostic={"phase": "sk-secret-credential"},
        journal_path=tmp_path,
    )

    assert result["accepted"] is False
    assert result["rejected_reason"] == "reason_not_recordable"


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


def test_inspect_brain_state_is_passive_host_probe_free_for_bundled_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bundled_runtime(monkeypatch)
    config = _bundled_config()
    _write_ready_record(tmp_path, config)
    _write_runtime_health_record(tmp_path)

    before = _health_snapshot(tmp_path)

    def fail_probe(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("passive bundled inspection must not probe host state")

    from solstone.think.providers import local_cuda, local_install

    monkeypatch.setattr(
        brain_state_module, "_bundled_runtime_fingerprint_sha", fail_probe
    )
    monkeypatch.setattr(local_install, "target_fingerprint", fail_probe)
    monkeypatch.setattr(local_cuda, "resolve_local_backend", fail_probe)
    if brain_state_module.sys.platform == "darwin":
        from solstone.think.providers import mlx_install

        monkeypatch.setattr(mlx_install, "target_fingerprint", fail_probe)

    inspection = inspect_brain_state(NOW, journal_path=tmp_path)

    assert inspection["status"] == "ok"
    assert inspection["projection"]["aggregate_state"] == "ready"
    assert inspection["projection"]["reason_code"] is None
    assert _health_snapshot(tmp_path) == before


def test_inspect_brain_state_uses_supplied_config_without_reread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _cloud_config()
    _write_ready_record(tmp_path, config)
    monkeypatch.setattr(
        brain_state_module,
        "read_journal_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("config reread")
        ),
    )

    inspection = inspect_brain_state(
        NOW,
        journal_path=tmp_path,
        config=config,
    )

    assert inspection["projection"]["aggregate_state"] == "ready"
    assert inspection["projection"]["active_lane"] == "byo-cloud"


def test_inspect_brain_state_faults_do_not_modify_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases: list[tuple[str, Any, str]] = [
        ("config-oserror", OSError("config unavailable"), "configuration_invalid"),
        (
            "config-corrupt",
            brain_state_module.CorruptConfigError(tmp_path / "config" / "journal.json"),
            "configuration_invalid",
        ),
    ]

    for _name, exc, expected_reason in cases:
        monkeypatch.setattr(
            brain_state_module,
            "read_journal_config",
            lambda _journal_path=None, exc=exc: (_ for _ in ()).throw(exc),
        )
        before = _health_snapshot(tmp_path)
        inspection = inspect_brain_state(NOW, journal_path=tmp_path)
        assert inspection["projection"]["aggregate_state"] == "unknown"
        assert inspection["projection"]["reason_code"] == expected_reason
        assert _health_snapshot(tmp_path) == before
        monkeypatch.undo()

    _write_ready_record(tmp_path, _cloud_config())
    before = _health_snapshot(tmp_path)
    monkeypatch.setattr(
        brain_state_module,
        "_read_fingerprint_key",
        lambda _path: (_ for _ in ()).throw(OSError("key unavailable")),
    )
    inspection = inspect_brain_state(NOW, journal_path=tmp_path)
    assert inspection["projection"]["reason_code"] == "fingerprint_key_unavailable"
    assert _health_snapshot(tmp_path) == before
    monkeypatch.undo()

    _write_config(tmp_path, _cloud_config())
    before = _health_snapshot(tmp_path)
    monkeypatch.setattr(
        brain_state_module,
        "read_json",
        lambda _path, **_kwargs: (_ for _ in ()).throw(OSError("record unavailable")),
    )
    inspection = inspect_brain_state(NOW, journal_path=tmp_path)
    assert inspection["projection"]["reason_code"] == "brain_record_unavailable"
    assert _health_snapshot(tmp_path) == before
    monkeypatch.undo()

    _write_config(tmp_path, _cloud_config())
    brain_state_path(journal_path=tmp_path).parent.mkdir(parents=True, exist_ok=True)
    brain_state_path(journal_path=tmp_path).write_bytes(b"{")
    before = _health_snapshot(tmp_path)
    inspection = inspect_brain_state(NOW, journal_path=tmp_path)
    assert inspection["projection"]["reason_code"] == "brain_record_invalid"
    assert _health_snapshot(tmp_path) == before
    brain_state_path(journal_path=tmp_path).unlink()

    _patch_bundled_runtime(monkeypatch)
    _write_ready_record(tmp_path, _bundled_config())
    for runtime_status, expected_reason in (
        ("corrupt", "local_runtime_state_invalid"),
        ("unavailable", "local_runtime_state_unavailable"),
    ):
        before = _health_snapshot(tmp_path)
        monkeypatch.setattr(
            brain_state_module,
            "inspect_runtime_health",
            lambda _provider, *, journal_path=None, runtime_status=runtime_status: (
                _runtime_inspection(
                    status=runtime_status,
                    record_present=False,
                )
            ),
        )
        inspection = inspect_brain_state(NOW, journal_path=tmp_path)
        assert inspection["projection"]["reason_code"] == expected_reason
        assert _health_snapshot(tmp_path) == before
        monkeypatch.undo()
        _patch_bundled_runtime(monkeypatch)

    _write_config(tmp_path, _cloud_config())
    permit = begin_brain_refresh(NOW, journal_path=tmp_path)
    assert permit is not None
    try:
        before = _health_snapshot(tmp_path)
        monkeypatch.setattr(
            brain_state_module,
            "probe_file_lease_held",
            lambda _path: (_ for _ in ()).throw(OSError("lease unavailable")),
        )
        inspection = inspect_brain_state(NOW, journal_path=tmp_path)
        assert inspection["projection"]["reason_code"] == "brain_check_interrupted"
        assert _health_snapshot(tmp_path) == before
    finally:
        permit.release()


@pytest.mark.parametrize(
    ("config", "expected_lane"),
    [
        (_cloud_config(), "byo-cloud"),
        (_local_endpoint_config(), "byo-endpoint"),
    ],
)
def test_non_bundled_inspection_never_reads_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, Any],
    expected_lane: str,
) -> None:
    _write_ready_record(tmp_path, config)

    def fail_runtime(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("runtime inspector should not be called")

    monkeypatch.setattr(brain_state_module, "inspect_runtime_health", fail_runtime)

    inspection = inspect_brain_state(NOW, journal_path=tmp_path)

    assert inspection["projection"]["aggregate_state"] == "ready"
    assert inspection["projection"]["active_lane"] == expected_lane


def test_bundled_runtime_health_phase_lattice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bundled_runtime(monkeypatch)
    config = _bundled_config()
    _write_ready_record(tmp_path, config)
    record = validate_brain_state_record(_read_raw_record(tmp_path))
    key = brain_fingerprint_key_path(journal_path=tmp_path).read_bytes()
    cases = {
        "not-desired": "local_runtime_not_ready",
        "observing": "local_runtime_not_ready",
        "artifact-not-ready": "local_artifact_not_ready",
        "host-blocked": "local_runtime_not_ready",
        "starting": "local_runtime_not_ready",
        "warming": "local_runtime_not_ready",
        "backoff": "local_runtime_not_ready",
        "retry-requested": "local_runtime_not_ready",
        "ready": None,
        "ready-proof-unavailable": "local_runtime_state_unavailable",
        "stop-deferred": "local_runtime_not_ready",
        "stopping": "local_runtime_not_ready",
        "stopped": "local_runtime_not_ready",
        "failed": "local_server_unhealthy",
        "cleanup-failed": "local_server_unhealthy",
        "state-corrupt": "local_runtime_state_invalid",
        "state-unavailable": "local_runtime_state_unavailable",
    }

    for phase, expected_reason in cases.items():
        projection = project_brain_state(
            record,
            NOW,
            config=config,
            hmac_key=key,
            refresh_permit_active=False,
            runtime_health=_runtime_inspection(phase=phase),
        )
        assert runtime_phase_reason(phase) == expected_reason
        assert projection["reason_code"] == expected_reason
        assert (projection["aggregate_state"] == "ready") is (expected_reason is None)


def test_bundled_runtime_health_reason_overrides_and_disagreements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bundled_runtime(monkeypatch)
    config = _bundled_config()
    _write_ready_record(tmp_path, config)
    record = validate_brain_state_record(_read_raw_record(tmp_path))
    key = brain_fingerprint_key_path(journal_path=tmp_path).read_bytes()
    cases = [
        ("host-blocked", "gpu-unavailable", "gpu_unavailable", "blocked", False),
        (
            "host-blocked",
            "gpu-probe-failed",
            "local_runtime_state_unavailable",
            "unknown",
            False,
        ),
        (
            "artifact-not-ready",
            "artifact-missing",
            "local_artifact_not_ready",
            "blocked",
            False,
        ),
        (
            "artifact-not-ready",
            "install-in-progress",
            "local_runtime_not_ready",
            "blocked",
            True,
        ),
        ("ready", "artifact-missing", "local_runtime_state_invalid", "unknown", False),
        ("ready", None, None, "ready", False),
    ]

    for (
        phase,
        runtime_reason,
        expected_reason,
        expected_state,
        expected_progressing,
    ) in cases:
        runtime_health = _runtime_inspection(phase=phase)
        assert runtime_health["record"] is not None
        runtime_health["record"]["reason_code"] = runtime_reason
        projection = project_brain_state(
            record,
            NOW,
            config=config,
            hmac_key=key,
            refresh_permit_active=False,
            runtime_health=runtime_health,
        )
        assert projection["reason_code"] == expected_reason
        assert projection["aggregate_state"] == expected_state
        assert projection["runtime_transition_in_progress"] is expected_progressing


def test_bundled_runtime_health_invalid_and_unavailable_records(
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
            "local_runtime_state_invalid",
        ),
        (
            _runtime_inspection(status="unavailable", record_present=False),
            "local_runtime_state_unavailable",
        ),
        (
            _runtime_inspection(phase="ready", desired=None),
            "local_runtime_state_invalid",
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
        assert projection["aggregate_state"] == "unknown"
        assert projection["reason_code"] == expected_reason


def test_bundled_runtime_desired_change_invalidates_old_brain_evidence(
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
        runtime_health=_runtime_inspection(desired="c" * 64),
    )

    assert projection["aggregate_state"] == "unknown"
    assert projection["reason_code"] == "brain_config_changed"


def test_runtime_transition_in_progress_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_bundled_runtime(monkeypatch)
    config = _bundled_config()
    _write_ready_record(tmp_path, config)
    record = validate_brain_state_record(_read_raw_record(tmp_path))
    key = brain_fingerprint_key_path(journal_path=tmp_path).read_bytes()
    for phase in ("observing", "starting", "warming", "retry-requested"):
        projection = project_brain_state(
            record,
            NOW,
            config=config,
            hmac_key=key,
            refresh_permit_active=False,
            runtime_health=_runtime_inspection(phase=phase),
        )
        assert projection["runtime_transition_in_progress"] is True

    runtime_health = _runtime_inspection(phase="artifact-not-ready")
    assert runtime_health["record"] is not None
    runtime_health["record"]["reason_code"] = "install-in-progress"
    projection = project_brain_state(
        record,
        NOW,
        config=config,
        hmac_key=key,
        refresh_permit_active=False,
        runtime_health=runtime_health,
    )
    assert projection["runtime_transition_in_progress"] is True

    projection = project_brain_state(
        record,
        NOW,
        config=config,
        hmac_key=key,
        refresh_permit_active=False,
        runtime_health=_runtime_inspection(phase="backoff"),
    )
    assert projection["runtime_transition_in_progress"] is False

    cloud_config = _cloud_config()
    _write_ready_record(tmp_path, cloud_config)
    cloud_record = validate_brain_state_record(_read_raw_record(tmp_path))
    cloud_key = brain_fingerprint_key_path(journal_path=tmp_path).read_bytes()
    projection = project_brain_state(
        cloud_record,
        NOW,
        config=cloud_config,
        hmac_key=cloud_key,
        refresh_permit_active=False,
        runtime_health=_runtime_inspection(phase="starting"),
    )
    assert projection["runtime_transition_in_progress"] is False


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
    assert projection["reason_code"] == "brain_config_changed"


def test_spp_fingerprint_ignores_confidential_restore_snapshots(
    tmp_path: Path,
) -> None:
    config = _spp_config(prior_model="gemini-flash-latest")
    _write_ready_record(tmp_path, config)
    record = validate_brain_state_record(_read_raw_record(tmp_path))
    key = brain_fingerprint_key_path(journal_path=tmp_path).read_bytes()
    before = build_active_brain_fingerprint(config, hmac_key=key)

    pinned = json.loads(json.dumps(config))
    confidential = pinned["services"]["confidential"]
    confidential["prior_active"] = {
        "provider": "google",
        "model": "gemini-3.5-flash",
    }
    confidential["prior_local_endpoint"] = {
        "endpoint_url": "https://replacement.example.test/v1",
        "served_model_id": "replacement-served-model",
        "credential": "replacement-secret",
    }
    after = build_active_brain_fingerprint(pinned, hmac_key=key)

    assert before["active_lane"] == "spp"
    assert after["active_lane"] == "spp"
    assert before["fingerprint_sha256"] == after["fingerprint_sha256"]
    projection = project_brain_state(
        record,
        NOW,
        config=pinned,
        hmac_key=key,
        refresh_permit_active=False,
        runtime_health=None,
    )
    assert projection["aggregate_state"] == "ready"


def test_spp_fingerprint_tracks_confidential_active_provenance() -> None:
    key = b"k" * 32
    before = build_active_brain_fingerprint(
        _spp_config(account_id="acct-a"), hmac_key=key
    )
    after = build_active_brain_fingerprint(
        _spp_config(account_id="acct-b"), hmac_key=key
    )

    assert before["active_lane"] == "spp"
    assert after["active_lane"] == "spp"
    assert before["fingerprint_sha256"] != after["fingerprint_sha256"]


def test_active_model_changes_fingerprint_but_byo_memory_does_not() -> None:
    key = b"k" * 32
    config = {
        "providers": {
            "active": {"provider": "google", "model": "gemini-3.5-flash"},
            "byo_models": {"google": "gemini-flash-latest"},
        },
        "env": {"GOOGLE_API_KEY": "google-secret"},
    }
    active_changed = json.loads(json.dumps(config))
    active_changed["providers"]["active"]["model"] = "gemini-3.1-flash-lite"
    remembered_changed = json.loads(json.dumps(config))
    remembered_changed["providers"]["byo_models"]["google"] = "gemini-3.5-flash"

    before = build_active_brain_fingerprint(config, hmac_key=key)
    active_after = build_active_brain_fingerprint(active_changed, hmac_key=key)
    remembered_after = build_active_brain_fingerprint(remembered_changed, hmac_key=key)

    assert before["fingerprint_sha256"] != active_after["fingerprint_sha256"]
    assert before["fingerprint_sha256"] == remembered_after["fingerprint_sha256"]


def test_unresolved_lane_begin_refresh_does_not_persist(tmp_path: Path) -> None:
    _write_ready_record(tmp_path, _cloud_config())
    state_path = brain_state_path(journal_path=tmp_path)
    before_bytes = state_path.read_bytes()
    before_revision = _read_raw_record(tmp_path)["revision"]
    configs = [
        {"providers": {"active": {"provider": "mystery", "model": "x"}}, "env": {}},
        {
            "providers": {
                "active": {"provider": "local", "model": "local/custom"},
                "local": {"endpoint_url": "https://brain.example.test/v1"},
            },
            "env": {},
        },
        {
            **_local_endpoint_config(),
            "services": {"confidential": {"prior_active": {"provider": "openai"}}},
        },
    ]

    for config in configs:
        _write_config(tmp_path, config)
        assert (
            begin_brain_refresh(NOW + timedelta(seconds=1), journal_path=tmp_path)
            is None
        )
        assert state_path.read_bytes() == before_bytes
        assert _read_raw_record(tmp_path)["revision"] == before_revision


def test_runtime_failure_ingress_rejects_stale_fingerprint_without_write(
    tmp_path: Path,
) -> None:
    config_f1 = _cloud_config(key="first")
    _write_ready_record(tmp_path, config_f1)
    state_path = brain_state_path(journal_path=tmp_path)
    prior_bytes = state_path.read_bytes()
    prior_revision = _read_raw_record(tmp_path)["revision"]
    expected_f1 = _current_fingerprint(tmp_path, config_f1)

    _write_config(tmp_path, _cloud_config(key="second"))
    result = record_brain_runtime_failure(
        "provider_unavailable",
        NOW,
        expected_fingerprint_sha256=expected_f1,
        component="generate",
        journal_path=tmp_path,
    )

    assert result["accepted"] is False
    assert result["rejected_reason"] == "fingerprint_mismatch"
    assert state_path.read_bytes() == prior_bytes
    assert _read_raw_record(tmp_path)["revision"] == prior_revision


def test_begin_refresh_expected_active_fingerprint_stales_after_switch(
    tmp_path: Path,
) -> None:
    original = _spp_config(account_id="acct-a")
    _write_ready_record(tmp_path, original)
    expected = _current_fingerprint(tmp_path, original)
    before_record = brain_state_path(journal_path=tmp_path).read_bytes()
    before_key = brain_fingerprint_key_path(journal_path=tmp_path).read_bytes()
    _write_config(tmp_path, _spp_config(account_id="acct-b"))

    with pytest.raises(BrainStateExpectedFingerprintStaleError):
        begin_brain_refresh(
            NOW + timedelta(seconds=1),
            expected_active_fingerprint_sha256=expected,
            journal_path=tmp_path,
        )

    assert brain_state_path(journal_path=tmp_path).read_bytes() == before_record
    assert brain_fingerprint_key_path(journal_path=tmp_path).read_bytes() == before_key


def test_begin_refresh_expected_absent_fingerprint_bootstraps_key(
    tmp_path: Path,
) -> None:
    _write_config(tmp_path, _spp_config())
    assert not brain_fingerprint_key_path(journal_path=tmp_path).exists()

    permit = begin_brain_refresh(
        NOW,
        expect_active_fingerprint_absent=True,
        journal_path=tmp_path,
    )

    assert permit is not None
    assert brain_fingerprint_key_path(journal_path=tmp_path).exists()
    record = finish_brain_refresh(
        permit,
        _ready_outcome(NOW + timedelta(seconds=1)),
        NOW + timedelta(seconds=1),
        journal_path=tmp_path,
    )
    assert record["aggregate_state"] == "ready"
    assert record["active_lane"] == "spp"


def test_begin_refresh_expected_absent_fingerprint_stales_when_key_exists(
    tmp_path: Path,
) -> None:
    _write_ready_record(tmp_path, _spp_config())
    before_record = brain_state_path(journal_path=tmp_path).read_bytes()
    before_key = brain_fingerprint_key_path(journal_path=tmp_path).read_bytes()

    with pytest.raises(BrainStateExpectedFingerprintStaleError):
        begin_brain_refresh(
            NOW + timedelta(seconds=1),
            expect_active_fingerprint_absent=True,
            journal_path=tmp_path,
        )

    assert brain_state_path(journal_path=tmp_path).read_bytes() == before_record
    assert brain_fingerprint_key_path(journal_path=tmp_path).read_bytes() == before_key


def test_prerequisite_renewal_preserves_same_fingerprint_model_evidence(
    tmp_path: Path,
) -> None:
    config = _spp_config()
    _write_ready_record(tmp_path, config)
    before = _read_raw_record(tmp_path)
    expected = _current_fingerprint(tmp_path, config)
    begin = begin_brain_prerequisite_renewal(
        NOW + timedelta(minutes=1),
        expected_fingerprint_sha256=expected,
        journal_path=tmp_path,
    )

    assert begin["status"] == "started"
    checking = _read_raw_record(tmp_path)
    assert checking["aggregate_state"] == "checking"
    assert checking["evidence"]["generate"] == before["evidence"]["generate"]
    assert checking["evidence"]["cogitate"] == before["evidence"]["cogitate"]

    finish_now = NOW + timedelta(minutes=2)
    record = finish_brain_prerequisite_renewal(
        begin["permit"],
        _component(finish_now, expires_at=finish_now + timedelta(minutes=10)),
        finish_now,
        journal_path=tmp_path,
    )

    assert record["aggregate_state"] == "ready"
    assert record["revision"] == before["revision"] + 2
    assert record["evidence"]["configuration"] == before["evidence"]["configuration"]
    assert record["evidence"]["generate"] == before["evidence"]["generate"]
    assert record["evidence"]["cogitate"] == before["evidence"]["cogitate"]
    assert (
        record["evidence"]["lane_prerequisites"]["observed_at"]
        == finish_now.isoformat()
    )


def test_prerequisite_renewal_refuses_expired_model_evidence(tmp_path: Path) -> None:
    config = _spp_config()
    _write_ready_record(tmp_path, config)
    raw = _read_raw_record(tmp_path)
    raw["evidence"]["generate"]["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    atomic_replace(brain_state_path(journal_path=tmp_path), json.dumps(raw), mode=0o600)
    expected = _current_fingerprint(tmp_path, config)

    result = begin_brain_prerequisite_renewal(
        NOW,
        expected_fingerprint_sha256=expected,
        journal_path=tmp_path,
    )

    assert result["status"] == "unsafe"
    assert _read_raw_record(tmp_path)["revision"] == raw["revision"]


def test_prerequisite_renewal_reports_busy_when_refresh_lease_is_held(
    tmp_path: Path,
) -> None:
    config = _spp_config()
    _write_ready_record(tmp_path, config)
    holder = begin_brain_refresh(NOW + timedelta(seconds=1), journal_path=tmp_path)
    assert holder is not None
    try:
        result = begin_brain_prerequisite_renewal(
            NOW + timedelta(seconds=2),
            expected_fingerprint_sha256=_current_fingerprint(tmp_path, config),
            journal_path=tmp_path,
        )
    finally:
        holder.release()

    assert result["status"] == "busy"


def test_prerequisite_renewal_refuses_non_spp_lane(tmp_path: Path) -> None:
    config = _cloud_config()
    _write_ready_record(tmp_path, config)
    before = brain_state_path(journal_path=tmp_path).read_bytes()

    result = begin_brain_prerequisite_renewal(
        NOW + timedelta(seconds=1),
        expected_fingerprint_sha256=_current_fingerprint(tmp_path, config),
        journal_path=tmp_path,
    )

    assert result["status"] == "unsafe"
    assert brain_state_path(journal_path=tmp_path).read_bytes() == before


@pytest.mark.parametrize(
    "mutation",
    (
        lambda raw: raw["evidence"].__setitem__("generate", None),
        lambda raw: raw["evidence"]["generate"].__setitem__("status", "failed"),
        lambda raw: raw["evidence"]["generate"].__setitem__("expires_at", "not-time"),
    ),
)
def test_prerequisite_renewal_refuses_missing_malformed_or_non_ok_model_evidence(
    tmp_path: Path,
    mutation,
) -> None:
    config = _spp_config()
    _write_ready_record(tmp_path, config)
    raw = _read_raw_record(tmp_path)
    mutation(raw)
    atomic_replace(brain_state_path(journal_path=tmp_path), json.dumps(raw), mode=0o600)
    before = brain_state_path(journal_path=tmp_path).read_bytes()

    result = begin_brain_prerequisite_renewal(
        NOW + timedelta(seconds=1),
        expected_fingerprint_sha256=_current_fingerprint(tmp_path, config),
        journal_path=tmp_path,
    )

    assert result["status"] == "unsafe"
    assert brain_state_path(journal_path=tmp_path).read_bytes() == before


def test_prerequisite_renewal_expected_fingerprint_mismatch_is_no_write(
    tmp_path: Path,
) -> None:
    config = _spp_config()
    _write_ready_record(tmp_path, config)
    before = brain_state_path(journal_path=tmp_path).read_bytes()

    result = begin_brain_prerequisite_renewal(
        NOW + timedelta(seconds=1),
        expected_fingerprint_sha256="0" * 64,
        journal_path=tmp_path,
    )

    assert result["status"] == "unsafe"
    assert result["reason"] == "fingerprint_mismatch"
    assert brain_state_path(journal_path=tmp_path).read_bytes() == before


def test_prerequisite_renewal_recovers_orphaned_checking_after_lease_released(
    tmp_path: Path,
) -> None:
    config = _spp_config()
    _write_ready_record(tmp_path, config)
    expected = _current_fingerprint(tmp_path, config)
    first = begin_brain_prerequisite_renewal(
        NOW + timedelta(seconds=1),
        expected_fingerprint_sha256=expected,
        journal_path=tmp_path,
    )
    assert first["status"] == "started"
    first["permit"].release()
    orphaned = _read_raw_record(tmp_path)
    assert orphaned["aggregate_state"] == "checking"

    second = begin_brain_prerequisite_renewal(
        NOW + timedelta(seconds=2),
        expected_fingerprint_sha256=expected,
        journal_path=tmp_path,
    )
    assert second["status"] == "started"
    recovered = _read_raw_record(tmp_path)
    assert recovered["revision"] == orphaned["revision"] + 1
    assert recovered["checking"]["run_id"] != orphaned["checking"]["run_id"]
    second["permit"].release()


def test_prerequisite_renewal_expired_permit_conflicts_and_releases(
    tmp_path: Path,
) -> None:
    config = _spp_config()
    _write_ready_record(tmp_path, config)
    expected = _current_fingerprint(tmp_path, config)
    begin = begin_brain_prerequisite_renewal(
        NOW,
        expected_fingerprint_sha256=expected,
        journal_path=tmp_path,
    )
    assert begin["status"] == "started"
    permit = begin["permit"]

    with pytest.raises(BrainStateConflictError):
        finish_brain_prerequisite_renewal(
            permit,
            _component(
                permit.expires_at, expires_at=permit.expires_at + timedelta(minutes=10)
            ),
            permit.expires_at,
            journal_path=tmp_path,
        )

    retry = begin_brain_prerequisite_renewal(
        NOW + timedelta(seconds=1),
        expected_fingerprint_sha256=expected,
        journal_path=tmp_path,
    )
    assert retry["status"] == "started"
    retry["permit"].release()


def test_prerequisite_renewal_conflicts_on_revision_drift_and_releases(
    tmp_path: Path,
) -> None:
    config = _spp_config()
    _write_ready_record(tmp_path, config)
    expected = _current_fingerprint(tmp_path, config)
    begin = begin_brain_prerequisite_renewal(
        NOW,
        expected_fingerprint_sha256=expected,
        journal_path=tmp_path,
    )
    assert begin["status"] == "started"
    raw = _read_raw_record(tmp_path)
    raw["revision"] += 1
    raw["checking"]["checking_revision"] = raw["revision"]
    atomic_replace(brain_state_path(journal_path=tmp_path), json.dumps(raw), mode=0o600)

    with pytest.raises(BrainStateConflictError):
        finish_brain_prerequisite_renewal(
            begin["permit"],
            _component(NOW + timedelta(seconds=1)),
            NOW + timedelta(seconds=1),
            journal_path=tmp_path,
        )

    retry = begin_brain_prerequisite_renewal(
        NOW + timedelta(seconds=2),
        expected_fingerprint_sha256=expected,
        journal_path=tmp_path,
    )
    assert retry["status"] == "started"
    retry["permit"].release()


def test_prerequisite_renewal_conflicts_on_runtime_marker_drift_and_releases(
    tmp_path: Path,
) -> None:
    config = _spp_config()
    _write_ready_record(tmp_path, config)
    expected = _current_fingerprint(tmp_path, config)
    begin = begin_brain_prerequisite_renewal(
        NOW,
        expected_fingerprint_sha256=expected,
        journal_path=tmp_path,
    )
    assert begin["status"] == "started"
    raw = _read_raw_record(tmp_path)
    raw["checking"]["runtime_failure_marker_seen"] = "changed-marker"
    atomic_replace(brain_state_path(journal_path=tmp_path), json.dumps(raw), mode=0o600)

    with pytest.raises(BrainStateConflictError):
        finish_brain_prerequisite_renewal(
            begin["permit"],
            _component(NOW + timedelta(seconds=1)),
            NOW + timedelta(seconds=1),
            journal_path=tmp_path,
        )

    retry = begin_brain_prerequisite_renewal(
        NOW + timedelta(seconds=2),
        expected_fingerprint_sha256=expected,
        journal_path=tmp_path,
    )
    assert retry["status"] == "started"
    retry["permit"].release()


def test_prerequisite_renewal_conflicts_on_fingerprint_drift(tmp_path: Path) -> None:
    config = _spp_config(account_id="acct-a")
    _write_ready_record(tmp_path, config)
    expected = _current_fingerprint(tmp_path, config)
    begin = begin_brain_prerequisite_renewal(
        NOW,
        expected_fingerprint_sha256=expected,
        journal_path=tmp_path,
    )
    assert begin["status"] == "started"
    _write_config(tmp_path, _spp_config(account_id="acct-b"))

    with pytest.raises(BrainStateConflictError):
        finish_brain_prerequisite_renewal(
            begin["permit"],
            _component(NOW, expires_at=NOW + timedelta(minutes=10)),
            NOW,
            journal_path=tmp_path,
        )

    retry = begin_brain_prerequisite_renewal(
        NOW + timedelta(seconds=1),
        expected_fingerprint_sha256=_current_fingerprint(
            tmp_path, _spp_config(account_id="acct-b")
        ),
        journal_path=tmp_path,
    )
    assert retry["status"] == "unsafe"


def test_prerequisite_renewal_abandon_records_failure_without_losing_model_evidence(
    tmp_path: Path,
) -> None:
    config = _spp_config()
    _write_ready_record(tmp_path, config)
    before = _read_raw_record(tmp_path)
    begin = begin_brain_prerequisite_renewal(
        NOW,
        expected_fingerprint_sha256=_current_fingerprint(tmp_path, config),
        journal_path=tmp_path,
    )
    assert begin["status"] == "started"

    record = abandon_brain_prerequisite_renewal(
        begin["permit"],
        "probe_internal_error",
        NOW + timedelta(seconds=1),
        journal_path=tmp_path,
    )

    assert record["reason_code"] == "probe_internal_error"
    assert record["evidence"]["lane_prerequisites"]["reason_code"] == (
        "probe_internal_error"
    )
    assert record["evidence"]["generate"] == before["evidence"]["generate"]
    assert record["evidence"]["cogitate"] == before["evidence"]["cogitate"]


def test_key_replacement_invalidates_prior_ready_record(tmp_path: Path) -> None:
    _write_ready_record(tmp_path, _cloud_config())
    inspection = inspect_brain_state(NOW, journal_path=tmp_path)
    assert inspection["projection"]["aggregate_state"] == "ready"

    key_path = brain_fingerprint_key_path(journal_path=tmp_path)
    key_path.unlink()
    atomic_replace(key_path, b"z" * 32, mode=0o600)
    projection = inspect_brain_state(NOW, journal_path=tmp_path)["projection"]

    assert projection["aggregate_state"] == "unknown"
    assert projection["reason_code"] == "brain_config_changed"


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

    failure_result = record_brain_runtime_failure(
        "local_server_unhealthy",
        NOW,
        expected_fingerprint_sha256=permit.fingerprint_sha256,
        component="lane_prerequisites",
        diagnostic={"phase": "failed"},
        journal_path=tmp_path,
    )
    assert failure_result["accepted"] is True
    failure = failure_result["record"]
    assert failure is not None
    assert failure["aggregate_state"] == "unhealthy"

    with pytest.raises(BrainStateConflictError):
        finish_brain_refresh(permit, _ready_outcome(), NOW, journal_path=tmp_path)

    projection = inspect_brain_state(NOW, journal_path=tmp_path)["projection"]
    assert projection["aggregate_state"] == "unhealthy"
    assert projection["reason_code"] == "local_server_unhealthy"

    newer = begin_brain_refresh(NOW, journal_path=tmp_path)
    assert newer is not None
    finish_brain_refresh(newer, _ready_outcome(), NOW, journal_path=tmp_path)
    assert (
        inspect_brain_state(NOW, journal_path=tmp_path)["projection"]["aggregate_state"]
        == "ready"
    )


def test_runtime_failure_preserves_other_same_fingerprint_evidence(
    tmp_path: Path,
) -> None:
    config = _cloud_config()
    _write_ready_record(tmp_path, config)
    expected = _current_fingerprint(tmp_path, config)

    generate_result = record_brain_runtime_failure(
        "provider_unavailable",
        NOW,
        expected_fingerprint_sha256=expected,
        component="generate",
        journal_path=tmp_path,
    )
    assert generate_result["accepted"] is True
    generate_record = generate_result["record"]
    assert generate_record is not None
    assert generate_record["evidence"]["generate"] is not None
    assert generate_record["evidence"]["generate"]["status"] == "failed"
    assert generate_record["evidence"]["cogitate"] is not None
    assert generate_record["evidence"]["cogitate"]["status"] == "ok"

    _write_ready_record(tmp_path, config)
    expected = _current_fingerprint(tmp_path, config)
    cogitate_result = record_brain_runtime_failure(
        "cogitate_terminal_error",
        NOW,
        expected_fingerprint_sha256=expected,
        component="cogitate",
        journal_path=tmp_path,
    )
    assert cogitate_result["accepted"] is True
    cogitate_record = cogitate_result["record"]
    assert cogitate_record is not None
    assert cogitate_record["evidence"]["generate"] is not None
    assert cogitate_record["evidence"]["generate"]["status"] == "ok"
    assert cogitate_record["evidence"]["cogitate"] is not None
    assert cogitate_record["evidence"]["cogitate"]["status"] == "failed"


def test_runtime_failure_drops_evidence_from_prior_fingerprint(tmp_path: Path) -> None:
    config_f1 = _cloud_config(key="first")
    _write_ready_record(tmp_path, config_f1)

    config_f2 = _cloud_config(key="second")
    _write_config(tmp_path, config_f2)
    expected_f2 = _current_fingerprint(tmp_path, config_f2)
    result = record_brain_runtime_failure(
        "provider_unavailable",
        NOW,
        expected_fingerprint_sha256=expected_f2,
        component="generate",
        journal_path=tmp_path,
    )

    assert result["accepted"] is True
    record = result["record"]
    assert record is not None
    assert record["fingerprint_sha256"] == expected_f2
    assert record["evidence"]["configuration"] is None
    assert record["evidence"]["lane_prerequisites"] is None
    assert record["evidence"]["generate"] is not None
    assert record["evidence"]["cogitate"] is None


def test_runtime_failure_creates_or_replaces_missing_prior_record(
    tmp_path: Path,
) -> None:
    config = _cloud_config()
    _write_ready_record(tmp_path, config)
    expected = _current_fingerprint(tmp_path, config)
    state_path = brain_state_path(journal_path=tmp_path)

    for prior_bytes in (None, b"{"):
        if prior_bytes is None:
            state_path.unlink(missing_ok=True)
        else:
            state_path.write_bytes(prior_bytes)
        result = record_brain_runtime_failure(
            "provider_unavailable",
            NOW,
            expected_fingerprint_sha256=expected,
            component="generate",
            journal_path=tmp_path,
        )

        assert result["accepted"] is True
        record = result["record"]
        assert record is not None
        assert record["revision"] == 1
        assert record["evidence"]["configuration"] is None
        assert record["evidence"]["generate"] is not None


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

    expected = finished["fingerprint_sha256"]
    assert expected is not None
    failure_result = record_brain_runtime_failure(
        "local_server_unhealthy",
        NOW,
        expected_fingerprint_sha256=expected,
        component="lane_prerequisites",
        diagnostic={"phase": "failed"},
        journal_path=tmp_path,
    )
    assert failure_result["accepted"] is True
    failure = failure_result["record"]
    assert failure is not None
    assert failure["revision"] == 3

    newer = begin_brain_refresh(NOW + timedelta(seconds=1), journal_path=tmp_path)
    assert newer is not None
    record = _read_raw_record(tmp_path)
    assert record["revision"] == 4

    abandoned = abandon_brain_refresh(
        newer,
        "local_runtime_not_ready",
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

    expected = _read_raw_record(tmp_path)["fingerprint_sha256"]
    assert expected is not None
    result = record_brain_runtime_failure(
        "local_server_unhealthy",
        NOW,
        expected_fingerprint_sha256=expected,
        component="lane_prerequisites",
        diagnostic={"phase": "failed"},
        journal_path=tmp_path,
    )

    assert result["accepted"] is False
    assert result["rejected_reason"] == "state_unavailable"
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
    assert projection["reason_code"] == "brain_record_invalid"


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
    assert inspection["reason_code"] == "brain_record_invalid"


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
    assert projection["reason_code"] == "brain_record_invalid"


def test_clock_rollback_never_projects_ready(tmp_path: Path) -> None:
    _write_ready_record(tmp_path, _cloud_config())

    projection = inspect_brain_state(NOW - timedelta(seconds=1), journal_path=tmp_path)[
        "projection"
    ]

    assert projection["aggregate_state"] != "ready"
    assert projection["reason_code"] == "brain_record_invalid"


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
