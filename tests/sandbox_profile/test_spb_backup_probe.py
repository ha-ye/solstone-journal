# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from solstone.think.backup.hosted import HostedCredentials, HostedCredsUnavailable
from solstone.think.backup.runner import ResticResult
from solstone.think.sandbox_profile import probe_contract
from solstone.think.sandbox_profile import spb_backup_probe as probe
from tests.sandbox_profile import (
    ATTEMPT_ID,
    invoke,
    prepare_ok,
    sandbox_journal,
    spb_payload,
    write_attempt_dir,
)

SPB_REASON_VOCABULARY = {
    probe_contract.REASON_CAPABILITY_NOT_READY,
    probe_contract.REASON_CONTENT_MISMATCH,
    probe_contract.REASON_DEADLINE_EXCEEDED,
    probe_contract.REASON_REMOTE_REJECTED,
    probe_contract.REASON_RESPONSE_INVALID,
    probe_contract.REASON_CLEANUP_UNVERIFIED,
    probe_contract.REASON_INTERNAL_ERROR,
}

CANARIES = (
    "broker-token",
    "ACCESS-SECRET",
    "SECRET-SECRET",
    "SESSION-SECRET",
    "https://storage.example.invalid",
    "sandbox-bucket",
    "sandbox-prefix",
    "acct-backup",
    "snapshot-secret-id",
)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)
        self.mono = 1000.0

    def utcnow(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        self.mono += 0.01
        return self.mono


class SequencedClock(FakeClock):
    def __init__(self, values: list[float]) -> None:
        super().__init__()
        self._values = list(values)

    def monotonic(self) -> float:
        if self._values:
            return self._values.pop(0)
        return super().monotonic()


def _assert_canaries_absent(text: str) -> None:
    for canary in CANARIES:
        assert canary not in text


def _assert_surviving_attempt_files_canary_clean(attempt_dir: Path) -> None:
    for path in attempt_dir.rglob("*"):
        _assert_canaries_absent(str(path))
        if path.is_file():
            _assert_canaries_absent(path.read_text(encoding="utf-8", errors="ignore"))


def _assert_outcome_contract(outcome: dict[str, Any]) -> None:
    assert set(outcome) == {
        probe_contract.FIELD_STATE,
        probe_contract.FIELD_CHECKS,
        probe_contract.FIELD_REASON,
        probe_contract.FIELD_DURATION_MS,
    }
    assert isinstance(outcome[probe_contract.FIELD_DURATION_MS], int)
    assert outcome[probe_contract.FIELD_DURATION_MS] >= 0
    checks = tuple(outcome[probe_contract.FIELD_CHECKS])
    assert checks == probe.PRIMITIVE_CHECKS[: len(checks)]
    if outcome[probe_contract.FIELD_STATE] == probe_contract.PROOF_STATE_FAILED:
        assert outcome[probe_contract.FIELD_REASON] in SPB_REASON_VOCABULARY
    else:
        assert outcome[probe_contract.FIELD_STATE] == probe_contract.PROOF_STATE_PASSED
        assert outcome[probe_contract.FIELD_REASON] is None
        assert checks == probe.PRIMITIVE_CHECKS


def _assert_failed(
    outcome: dict[str, Any],
    reason: str,
    *,
    checks: tuple[str, ...] = (),
) -> None:
    _assert_outcome_contract(outcome)
    assert outcome[probe_contract.FIELD_STATE] == probe_contract.PROOF_STATE_FAILED
    assert tuple(outcome[probe_contract.FIELD_CHECKS]) == checks
    assert outcome[probe_contract.FIELD_REASON] == reason


def _ready_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    result = invoke(
        ["apply", "spb", "--json"], input_text=json.dumps(spb_payload(journal))
    )
    assert result.exit_code == 0, result.output
    attempt_dir = write_attempt_dir(journal, ATTEMPT_ID)
    return journal, attempt_dir


def _install_ready_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe.readiness,
        "inspect_restic_ready",
        lambda **_kwargs: (Path("/tool/restic"), None),
    )
    monkeypatch.setattr(
        probe.rclone_install,
        "check_rclone_ready",
        lambda **_kwargs: Path("/tool/rclone"),
    )


