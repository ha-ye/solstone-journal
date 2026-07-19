# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from solstone.think.journal_io.errors import LockTimeout
from solstone.think.providers import runtime_health
from solstone.think.providers.runtime_health import (
    RuntimeHealthConflictError,
    RuntimeHealthMalformedError,
    RuntimeHealthRecord,
    RuntimeHealthUnavailableError,
    consume_retry_token,
    inspect_retry_token,
    inspect_runtime_health,
    make_synthetic_runtime_health,
    observe_runtime_repair,
    read_retry_token,
    read_runtime_health,
    repair_corrupt_record,
    request_retry_token,
    request_runtime_retry,
    runtime_directory,
    runtime_health_path,
    runtime_operation_lock_path,
    runtime_operation_path,
    runtime_retry_token_path,
    write_runtime_health,
)


@pytest.fixture
def provider_cache_reset() -> Iterator[None]:
    from solstone.think.providers import local_server, local_vulkan

    local_vulkan.reset_detect_cache()
    local_server.reset_parallel_slots_cache()
    try:
        yield
    finally:
        local_vulkan.reset_detect_cache()
        local_server.reset_parallel_slots_cache()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _health(
    provider: str = "local",
    *,
    phase: str = "observing",
    fingerprint: str | None = "fp-1",
) -> RuntimeHealthRecord:
    record = make_synthetic_runtime_health(provider)
    record["phase"] = phase
    record["reason_code"] = "truth-observation-started"
    record["desired_fingerprint_sha256"] = fingerprint
    record["updated_at"] = "2026-07-19T00:00:00+00:00"
    return record


def _write_corrupt(path: Path, payload: bytes = b"{not-json") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _contains_value(value: Any, needle: str) -> bool:
    if value == needle:
        return True
    if isinstance(value, dict):
        return any(
            _contains_value(key, needle) or _contains_value(item, needle)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_value(item, needle) for item in value)
    if isinstance(value, Path):
        return needle in str(value)
    if isinstance(value, str):
        return needle in value
    return False


def test_phase_and_reason_code_vocabularies_are_disjoint() -> None:
    assert len(runtime_health.RUNTIME_PHASES) == 17
    assert runtime_health.RUNTIME_PHASES.isdisjoint(runtime_health.REASON_CODES)
    assert set().union(*runtime_health.REASON_CODE_GROUPS.values()) == set(
        runtime_health.REASON_CODES
    )


def test_invalid_provider_rejected_on_entry_points(tmp_path: Path) -> None:
    for call in (
        lambda: runtime_health_path("mlx", journal_path=tmp_path),
        lambda: runtime_retry_token_path("mlx", journal_path=tmp_path),
        lambda: runtime_operation_path("mlx", journal_path=tmp_path),
        lambda: read_runtime_health("mlx", journal_path=tmp_path),
        lambda: read_retry_token("mlx", journal_path=tmp_path),
        lambda: request_retry_token(
            "mlx",
            desired_fingerprint_sha256="fp",
            journal_path=tmp_path,
        ),
    ):
        with pytest.raises(ValueError):
            call()


def test_paths_modes_and_atomic_replace_for_both_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, int | None]] = []
    real_atomic_replace = runtime_health.atomic_replace

    def spy_atomic_replace(path: Path, data: str | bytes, *, mode: int | None = None):
        calls.append((path, mode))
        return real_atomic_replace(path, data, mode=mode)

    monkeypatch.setattr(runtime_health, "atomic_replace", spy_atomic_replace)

    stored_health = write_runtime_health(_health(), journal_path=tmp_path)
    stored_retry = request_retry_token(
        "local",
        desired_fingerprint_sha256="fp-1",
        owner={"actor": "test"},
        journal_path=tmp_path,
    )

    assert stored_health["revision"] == 1
    assert stored_retry["revision"] == 1
    assert runtime_health_path("local", journal_path=tmp_path) == (
        tmp_path / "health" / "providers" / "runtime" / "local.json"
    )
    assert runtime_retry_token_path("local", journal_path=tmp_path) == (
        tmp_path / "health" / "providers" / "runtime" / "local.retry-token.json"
    )
    assert runtime_operation_lock_path("local", journal_path=tmp_path) == (
        tmp_path / "health" / "providers" / "runtime" / "local.operation.lock"
    )
    assert _mode(runtime_health_path("local", journal_path=tmp_path)) == 0o600
    assert _mode(runtime_retry_token_path("local", journal_path=tmp_path)) == 0o600
    assert _mode(runtime_operation_lock_path("local", journal_path=tmp_path)) == 0o600
    assert calls == [
        (runtime_health_path("local", journal_path=tmp_path), 0o600),
        (runtime_retry_token_path("local", journal_path=tmp_path), 0o600),
    ]
    assert not list(runtime_directory(journal_path=tmp_path).glob(".tmp_*.tmp"))