def _install_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(probe, "_clock", clock)
    return clock


def _creds(clock: FakeClock) -> HostedCredentials:
    return HostedCredentials(
        access_key_id="ACCESS-SECRET",
        secret_access_key="SECRET-SECRET",
        session_token="SESSION-SECRET",
        endpoint="https://storage.example.invalid",
        expires_at=(clock.now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _creds_with_expiry(clock: FakeClock, expires_at: str) -> HostedCredentials:
    base = _creds(clock)
    return HostedCredentials(
        access_key_id=base.access_key_id,
        secret_access_key=base.secret_access_key,
        session_token=base.session_token,
        endpoint=base.endpoint,
        expires_at=expires_at,
    )


def _records(*records: dict[str, object]) -> str:
    return "\n".join(json.dumps(record) for record in records) + "\n"


def _summary_record(snapshot_id: object = "snapshot-secret-id") -> dict[str, object]:
    return {"message_type": "summary", "snapshot_id": snapshot_id}


def _ls_records(
    *,
    snapshot_id: str = "snapshot-secret-id",
    paths: object | None = None,
    file_node: dict[str, object] | None = None,
    extra: tuple[dict[str, object], ...] = (),
) -> tuple[dict[str, object], ...]:
    if paths is None:
        paths = [probe.LOGICAL_SOURCE_PATH]
    if file_node is None:
        file_node = {
            "message_type": "node",
            "path": probe.LOGICAL_SOURCE_PATH,
            "type": "file",
            "size": probe.FIXTURE_LENGTH,
        }
    return (
        {
            "message_type": "snapshot",
            "id": snapshot_id,
            "paths": paths,
        },
        {"message_type": "node", "path": "/spb", "type": "dir"},
        file_node,
        *extra,
    )


def _fake_restic(
    events: list[str],
    *,
    restore_mutator: Callable[[Path], None] | None = None,
):
    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        command = next(
            token for token in ("init", "backup", "ls", "restore") if token in args
        )
        events.append(command)
        assert kwargs["process_group"] is True
        assert kwargs["timeout"] == probe.RESTIC_CHILD_TIMEOUT_S
        assert kwargs["password"]
        if command == "init":
            return ResticResult(0, "", "", None, tuple(args))
        if command == "backup":
            assert kwargs["stdin_bytes"] == probe.SPB_SYNTHETIC_FIXTURE_BYTES
            stdout = _records(
                {
                    "message_type": "summary",
                    "snapshot_id": "snapshot-secret-id",
                }
            )
            return ResticResult(0, stdout, "", None, tuple(args))
        if command == "ls":
            stdout = _records(
                {
                    "message_type": "snapshot",
                    "id": "snapshot-secret-id",
                    "paths": [probe.LOGICAL_SOURCE_PATH],
                },
                {"message_type": "node", "path": "/spb", "type": "dir"},
                {
                    "message_type": "node",
                    "path": probe.LOGICAL_SOURCE_PATH,
                    "type": "file",
                    "size": probe.FIXTURE_LENGTH,
                },
            )
            return ResticResult(0, stdout, "", None, tuple(args))
        target = Path(args[args.index("--target") + 1])
        (target / "spb").mkdir(parents=True)
        (target / "spb" / "source.bin").write_bytes(probe.SPB_SYNTHETIC_FIXTURE_BYTES)
        if restore_mutator is not None:
            restore_mutator(target)
        stdout = _records(
            {
                "message_type": "summary",
                "bytes_restored": probe.FIXTURE_LENGTH,
            }
        )
        return ResticResult(0, stdout, "", None, tuple(args))

    return fake_run_restic


def _install_sequenced_creds(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    events: list[str],
    expires_at_values: list[str],
) -> None:
    values = list(expires_at_values)

    def fetch(_binding, *, scope: str) -> HostedCredentials:
        assert scope == "operated"
        events.append("fetch")
        if not values:
            return _creds(clock)
        return _creds_with_expiry(clock, values.pop(0))

    monkeypatch.setattr(probe, "fetch_hosted_credentials", fetch)


def _install_empty_listing(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
) -> None:
    def list_prefix_contents(**_kwargs: Any):
        events.append("list")
        return (), ()

    monkeypatch.setattr(probe.s3_wipe, "list_prefix_contents", list_prefix_contents)


def _install_phase_restic(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    phase: str,
    result: ResticResult,
    mutate_before_return: Callable[[], None] | None = None,
) -> None:
    success = _fake_restic(events)

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        command = next(
            token for token in ("init", "backup", "ls", "restore") if token in args
        )
        if command != phase:
            return success(args, **kwargs)
        events.append(command)
        if mutate_before_return is not None:
            mutate_before_return()
        return ResticResult(
            result.returncode,
            result.stdout,
            result.stderr,
            result.json,
            tuple(args),
        )

    monkeypatch.setattr(probe, "run_restic", fake_run_restic)


def _install_ls_records(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    stdout: str,
) -> None:
    success = _fake_restic(events)

    def fake_run_restic(args: list[str], **kwargs: Any) -> ResticResult:
        command = next(
            token for token in ("init", "backup", "ls", "restore") if token in args
        )
        if command != "ls":
            return success(args, **kwargs)
        events.append("ls")
        return ResticResult(0, stdout, "", None, tuple(args))

    monkeypatch.setattr(probe, "run_restic", fake_run_restic)


def _install_success_fakes(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    events: list[str],
    *,
    restore_mutator: Callable[[Path], None] | None = None,
) -> None:
    def fetch(_binding, *, scope: str) -> HostedCredentials:
        assert scope == "operated"
        events.append("fetch")
        return _creds(clock)

    def list_prefix_contents(**_kwargs: Any):
        events.append("list")
        return (), ()

    monkeypatch.setattr(probe, "fetch_hosted_credentials", fetch)
    monkeypatch.setattr(probe.s3_wipe, "list_prefix_contents", list_prefix_contents)
    monkeypatch.setattr(
        probe,
        "run_restic",
        _fake_restic(events, restore_mutator=restore_mutator),
    )


def _forbid_contact(*_args: Any, **_kwargs: Any):
    raise AssertionError("SPB proof contacted remote state")


def test_spb_success_uses_five_fresh_fetches_and_four_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_success_fakes(monkeypatch, clock, events)
    caplog.set_level(logging.DEBUG)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    assert outcome["state"] == probe_contract.PROOF_STATE_PASSED
    _assert_outcome_contract(outcome)
    assert tuple(outcome["checks"]) == probe.PRIMITIVE_CHECKS
    assert "spb.local_cleanup" not in outcome["checks"]
    assert events == [
        "fetch",
        "list",
        "fetch",
        "init",
        "fetch",
        "backup",
        "fetch",
        "ls",
        "fetch",
        "restore",
    ]
    assert (attempt_dir / "spb" / "source.bin").exists()
    _assert_canaries_absent(repr(outcome))
    _assert_canaries_absent(caplog.text)
    _assert_surviving_attempt_files_canary_clean(attempt_dir)


def test_nonempty_prefix_refuses_before_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []

    def fetch(_binding, *, scope: str) -> HostedCredentials:
        events.append("fetch")
        return _creds(clock)

    def list_prefix_contents(**_kwargs: Any):
        events.append("list")
        return ("sandbox-prefix/proofs/x/config",), ()

    monkeypatch.setattr(probe, "fetch_hosted_credentials", fetch)
    monkeypatch.setattr(probe.s3_wipe, "list_prefix_contents", list_prefix_contents)
    monkeypatch.setattr(probe, "run_restic", _forbid_contact)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, probe_contract.REASON_RESPONSE_INVALID)
    assert events == ["fetch", "list"]


def test_local_refusal_has_no_broker_or_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    attempt_dir = write_attempt_dir(journal, ATTEMPT_ID)
    _install_ready_tools(monkeypatch)
    monkeypatch.setattr(probe, "fetch_hosted_credentials", _forbid_contact)
    monkeypatch.setattr(probe.s3_wipe, "list_prefix_contents", _forbid_contact)
    monkeypatch.setattr(probe, "run_restic", _forbid_contact)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, probe_contract.REASON_CAPABILITY_NOT_READY)