def test_records_are_independent_but_share_operation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored_health = write_runtime_health(_health(), journal_path=tmp_path)
    stored_retry = request_retry_token(
        "local",
        desired_fingerprint_sha256="fp-1",
        journal_path=tmp_path,
    )

    updated_health = {**stored_health, "phase": "starting"}
    updated_health["reason_code"] = "launch-requested"
    written_health = write_runtime_health(updated_health, journal_path=tmp_path)
    assert read_retry_token("local", journal_path=tmp_path) == stored_retry

    written_retry = request_retry_token(
        "local",
        desired_fingerprint_sha256="fp-1",
        owner={"actor": "second"},
        journal_path=tmp_path,
    )
    assert read_runtime_health("local", journal_path=tmp_path) == written_health
    assert written_retry["token_id"] == stored_retry["token_id"]

    blocked = {runtime_operation_path("local", journal_path=tmp_path)}

    @contextmanager
    def fake_hold_lock(
        path: Path,
        *,
        timeout: float = 10.0,
        poll_interval: float = 0.05,
        mode: int | None = None,
    ) -> Iterator[None]:
        del poll_interval, mode
        if path in blocked:
            raise LockTimeout(path=path, timeout=timeout)
        yield

    monkeypatch.setattr(runtime_health, "hold_lock", fake_hold_lock)

    with pytest.raises(RuntimeHealthUnavailableError):
        write_runtime_health(written_health, journal_path=tmp_path)
    with pytest.raises(RuntimeHealthUnavailableError):
        request_retry_token(
            "local",
            desired_fingerprint_sha256="fp-1",
            journal_path=tmp_path,
        )


def test_absent_records_are_synthetic_and_read_verbs_do_not_create_directory(
    tmp_path: Path,
) -> None:
    assert not runtime_directory(journal_path=tmp_path).exists()

    health = read_runtime_health("local", journal_path=tmp_path)
    retry = read_retry_token("local", journal_path=tmp_path)
    health_inspection = inspect_runtime_health("local", journal_path=tmp_path)
    retry_inspection = inspect_retry_token("local", journal_path=tmp_path)
    repair_observation = observe_runtime_repair(
        "local",
        record_kind="health",
        journal_path=tmp_path,
    )

    assert health["phase"] == "stopped"
    assert health["revision"] == 0
    assert retry["token_id"] is None
    assert retry["revision"] == 0
    assert health_inspection["status"] == "ok"
    assert retry_inspection["status"] == "ok"
    assert repair_observation["status"] == "ok"
    assert not runtime_directory(journal_path=tmp_path).exists()


@pytest.mark.parametrize(
    ("record_kind", "path_func", "read_func", "inspect_func"),
    [
        ("health", runtime_health_path, read_runtime_health, inspect_runtime_health),
        (
            "retry-token",
            runtime_retry_token_path,
            read_retry_token,
            inspect_retry_token,
        ),
    ],
)
def test_malformed_record_raises_without_quarantine_or_reinitialize(
    tmp_path: Path,
    record_kind: str,
    path_func,
    read_func,
    inspect_func,
) -> None:
    path = path_func("local", journal_path=tmp_path)
    _write_corrupt(path)
    before = path.read_bytes()

    with pytest.raises(RuntimeHealthMalformedError):
        read_func("local", journal_path=tmp_path)

    inspection = inspect_func("local", journal_path=tmp_path)
    assert inspection["status"] == "corrupt"
    assert inspection["record_kind"] == record_kind
    assert "repair_handle" not in inspection
    assert path.read_bytes() == before
    assert not (runtime_directory(journal_path=tmp_path) / "corrupt").exists()


@pytest.mark.parametrize(
    ("record_kind", "path_func", "read_func", "inspect_func"),
    [
        ("health", runtime_health_path, read_runtime_health, inspect_runtime_health),
        (
            "retry-token",
            runtime_retry_token_path,
            read_retry_token,
            inspect_retry_token,
        ),
    ],
)
@pytest.mark.parametrize(
    "exc", [OSError("disk read failed"), PermissionError("denied")]
)
def test_read_oserror_and_permission_denial_are_unavailable_without_reinitialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_kind: str,
    path_func,
    read_func,
    inspect_func,
    exc: OSError,
) -> None:
    path = path_func("local", journal_path=tmp_path)
    if record_kind == "health":
        write_runtime_health(_health(), journal_path=tmp_path)
    else:
        request_retry_token(
            "local",
            desired_fingerprint_sha256="fp-1",
            journal_path=tmp_path,
        )
    before = path.read_bytes()
    original_read = runtime_health._read_record_bytes

    def fail_target(target: Path) -> bytes:
        if target == path:
            raise exc
        return original_read(target)

    monkeypatch.setattr(runtime_health, "_read_record_bytes", fail_target)

    with pytest.raises(RuntimeHealthUnavailableError):
        read_func("local", journal_path=tmp_path)

    inspection = inspect_func("local", journal_path=tmp_path)
    assert inspection["status"] == "unavailable"
    assert inspection["reason_code"] == "record-unavailable"
    assert inspection["record_kind"] == record_kind
    assert path.read_bytes() == before
    assert not (runtime_directory(journal_path=tmp_path) / "corrupt").exists()


def test_stale_revision_and_fingerprint_rejected(tmp_path: Path) -> None:
    stored = write_runtime_health(_health(fingerprint="fp-1"), journal_path=tmp_path)
    stale_revision = {**stored, "revision": 0}
    stale_revision["phase"] = "starting"

    with pytest.raises(RuntimeHealthConflictError):
        write_runtime_health(stale_revision, journal_path=tmp_path)

    changed_fingerprint = {**stored, "desired_fingerprint_sha256": "fp-2"}
    with pytest.raises(RuntimeHealthConflictError):
        write_runtime_health(
            changed_fingerprint,
            expected_desired_fingerprint_sha256="other-fp",
            journal_path=tmp_path,
        )

    first = request_retry_token(
        "local",
        desired_fingerprint_sha256="fp-1",
        journal_path=tmp_path,
    )
    current = request_retry_token(
        "local",
        desired_fingerprint_sha256="fp-1",
        journal_path=tmp_path,
    )
    with pytest.raises(RuntimeHealthConflictError):
        consume_retry_token(
            "local",
            token_id=str(first["token_id"]),
            revision=first["revision"],
            desired_fingerprint_sha256="fp-1",
            journal_path=tmp_path,
        )
    with pytest.raises(RuntimeHealthConflictError):
        consume_retry_token(
            "local",
            token_id=str(current["token_id"]),
            revision=current["revision"],
            desired_fingerprint_sha256="wrong-fp",
            journal_path=tmp_path,
        )


def test_retry_token_lifecycle_coalesces_consumes_and_survives_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = request_retry_token(
        "local",
        desired_fingerprint_sha256="fp-1",
        owner={"actor": "first"},
        journal_path=tmp_path,
    )
    second = request_retry_token(
        "local",
        desired_fingerprint_sha256="fp-1",
        owner={"actor": "second"},
        journal_path=tmp_path,
    )

    assert second["token_id"] == first["token_id"]
    assert second["revision"] == first["revision"] + 1
    assert second["owner"] == {"actor": "second"}

    outstanding = read_retry_token("local", journal_path=tmp_path)
    assert outstanding == second

    real_atomic_replace = runtime_health.atomic_replace

    def fail_replace(path: Path, data: str | bytes, *, mode: int | None = None):
        del path, data, mode
        raise OSError("write failed")

    monkeypatch.setattr(runtime_health, "atomic_replace", fail_replace)
    with pytest.raises(RuntimeHealthUnavailableError):
        consume_retry_token(
            "local",
            token_id=str(second["token_id"]),
            revision=second["revision"],
            desired_fingerprint_sha256="fp-1",
            journal_path=tmp_path,
        )
    monkeypatch.setattr(runtime_health, "atomic_replace", real_atomic_replace)
    assert read_retry_token("local", journal_path=tmp_path) == second

    consumed = consume_retry_token(
        "local",
        token_id=str(second["token_id"]),
        revision=second["revision"],
        desired_fingerprint_sha256="fp-1",
        journal_path=tmp_path,
    )
    assert consumed["token_id"] is None
    assert consumed["revision"] == second["revision"] + 1
    assert read_retry_token("local", journal_path=tmp_path) == consumed