def test_recovery_key_path_is_structurally_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_success_fakes(monkeypatch, clock, events)

    import solstone.think.backup.repo as repo

    monkeypatch.setattr(repo, "init_repository", _forbid_contact)
    monkeypatch.setattr(repo, "_add_recovery_key", _forbid_contact)
    monkeypatch.setattr(repo, "_verify_recovery_key", _forbid_contact)
    monkeypatch.setattr(repo, "_capture_current_key_id", _forbid_contact)
    monkeypatch.setattr(probe.state, "get_keys", _forbid_contact, raising=False)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    assert outcome["state"] == probe_contract.PROOF_STATE_PASSED
    _assert_outcome_contract(outcome)


def test_restore_tree_must_be_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []

    def mutate(target: Path) -> None:
        (target / "extra").write_text("not expected", encoding="utf-8")

    _install_success_fakes(monkeypatch, clock, events, restore_mutator=mutate)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(
        outcome,
        probe_contract.REASON_CONTENT_MISMATCH,
        checks=probe.PRIMITIVE_CHECKS[:3],
    )


def test_cleanup_helper_removes_only_spb_subtree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    attempt_dir = write_attempt_dir(journal, ATTEMPT_ID)
    (attempt_dir / "spb").mkdir()
    (attempt_dir / "spb" / "source.bin").write_text("synthetic", encoding="utf-8")

    outcome = probe.cleanup_spb_attempt_local(journal, attempt_dir=attempt_dir)

    assert outcome == {"state": "verified", "reason": None, "duration_ms": 0}
    assert attempt_dir.exists()
    assert not (attempt_dir / "spb").exists()