def test_owner_runtime_retry_requires_current_terminal_failure(tmp_path: Path) -> None:
    failed = _health(phase="failed")
    failed["reason_code"] = "launch-budget-exhausted"
    stored = write_runtime_health(failed, journal_path=tmp_path)

    requested = request_runtime_retry(
        "local",
        expected_health_revision=stored["revision"],
        expected_retry_revision=0,
        desired_fingerprint_sha256="fp-1",
        owner={"source": "owner-recovery"},
        journal_path=tmp_path,
    )

    assert requested["revision"] == 1
    assert requested["token_id"] is not None
    assert requested["desired_fingerprint_sha256"] == "fp-1"
    assert requested["reason_code"] == "retry-token-requested"
    assert requested["owner"] == {"source": "owner-recovery"}
    assert read_runtime_health("local", journal_path=tmp_path) == stored


@pytest.mark.parametrize("phase", ["ready", "starting", "backoff", "host-blocked"])
def test_owner_runtime_retry_rejects_nonterminal_state(
    tmp_path: Path,
    phase: str,
) -> None:
    health = _health(phase=phase)
    stored = write_runtime_health(health, journal_path=tmp_path)

    with pytest.raises(
        RuntimeHealthConflictError,
        match="terminal failure",
    ):
        request_runtime_retry(
            "local",
            expected_health_revision=stored["revision"],
            expected_retry_revision=0,
            desired_fingerprint_sha256="fp-1",
            journal_path=tmp_path,
        )

    assert read_retry_token("local", journal_path=tmp_path)["token_id"] is None


def test_owner_runtime_retry_rejects_stale_and_outstanding_requests(
    tmp_path: Path,
) -> None:
    failed = _health(phase="failed")
    failed["reason_code"] = "launch-budget-exhausted"
    stored = write_runtime_health(failed, journal_path=tmp_path)

    with pytest.raises(RuntimeHealthConflictError, match="health revision"):
        request_runtime_retry(
            "local",
            expected_health_revision=stored["revision"] - 1,
            expected_retry_revision=0,
            desired_fingerprint_sha256="fp-1",
            journal_path=tmp_path,
        )
    with pytest.raises(RuntimeHealthConflictError, match="fingerprint"):
        request_runtime_retry(
            "local",
            expected_health_revision=stored["revision"],
            expected_retry_revision=0,
            desired_fingerprint_sha256="fp-stale",
            journal_path=tmp_path,
        )

    requested = request_runtime_retry(
        "local",
        expected_health_revision=stored["revision"],
        expected_retry_revision=0,
        desired_fingerprint_sha256="fp-1",
        journal_path=tmp_path,
    )
    with pytest.raises(RuntimeHealthConflictError, match="retry-token revision"):
        request_runtime_retry(
            "local",
            expected_health_revision=stored["revision"],
            expected_retry_revision=0,
            desired_fingerprint_sha256="fp-1",
            journal_path=tmp_path,
        )
    with pytest.raises(RuntimeHealthConflictError, match="already requested"):
        request_runtime_retry(
            "local",
            expected_health_revision=stored["revision"],
            expected_retry_revision=requested["revision"],
            desired_fingerprint_sha256="fp-1",
            journal_path=tmp_path,
        )