def test_inspector_surface_is_exact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(
        probe.readiness,
        "inspect_restic_ready",
        lambda **_kwargs: (None, "restic_missing"),
    )
    assert probe.inspect_sandbox_spb_prerequisites(tmp_path) == {
        "state": "unavailable",
        "reason": "restic_missing",
    }


@pytest.mark.parametrize(
    "reason_code",
    ["broker_unreachable", "hosted_entitlement_inactive"],
)
def test_broker_rejection_maps_to_remote_rejected_before_contact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    _install_clock(monkeypatch)

    def fetch(_binding, *, scope: str) -> HostedCredentials:
        assert scope == "operated"
        raise HostedCredsUnavailable(reason_code)

    monkeypatch.setattr(probe, "fetch_hosted_credentials", fetch)
    monkeypatch.setattr(probe.s3_wipe, "list_prefix_contents", _forbid_contact)
    monkeypatch.setattr(probe, "run_restic", _forbid_contact)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, probe_contract.REASON_REMOTE_REJECTED)


@pytest.mark.parametrize(
    "expires_at",
    [
        "2026-01-01T00:10:00.000Z",
        "2026-01-01T00:10:00+00:00",
        "2026-01-01T00:10:00z",
        " 2026-01-01T00:10:00Z",
        "2026-02-31T00:10:00Z",
    ],
)
def test_malformed_expires_at_is_response_invalid_before_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expires_at: str,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_sequenced_creds(monkeypatch, clock, events, [expires_at])
    monkeypatch.setattr(probe.s3_wipe, "list_prefix_contents", _forbid_contact)
    monkeypatch.setattr(probe, "run_restic", _forbid_contact)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, probe_contract.REASON_RESPONSE_INVALID)
    assert events == ["fetch"]


@pytest.mark.parametrize(
    "expires_at",
    [
        "2025-12-31T23:59:59Z",
        "2026-01-01T00:01:01Z",
    ],
)
def test_expired_or_insufficient_listing_lease_is_response_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expires_at: str,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_sequenced_creds(monkeypatch, clock, events, [expires_at])
    monkeypatch.setattr(probe.s3_wipe, "list_prefix_contents", _forbid_contact)
    monkeypatch.setattr(probe, "run_restic", _forbid_contact)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, probe_contract.REASON_RESPONSE_INVALID)
    assert events == ["fetch"]


def test_lease_remaining_exactly_seventy_five_rejects_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_sequenced_creds(
        monkeypatch,
        clock,
        events,
        [
            "2026-01-01T00:10:00Z",
            "2026-01-01T00:01:15Z",
        ],
    )
    _install_empty_listing(monkeypatch, events)
    monkeypatch.setattr(probe, "run_restic", _forbid_contact)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, probe_contract.REASON_RESPONSE_INVALID)
    assert events == ["fetch", "list", "fetch"]


def test_lease_remaining_above_seventy_five_proceeds_to_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_sequenced_creds(
        monkeypatch,
        clock,
        events,
        [
            "2026-01-01T00:10:00Z",
            "2026-01-01T00:01:16Z",
            "2026-01-01T00:10:00Z",
            "2026-01-01T00:10:00Z",
            "2026-01-01T00:10:00Z",
        ],
    )
    _install_empty_listing(monkeypatch, events)
    monkeypatch.setattr(probe, "run_restic", _fake_restic(events))

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    assert outcome["state"] == probe_contract.PROOF_STATE_PASSED
    _assert_outcome_contract(outcome)
    assert "init" in events


@pytest.mark.parametrize(
    ("keys", "uploads"),
    [
        ((), (("sandbox-prefix/proofs/x/multipart", "upload-1"),)),
        (
            (
                "sandbox-prefix/proofs/x/page-1-object",
                "sandbox-prefix/proofs/x/page-2-object",
            ),
            (),
        ),
        (
            (),
            (
                ("sandbox-prefix/proofs/x/page-1-upload", "upload-1"),
                ("sandbox-prefix/proofs/x/page-2-upload", "upload-2"),
            ),
        ),
        (("sandbox-prefix/proofs/x/config",), ()),
        (("sandbox-prefix/proofs/x/keys/key",), ()),
        (("sandbox-prefix/proofs/x/data/aa/blob",), ()),
        (("sandbox-prefix/proofs/x/index/index",), ()),
        (("sandbox-prefix/proofs/x/snapshots/snapshot",), ()),
        (("sandbox-prefix/proofs/x/locks/lock",), ()),
    ],
)
def test_storage_contents_refuse_before_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keys: tuple[str, ...],
    uploads: tuple[tuple[str, str], ...],
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []

    def fetch(_binding, *, scope: str) -> HostedCredentials:
        events.append("fetch")
        return _creds(clock)

    def list_prefix_contents(**_kwargs: Any):
        events.append("list")
        return keys, uploads

    monkeypatch.setattr(probe, "fetch_hosted_credentials", fetch)
    monkeypatch.setattr(probe.s3_wipe, "list_prefix_contents", list_prefix_contents)
    monkeypatch.setattr(probe, "run_restic", _forbid_contact)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, probe_contract.REASON_RESPONSE_INVALID)
    assert events == ["fetch", "list"]


@pytest.mark.parametrize(
    "failure",
    [
        "object-pagination-missing-token",
        "multipart-pagination-missing-markers",
    ],
)
def test_ambiguous_storage_listing_refuses_before_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []

    def fetch(_binding, *, scope: str) -> HostedCredentials:
        events.append("fetch")
        return _creds(clock)

    def list_prefix_contents(**_kwargs: Any):
        events.append(f"list:{failure}")
        raise RuntimeError(failure)

    monkeypatch.setattr(probe, "fetch_hosted_credentials", fetch)
    monkeypatch.setattr(probe.s3_wipe, "list_prefix_contents", list_prefix_contents)
    monkeypatch.setattr(probe, "run_restic", _forbid_contact)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, probe_contract.REASON_RESPONSE_INVALID)
    assert events == ["fetch", f"list:{failure}"]