def test_owner_runtime_retry_replaces_an_old_target_token(tmp_path: Path) -> None:
    stale = request_retry_token(
        "local",
        desired_fingerprint_sha256="fp-old",
        journal_path=tmp_path,
    )
    failed = _health(phase="failed")
    failed["reason_code"] = "launch-budget-exhausted"
    stored = write_runtime_health(failed, journal_path=tmp_path)

    requested = request_runtime_retry(
        "local",
        expected_health_revision=stored["revision"],
        expected_retry_revision=stale["revision"],
        desired_fingerprint_sha256="fp-1",
        journal_path=tmp_path,
    )

    assert requested["token_id"] != stale["token_id"]
    assert requested["desired_fingerprint_sha256"] == "fp-1"
    assert requested["revision"] == stale["revision"] + 1


def test_repair_handle_allows_repair_without_parseable_token(tmp_path: Path) -> None:
    path = runtime_health_path("local", journal_path=tmp_path)
    _write_corrupt(path)

    observation = observe_runtime_repair(
        "local",
        record_kind="health",
        journal_path=tmp_path,
    )
    repair_handle = observation["repair_handle"]
    repaired = repair_corrupt_record(
        "local",
        record_kind="health",
        repair_handle=repair_handle,
        journal_path=tmp_path,
    )

    assert observation["status"] == "corrupt"
    assert repaired == make_synthetic_runtime_health("local")
    assert read_runtime_health("local", journal_path=tmp_path) == repaired
    assert repair_handle not in path.read_text(encoding="utf-8")


def test_repair_handle_rejects_stale_corrupt_bytes(tmp_path: Path) -> None:
    path = runtime_retry_token_path("local", journal_path=tmp_path)
    _write_corrupt(path, b"{first")
    observation = observe_runtime_repair(
        "local",
        record_kind="retry-token",
        journal_path=tmp_path,
    )

    path.write_bytes(b"{second")

    with pytest.raises(RuntimeHealthConflictError):
        repair_corrupt_record(
            "local",
            record_kind="retry-token",
            repair_handle=observation["repair_handle"],
            journal_path=tmp_path,
        )
    assert path.read_bytes() == b"{second"


def test_repair_handle_never_appears_in_logs_owner_copy_or_diagnostics(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    corrupt_path = runtime_retry_token_path("local", journal_path=tmp_path)
    _write_corrupt(corrupt_path, b"{private")
    repair_observation = observe_runtime_repair(
        "local",
        record_kind="retry-token",
        journal_path=tmp_path,
    )
    repair_handle = repair_observation["repair_handle"]

    public_returns: list[Any] = [
        runtime_health_path("parakeet", journal_path=tmp_path),
        runtime_retry_token_path("parakeet", journal_path=tmp_path),
        runtime_operation_path("parakeet", journal_path=tmp_path),
        runtime_operation_lock_path("parakeet", journal_path=tmp_path),
        make_synthetic_runtime_health("parakeet"),
        runtime_health.make_synthetic_retry_token("parakeet"),
        read_runtime_health("parakeet", journal_path=tmp_path),
        read_retry_token("parakeet", journal_path=tmp_path),
        inspect_runtime_health("parakeet", journal_path=tmp_path),
        inspect_retry_token("local", journal_path=tmp_path),
    ]
    health = write_runtime_health(
        _health("parakeet", fingerprint="fp-public"),
        journal_path=tmp_path,
    )
    token = request_retry_token(
        "parakeet",
        desired_fingerprint_sha256="fp-public",
        owner={"actor": "test"},
        journal_path=tmp_path,
    )
    consumed = consume_retry_token(
        "parakeet",
        token_id=str(token["token_id"]),
        revision=token["revision"],
        desired_fingerprint_sha256="fp-public",
        journal_path=tmp_path,
    )
    repaired = repair_corrupt_record(
        "local",
        record_kind="retry-token",
        repair_handle=repair_handle,
        journal_path=tmp_path,
    )
    public_returns.extend([health, token, consumed, repaired])

    for value in public_returns:
        assert not _contains_value(value, repair_handle)
    for record in caplog.records:
        assert repair_handle not in record.getMessage()


def test_scoped_provider_cache_reset_fixture_does_not_invoke_detection(
    provider_cache_reset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from solstone.think.providers import local_vulkan

    def fail_detection():
        raise AssertionError("provider cache reset must not detect GPUs")

    monkeypatch.setattr(local_vulkan, "_enumerate_gpus", fail_detection)
    local_vulkan.reset_detect_cache()