@pytest.mark.parametrize(
    "result",
    [
        ResticResult(1, "", "denied", None, ("restic", "init")),
        ResticResult(124, "", "timeout", None, ("restic", "init")),
    ],
)
def test_init_nonzero_and_timeout_map_to_expected_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: ResticResult,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_success_fakes(monkeypatch, clock, events)
    _install_phase_restic(monkeypatch, events, phase="init", result=result)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    reason = (
        probe_contract.REASON_DEADLINE_EXCEEDED
        if result.returncode == 124
        else probe_contract.REASON_REMOTE_REJECTED
    )
    _assert_failed(outcome, reason)


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        ("", probe_contract.REASON_RESPONSE_INVALID),
        (_records({"message_type": "summary"}), probe_contract.REASON_RESPONSE_INVALID),
        (_records(_summary_record("")), probe_contract.REASON_RESPONSE_INVALID),
        (
            _records(_summary_record("snapshot-secret-id")),
            probe_contract.REASON_REMOTE_REJECTED,
        ),
    ],
)
def test_backup_failure_shapes_map_to_expected_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    reason: str,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_success_fakes(monkeypatch, clock, events)
    returncode = 1 if reason == probe_contract.REASON_REMOTE_REJECTED else 0
    _install_phase_restic(
        monkeypatch,
        events,
        phase="backup",
        result=ResticResult(returncode, stdout, "denied", None, ("restic", "backup")),
    )

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, reason, checks=probe.PRIMITIVE_CHECKS[:1])


def test_fixture_mutation_between_backup_checks_is_content_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_success_fakes(monkeypatch, clock, events)

    def mutate_fixture() -> None:
        (attempt_dir / "spb" / "source.bin").write_bytes(b"mutated")

    _install_phase_restic(
        monkeypatch,
        events,
        phase="backup",
        result=ResticResult(
            0,
            _records(_summary_record("snapshot-secret-id")),
            "",
            None,
            ("restic", "backup"),
        ),
        mutate_before_return=mutate_fixture,
    )

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(
        outcome,
        probe_contract.REASON_CONTENT_MISMATCH,
        checks=probe.PRIMITIVE_CHECKS[:1],
    )


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        (
            _records(*_ls_records(snapshot_id="wrong-id")),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(
                *_ls_records(),
                {
                    "message_type": "snapshot",
                    "id": "snapshot-secret-id",
                    "paths": [probe.LOGICAL_SOURCE_PATH],
                },
            ),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(*_ls_records(paths=["/wrong"])),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(
                *_ls_records(
                    file_node={
                        "message_type": "node",
                        "path": probe.LOGICAL_SOURCE_PATH,
                        "type": "file",
                        "size": True,
                    }
                )
            ),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(
                *_ls_records(
                    file_node={
                        "message_type": "node",
                        "path": probe.LOGICAL_SOURCE_PATH,
                        "type": "file",
                        "size": probe.FIXTURE_LENGTH + 1,
                    }
                )
            ),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            _records(
                {
                    "message_type": "snapshot",
                    "id": "snapshot-secret-id",
                    "paths": [probe.LOGICAL_SOURCE_PATH],
                },
                {"message_type": "node", "path": "/spb", "type": "dir"},
            ),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(
                *_ls_records(
                    extra=(
                        {"message_type": "node", "path": "/spb/extra", "type": "dir"},
                    )
                )
            ),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            _records(
                *_ls_records(
                    file_node={
                        "message_type": "node",
                        "path": str(Path("/tmp") / "attempt" / "spb" / "source.bin"),
                        "type": "file",
                        "size": probe.FIXTURE_LENGTH,
                    }
                )
            ),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            _records(
                {
                    "message_type": "snapshot",
                    "id": "snapshot-secret-id",
                    "paths": [probe.LOGICAL_SOURCE_PATH],
                },
                {"message_type": "unknown"},
            ),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            '{"message_type":"snapshot","message_type":"snapshot",'
            '"id":"snapshot-secret-id","paths":["/spb/source.bin"]}\n',
            probe_contract.REASON_RESPONSE_INVALID,
        ),
    ],
)
def test_ls_strictness_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    reason: str,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_success_fakes(monkeypatch, clock, events)
    _install_ls_records(monkeypatch, events, stdout=stdout)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, reason, checks=probe.PRIMITIVE_CHECKS[:2])


@pytest.mark.parametrize(
    ("returncode", "mutator", "reason"),
    [
        (1, None, probe_contract.REASON_REMOTE_REJECTED),
        (
            0,
            lambda target: (target / "extra").write_text(
                "not expected",
                encoding="utf-8",
            ),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            0,
            lambda target: (target / "spb" / "linked").symlink_to("source.bin"),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            0,
            lambda target: os.mkfifo(target / "spb" / "fifo"),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
    ],
)
def test_restore_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    mutator: Callable[[Path], None] | None,
    reason: str,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_success_fakes(monkeypatch, clock, events)
    if returncode:
        _install_phase_restic(
            monkeypatch,
            events,
            phase="restore",
            result=ResticResult(
                returncode,
                "",
                "denied",
                None,
                ("restic", "restore"),
            ),
        )
    elif mutator is not None:
        monkeypatch.setattr(
            probe,
            "run_restic",
            _fake_restic(events, restore_mutator=mutator),
        )

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, reason, checks=probe.PRIMITIVE_CHECKS[:3])


def test_process_group_survivor_ambiguity_maps_to_cleanup_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_success_fakes(monkeypatch, clock, events)
    _install_phase_restic(
        monkeypatch,
        events,
        phase="init",
        result=ResticResult(
            1,
            "",
            "process_group_cleanup_unverified",
            None,
            ("restic", "init"),
        ),
    )

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, probe_contract.REASON_CLEANUP_UNVERIFIED)


def test_pre_mint_budget_refusal_has_no_mint_or_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = SequencedClock([1000.0, 1268.0, 1268.0])
    monkeypatch.setattr(probe, "_clock", clock)
    monkeypatch.setattr(probe, "fetch_hosted_credentials", _forbid_contact)
    monkeypatch.setattr(probe.s3_wipe, "list_prefix_contents", _forbid_contact)
    monkeypatch.setattr(probe, "run_restic", _forbid_contact)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, probe_contract.REASON_DEADLINE_EXCEEDED)


def test_cleanup_helper_unverified_and_independent_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = sandbox_journal(tmp_path, monkeypatch)
    attempt_dir = write_attempt_dir(journal, ATTEMPT_ID)
    (attempt_dir / "spb").mkdir()
    clock = SequencedClock([10.0, 10.5, 10.5])
    monkeypatch.setattr(probe, "_clock", clock)
    captured: dict[str, float] = {}

    def cleanup_path_absent(_path: Path, deadline: float) -> bool:
        captured["deadline"] = deadline
        return False

    monkeypatch.setattr(probe, "_cleanup_path_absent", cleanup_path_absent)

    outcome = probe.cleanup_spb_attempt_local(journal, attempt_dir=attempt_dir)

    assert outcome["state"] == probe_contract.CLEANUP_STATE_UNVERIFIED
    assert outcome["reason"] == probe_contract.REASON_CLEANUP_UNVERIFIED
    assert captured["deadline"] == 40.0


@pytest.mark.parametrize(
    "mode",
    ["success", "timeout", "error"],
)
def test_canaries_absent_across_success_timeout_and_error_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_success_fakes(monkeypatch, clock, events)
    if mode == "timeout":
        _install_phase_restic(
            monkeypatch,
            events,
            phase="init",
            result=ResticResult(124, "", "timeout", None, ("restic", "init")),
        )
    elif mode == "error":

        def list_prefix_contents(**_kwargs: Any):
            events.append("list")
            return ("sandbox-prefix/proofs/x/config",), ()

        monkeypatch.setattr(
            probe.s3_wipe,
            "list_prefix_contents",
            list_prefix_contents,
        )
    caplog.set_level(logging.DEBUG)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_outcome_contract(outcome)
    _assert_canaries_absent(repr(outcome))
    _assert_canaries_absent(caplog.text)
    _assert_surviving_attempt_files_canary_clean(attempt_dir)
