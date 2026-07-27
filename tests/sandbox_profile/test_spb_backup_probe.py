# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from solstone.think.backup import runner as backup_runner
from solstone.think.backup.hosted import (
    HostedBinding,
    HostedCredentials,
    HostedCredsUnavailable,
)
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

SNAPSHOT_ID = "5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d5a1d"
CANARIES = (
    "broker-token",
    "ACCESS-SECRET",
    "SECRET-SECRET",
    "SESSION-SECRET",
    "https://storage.example.invalid",
    "sandbox-bucket",
    "sandbox-prefix",
    "acct-backup",
    SNAPSHOT_ID,
)
HUMAN_INIT_STDOUT = """created restic repository 00000000 at local:/tmp/spb-restic-proof

Please note that knowledge of your password is required to access
the repository. Losing your password means that your data is
irrecoverably lost.
"""


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


def _records(*records: object) -> str:
    return "\n".join(json.dumps(record) for record in records) + "\n"


def _scrub_text(value: object, secrets: tuple[str, ...]) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = "[redacted]".join(text.split(secret))
    return text


def _captured_command(capture: dict[str, object]) -> str:
    argv = capture["argv"]
    assert isinstance(argv, tuple)
    return next(token for token in ("init", "backup", "ls", "restore") if token in argv)


def _summary_record(
    snapshot_id: object = SNAPSHOT_ID,
    **overrides: object,
) -> dict[str, object]:
    record = {
        "message_type": "summary",
        "total_files_processed": 1,
        "total_bytes_processed": probe.FIXTURE_LENGTH,
        "snapshot_id": snapshot_id,
    }
    record.update(overrides)
    return record


def _restore_summary_record(**overrides: object) -> dict[str, object]:
    record = {
        "message_type": "summary",
        "total_files": 2,
        "files_restored": 2,
        "total_bytes": probe.FIXTURE_LENGTH,
        "bytes_restored": probe.FIXTURE_LENGTH,
    }
    record.update(overrides)
    return record


def _ls_records(
    *,
    snapshot_id: str = SNAPSHOT_ID,
    paths: object | None = None,
    file_node: dict[str, object] | None = None,
    extra: tuple[dict[str, object], ...] = (),
) -> tuple[dict[str, object], ...]:
    if paths is None:
        paths = [probe.LOGICAL_SOURCE_PATH]
    if file_node is None:
        file_node = {
            "message_type": "node",
            "struct_type": "node",
            "path": probe.LOGICAL_SOURCE_PATH,
            "type": "file",
            "size": probe.FIXTURE_LENGTH,
        }
    return (
        {
            "message_type": "snapshot",
            "struct_type": "snapshot",
            "id": snapshot_id,
            "paths": paths,
        },
        {"message_type": "node", "struct_type": "node", "path": "/spb", "type": "dir"},
        file_node,
        *extra,
    )


def _restic_command(args: list[str]) -> str:
    return next(token for token in ("init", "backup", "ls", "restore") if token in args)


@dataclass(frozen=True)
class _PhaseOutput:
    returncode: int = 0
    stdout: str | bytes = b""
    stderr: str | bytes = b""
    mutate_before_return: Callable[[], None] | None = None
    restore_mutator: Callable[[Path], None] | None = None


def _bytes(value: str | bytes) -> bytes:
    return value.encode() if isinstance(value, str) else value


def _assert_child_contract(
    args: list[str],
    kwargs: dict[str, Any],
    *,
    command: str,
    input_bytes: bytes | None,
    timeout: float | None,
) -> None:
    assert "--no-cache" in args
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
    assert kwargs["pass_fds"] == ()
    assert timeout == probe.RESTIC_CHILD_TIMEOUT_S
    env = kwargs["env"]
    assert env["RESTIC_PASSWORD"]
    if command == "init":
        assert "--json" not in args
    else:
        assert "--json" in args
    if command == "backup":
        assert input_bytes == probe.SPB_SYNTHETIC_FIXTURE_BYTES


def _default_phase_output(command: str) -> _PhaseOutput:
    if command == "init":
        return _PhaseOutput(stdout=HUMAN_INIT_STDOUT)
    if command == "backup":
        return _PhaseOutput(stdout=_records(_summary_record()))
    if command == "ls":
        return _PhaseOutput(stdout=_records(*_ls_records()))
    return _PhaseOutput(stdout=_records(_restore_summary_record()))


def _capture_child_surface(
    args: list[str],
    kwargs: dict[str, Any],
    *,
    input_bytes: bytes | None,
    timeout: float | None,
) -> dict[str, object]:
    env = kwargs["env"]
    assert isinstance(env, dict)
    child_secret_values = tuple(
        str(value)
        for value in (
            *env.values(),
            *CANARIES,
            SNAPSHOT_ID,
            probe.LOGICAL_SOURCE_PATH,
        )
        if value
    )
    return {
        "argv": tuple(_scrub_text(token, child_secret_values) for token in args),
        "env": {
            key: _scrub_text(value, child_secret_values) for key, value in env.items()
        },
        "json": "--json" in args,
        "stdin_bytes": input_bytes or b"",
        "timeout": timeout,
    }


def _install_popen_harness(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    phase_outputs: dict[str, _PhaseOutput] | None = None,
    restore_mutator: Callable[[Path], None] | None = None,
    captures: list[dict[str, object]] | None = None,
    passwords: list[str] | None = None,
) -> None:
    outputs = dict(phase_outputs or {})

    class FakePopen:
        pid = 12345

        def __init__(self, args: list[str], **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs
            self.returncode = 0

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            command = _restic_command(self.args)
            output = outputs.get(command, _default_phase_output(command))
            self.returncode = output.returncode
            if passwords is not None:
                passwords.append(self.kwargs["env"]["RESTIC_PASSWORD"])
            if captures is not None:
                captures.append(
                    _capture_child_surface(
                        self.args,
                        self.kwargs,
                        input_bytes=input,
                        timeout=timeout,
                    )
                )
            events.append(command)
            _assert_child_contract(
                self.args,
                self.kwargs,
                command=command,
                input_bytes=input,
                timeout=timeout,
            )
            if output.mutate_before_return is not None:
                output.mutate_before_return()
            if command == "restore" and output.returncode == 0:
                target = Path(self.args[self.args.index("--target") + 1])
                (target / "spb").mkdir(parents=True)
                (target / "spb" / "source.bin").write_bytes(
                    probe.SPB_SYNTHETIC_FIXTURE_BYTES
                )
                mutator = output.restore_mutator or restore_mutator
                if mutator is not None:
                    mutator(target)
            return _bytes(output.stdout), _bytes(output.stderr)

    monkeypatch.setattr(backup_runner.subprocess, "Popen", FakePopen)


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
    result: _PhaseOutput,
    mutate_before_return: Callable[[], None] | None = None,
    captures: list[dict[str, object]] | None = None,
) -> None:
    output = result
    if mutate_before_return is not None:
        output = _PhaseOutput(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            mutate_before_return=mutate_before_return,
            restore_mutator=result.restore_mutator,
        )
    _install_popen_harness(
        monkeypatch,
        events,
        phase_outputs={phase: output},
        captures=captures,
    )


def _install_ls_records(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    stdout: str,
) -> None:
    _install_popen_harness(
        monkeypatch,
        events,
        phase_outputs={"ls": _PhaseOutput(stdout=stdout)},
    )


def _install_success_fakes(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    events: list[str],
    *,
    restore_mutator: Callable[[Path], None] | None = None,
    captures: list[dict[str, object]] | None = None,
    passwords: list[str] | None = None,
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
    _install_popen_harness(
        monkeypatch,
        events,
        restore_mutator=restore_mutator,
        captures=captures,
        passwords=passwords,
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
    captures: list[dict[str, object]] = []
    _install_success_fakes(monkeypatch, clock, events, captures=captures)
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
    assert len(captures) == 4
    captures_by_command = {_captured_command(capture): capture for capture in captures}
    assert captures_by_command["init"]["json"] is False
    assert captures_by_command["backup"]["json"] is True
    assert captures_by_command["ls"]["json"] is True
    assert captures_by_command["restore"]["json"] is True
    for capture in captures:
        argv = capture["argv"]
        assert isinstance(argv, tuple)
        assert "--no-cache" in argv
    assert (attempt_dir / "spb" / "source.bin").exists()
    _assert_canaries_absent(repr(outcome))
    _assert_canaries_absent(caplog.text)
    _assert_canaries_absent(repr(captures))
    _assert_surviving_attempt_files_canary_clean(attempt_dir)


def test_spb_json_phase_scrub_set_contains_every_production_identifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_success_fakes(monkeypatch, clock, events)
    observed: dict[str, set[str]] = {}
    original = probe.run_restic_json_records

    def spy_run_restic_json_records(args: list[str], **kwargs: Any):
        command = _restic_command(args)
        observed[command] = {str(value) for value in kwargs["scrub_values"] if value}
        return original(args, **kwargs)

    monkeypatch.setattr(
        probe,
        "run_restic_json_records",
        spy_run_restic_json_records,
    )

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    assert outcome["state"] == probe_contract.PROOF_STATE_PASSED
    binding = probe._load_binding(journal)
    proof_binding = probe._proof_binding(binding, attempt_dir.name)
    spb_root = attempt_dir / probe.SPB_DIR_NAME
    restore_target = spb_root / probe.RESTORE_DIR_NAME
    repository = f"rclone:spb:{proof_binding.bucket}/{proof_binding.prefix}"
    expected_common = {
        binding.broker_endpoint,
        binding.account_id,
        binding.instance_id,
        binding.bucket,
        binding.prefix,
        binding.broker_token,
        proof_binding.prefix,
        str(attempt_dir),
        str(spb_root),
        str(restore_target),
        probe.LOGICAL_SOURCE_PATH,
        repository,
    }
    assert set(observed) == {"backup", "ls", "restore"}
    for command, scrub_values in observed.items():
        assert expected_common <= scrub_values
        if command in {"ls", "restore"}:
            assert SNAPSHOT_ID in scrub_values
        else:
            assert SNAPSHOT_ID not in scrub_values


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


def test_partial_fixture_write_is_internal_error_before_contact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)

    def partial_write(_fd: int, data: bytes) -> int:
        assert len(data) == probe.FIXTURE_LENGTH
        return probe.FIXTURE_LENGTH - 1

    monkeypatch.setattr(probe.os, "write", partial_write)
    monkeypatch.setattr(probe, "fetch_hosted_credentials", _forbid_contact)
    monkeypatch.setattr(probe.s3_wipe, "list_prefix_contents", _forbid_contact)
    monkeypatch.setattr(probe, "run_restic", _forbid_contact)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, probe_contract.REASON_INTERNAL_ERROR)


def test_recovery_key_path_is_structurally_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    captures: list[dict[str, object]] = []
    _install_success_fakes(monkeypatch, clock, events, captures=captures)

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


@pytest.mark.parametrize("prefix", ["users/acct/inst", "users/acct/inst/"])
def test_proof_binding_normalizes_optional_trailing_slash(prefix: str) -> None:
    binding = HostedBinding(
        broker_endpoint="https://broker.example.invalid",
        account_id="acct",
        instance_id="inst",
        bucket="bucket",
        prefix=prefix,
        broker_token="broker-token",
    )

    proof_binding = probe._proof_binding(binding, ATTEMPT_ID)

    assert proof_binding.prefix == f"users/acct/inst/proofs/{ATTEMPT_ID}/"


def test_spb_capability_mismatch_refuses_before_contact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    payload_path = journal / "backup" / "hosted" / "binding.json"
    payload = json.loads(payload_path.read_text("utf-8"))
    payload["instance_id"] = "99999999-9999-4999-8999-999999999999"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    _install_ready_tools(monkeypatch)
    monkeypatch.setattr(probe, "fetch_hosted_credentials", _forbid_contact)
    monkeypatch.setattr(probe.s3_wipe, "list_prefix_contents", _forbid_contact)
    monkeypatch.setattr(probe, "run_restic", _forbid_contact)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, probe_contract.REASON_CAPABILITY_NOT_READY)


def test_daily_key_comes_from_passed_journal_not_ambient_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    daily_key = probe.state.get_daily_key(journal)
    assert daily_key is not None
    ambient = tmp_path / "ambient-journal"
    (ambient / "config").mkdir(parents=True)
    (ambient / "config" / "journal.json").write_text(
        json.dumps(
            {
                "backup": {
                    "enabled": True,
                    "mode": "operated",
                    "daily_key": "ambient-daily-key",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(ambient))
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    passwords: list[str] = []
    _install_success_fakes(monkeypatch, clock, events, passwords=passwords)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    assert outcome["state"] == probe_contract.PROOF_STATE_PASSED
    assert set(passwords) == {daily_key}
    assert "ambient-daily-key" not in passwords


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
    _install_popen_harness(monkeypatch, events)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    assert outcome["state"] == probe_contract.PROOF_STATE_PASSED
    _assert_outcome_contract(outcome)
    assert "init" in events


def test_elapsed_monotonic_time_reduces_lease_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = SequencedClock(
        [
            1000.0,
            1000.0,
            1000.0,
            1000.0,
            1000.0,
            1000.0,
            1000.0,
            1000.0,
            1002.0,
            1002.0,
        ]
    )
    monkeypatch.setattr(probe, "_clock", clock)
    events: list[str] = []
    _install_sequenced_creds(
        monkeypatch,
        clock,
        events,
        [
            "2026-01-01T00:10:00Z",
            "2026-01-01T00:01:16Z",
        ],
    )
    _install_empty_listing(monkeypatch, events)
    monkeypatch.setattr(probe, "run_restic", _forbid_contact)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, probe_contract.REASON_RESPONSE_INVALID)
    assert events == ["fetch", "list", "fetch"]


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
    (
        "phase",
        "result",
        "reason",
        "checks",
        "expected_parse_calls",
        "expected_consume_calls",
    ),
    [
        (
            "init",
            _PhaseOutput(
                returncode=1,
                stderr=backup_runner._PROCESS_GROUP_CLEANUP_UNVERIFIED,
            ),
            probe_contract.REASON_CLEANUP_UNVERIFIED,
            (),
            0,
            0,
        ),
        (
            "init",
            _PhaseOutput(returncode=124, stderr="timeout"),
            probe_contract.REASON_DEADLINE_EXCEEDED,
            (),
            0,
            0,
        ),
        (
            "backup",
            _PhaseOutput(returncode=1, stdout=_records(_summary_record())),
            probe_contract.REASON_REMOTE_REJECTED,
            probe.PRIMITIVE_CHECKS[:1],
            0,
            0,
        ),
        (
            "backup",
            _PhaseOutput(stdout=""),
            probe_contract.REASON_RESPONSE_INVALID,
            probe.PRIMITIVE_CHECKS[:1],
            1,
            0,
        ),
        (
            "backup",
            _PhaseOutput(stdout='{"message_type":'),
            probe_contract.REASON_RESPONSE_INVALID,
            probe.PRIMITIVE_CHECKS[:1],
            1,
            0,
        ),
        (
            "backup",
            _PhaseOutput(
                stdout=(
                    '{"message_type":"summary","message_type":"summary",'
                    '"total_files_processed":1,"total_bytes_processed":4096,'
                    f'"snapshot_id":"{SNAPSHOT_ID}"}}\n'
                )
            ),
            probe_contract.REASON_RESPONSE_INVALID,
            probe.PRIMITIVE_CHECKS[:1],
            1,
            0,
        ),
        (
            "backup",
            _PhaseOutput(
                stdout=_records(
                    _summary_record(total_bytes_processed=probe.FIXTURE_LENGTH + 1)
                )
            ),
            probe_contract.REASON_CONTENT_MISMATCH,
            probe.PRIMITIVE_CHECKS[:1],
            1,
            1,
        ),
    ],
    ids=[
        "cleanup_unverified",
        "timeout",
        "nonzero",
        "empty_parse_rejection",
        "malformed_parse_rejection",
        "duplicate_key_parse_rejection",
        "semantic_rejection",
    ],
)
def test_spb_precedence_table_and_parser_consume_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    result: _PhaseOutput,
    reason: str,
    checks: tuple[str, ...],
    expected_parse_calls: int,
    expected_consume_calls: int,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    _install_success_fakes(monkeypatch, clock, events)
    _install_phase_restic(monkeypatch, events, phase=phase, result=result)
    parse_calls = 0
    consume_calls = 0
    original_parse = backup_runner._parse_json_records
    original_consume = backup_runner.ResticJsonRecordsResult.consume_records

    def counting_parse(raw_stdout: bytes | None) -> tuple[object, ...] | None:
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(raw_stdout)

    def counting_consume(
        self: backup_runner.ResticJsonRecordsResult,
    ) -> tuple[object, ...]:
        nonlocal consume_calls
        consume_calls += 1
        return original_consume(self)

    monkeypatch.setattr(backup_runner, "_parse_json_records", counting_parse)
    monkeypatch.setattr(
        backup_runner.ResticJsonRecordsResult,
        "consume_records",
        counting_consume,
    )

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, reason, checks=checks)
    assert parse_calls == expected_parse_calls
    assert consume_calls == expected_consume_calls


@pytest.mark.parametrize(
    "result",
    [
        _PhaseOutput(returncode=1, stderr="denied"),
        _PhaseOutput(returncode=124, stderr="timeout"),
    ],
)
def test_init_nonzero_and_timeout_map_to_expected_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: _PhaseOutput,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    captures: list[dict[str, object]] = []
    _install_success_fakes(monkeypatch, clock, events, captures=captures)
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
        (_records([]), probe_contract.REASON_RESPONSE_INVALID),
        (_records({"message_type": "summary"}), probe_contract.REASON_RESPONSE_INVALID),
        (_records(_summary_record("")), probe_contract.REASON_RESPONSE_INVALID),
        (
            _records(_summary_record(total_files_processed=True)),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(_summary_record(total_bytes_processed=probe.FIXTURE_LENGTH + 1)),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            _records(_summary_record(SNAPSHOT_ID.upper())),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records({"message_type": "verbose_status"}),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records({"message_type": "status", "percent_done": 0.5}),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(_summary_record(), _summary_record()),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(_summary_record(), {"message_type": "status"}),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(_summary_record(SNAPSHOT_ID)),
            probe_contract.REASON_REMOTE_REJECTED,
        ),
    ],
    ids=[
        "empty_stdout",
        "non_object_record",
        "summary_missing_required_fields",
        "empty_snapshot_id",
        "bool_total_files_processed",
        "wrong_total_bytes_processed",
        "uppercase_snapshot_id",
        "verbose_status_record",
        "status_without_summary",
        "duplicate_summary",
        "record_after_summary",
        "remote_rejected_preempts_stdout_validation",
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
        result=_PhaseOutput(returncode=returncode, stdout=stdout, stderr="denied"),
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
        result=_PhaseOutput(stdout=_records(_summary_record(SNAPSHOT_ID))),
        mutate_before_return=mutate_fixture,
    )

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(
        outcome,
        probe_contract.REASON_CONTENT_MISMATCH,
        checks=probe.PRIMITIVE_CHECKS[:1],
    )


def test_backup_and_restore_accept_status_records_before_terminal_summary() -> None:
    status_records = [
        {"message_type": "status", "percent_done": 0.25},
        {"message_type": "status", "percent_done": 0.75},
    ]

    assert (
        probe._validate_backup_records([*status_records, _summary_record()])
        == SNAPSHOT_ID
    )
    probe._validate_restore_records([*status_records, _restore_summary_record()])


@pytest.mark.parametrize(
    ("stdout_source", "reason"),
    [
        (_records([]), probe_contract.REASON_RESPONSE_INVALID),
        (
            _records(*_ls_records(snapshot_id="wrong-id")),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            _records(
                {
                    "message_type": "snapshot",
                    "struct_type": "node",
                    "id": SNAPSHOT_ID,
                    "paths": [probe.LOGICAL_SOURCE_PATH],
                },
                {"message_type": "node", "path": "/spb", "type": "dir"},
                {
                    "message_type": "node",
                    "path": probe.LOGICAL_SOURCE_PATH,
                    "type": "file",
                    "size": probe.FIXTURE_LENGTH,
                },
            ),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(
                *_ls_records(),
                {
                    "message_type": "snapshot",
                    "id": SNAPSHOT_ID,
                    "paths": [probe.LOGICAL_SOURCE_PATH],
                },
            ),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(*_ls_records(paths=["/wrong"])),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            _records(
                *_ls_records(
                    file_node={
                        "message_type": "node",
                        "struct_type": "snapshot",
                        "path": probe.LOGICAL_SOURCE_PATH,
                        "type": "file",
                        "size": probe.FIXTURE_LENGTH,
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
                    "id": SNAPSHOT_ID,
                    "paths": [probe.LOGICAL_SOURCE_PATH],
                },
                {"message_type": "node", "path": "/spb", "type": "dir"},
            ),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            _records(
                {
                    "message_type": "snapshot",
                    "id": SNAPSHOT_ID,
                    "paths": [probe.LOGICAL_SOURCE_PATH],
                },
                {
                    "message_type": "node",
                    "path": probe.LOGICAL_SOURCE_PATH,
                    "type": "file",
                    "size": probe.FIXTURE_LENGTH,
                },
            ),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            _records(
                *_ls_records(
                    extra=({"message_type": "node", "path": "/spb", "type": "dir"},)
                )
            ),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            _records(
                *_ls_records(
                    extra=(
                        {
                            "message_type": "node",
                            "path": probe.LOGICAL_SOURCE_PATH,
                            "type": "file",
                            "size": probe.FIXTURE_LENGTH,
                        },
                    )
                )
            ),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            _records(
                *_ls_records(
                    extra=(
                        {
                            "message_type": "node",
                            "path": "/spb/link",
                            "type": "symlink",
                        },
                    )
                )
            ),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            _records(
                *_ls_records(
                    extra=(
                        {
                            "message_type": "node",
                            "path": "/spb/fifo",
                            "type": "fifo",
                        },
                    )
                )
            ),
            probe_contract.REASON_CONTENT_MISMATCH,
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
            lambda attempt_dir: _records(
                *_ls_records(
                    file_node={
                        "message_type": "node",
                        "path": str(attempt_dir / "spb" / "source.bin"),
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
                    "id": SNAPSHOT_ID,
                    "paths": [probe.LOGICAL_SOURCE_PATH],
                },
                {"message_type": "unknown"},
            ),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(
                *_ls_records(
                    extra=(
                        {
                            "message_type": "error",
                            "message": "unexpected restic record",
                        },
                    )
                )
            ),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            '{"message_type":"snapshot","message_type":"snapshot",'
            f'"id":"{SNAPSHOT_ID}","paths":["/spb/source.bin"]}}\n',
            probe_contract.REASON_RESPONSE_INVALID,
        ),
    ],
    ids=[
        "non_object_record",
        "wrong_snapshot_id",
        "snapshot_struct_type_mismatch",
        "duplicate_snapshot_record",
        "paths_mismatch",
        "node_struct_type_mismatch",
        "bool_file_size",
        "missing_file_size",
        "wrong_file_size",
        "missing_file_node",
        "missing_directory_node",
        "duplicate_directory_node",
        "duplicate_file_node",
        "link_node",
        "special_node",
        "extra_dir_node",
        "physical_source_path_node",
        "unknown_record_kind",
        "error_record_kind",
        "duplicate_message_type_key",
    ],
)
def test_ls_strictness_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout_source: str | Callable[[Path], str],
    reason: str,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    stdout = stdout_source(attempt_dir) if callable(stdout_source) else stdout_source
    _install_success_fakes(monkeypatch, clock, events)
    _install_ls_records(monkeypatch, events, stdout=stdout)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, reason, checks=probe.PRIMITIVE_CHECKS[:2])


def test_ls_accepts_permuted_records_without_struct_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal, attempt_dir = _ready_journal(tmp_path, monkeypatch)
    _install_ready_tools(monkeypatch)
    clock = _install_clock(monkeypatch)
    events: list[str] = []
    stdout = _records(
        {
            "message_type": "node",
            "path": probe.LOGICAL_SOURCE_PATH,
            "type": "file",
            "size": probe.FIXTURE_LENGTH,
        },
        {
            "message_type": "snapshot",
            "id": SNAPSHOT_ID,
            "paths": [probe.LOGICAL_SOURCE_PATH],
        },
        {"message_type": "node", "path": "/spb", "type": "dir"},
    )
    _install_success_fakes(monkeypatch, clock, events)
    _install_ls_records(monkeypatch, events, stdout=stdout)

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    assert outcome["state"] == probe_contract.PROOF_STATE_PASSED
    _assert_outcome_contract(outcome)


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
    ids=[
        "remote_rejected",
        "extra_restored_file",
        "restored_symlink",
        "restored_fifo",
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
            result=_PhaseOutput(returncode=returncode, stderr="denied"),
        )
    elif mutator is not None:
        _install_popen_harness(
            monkeypatch,
            events,
            restore_mutator=mutator,
        )

    outcome = probe.prove_spb_backup(journal, attempt_dir=attempt_dir)

    _assert_failed(outcome, reason, checks=probe.PRIMITIVE_CHECKS[:3])


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        ("", probe_contract.REASON_RESPONSE_INVALID),
        (_records({"message_type": "summary"}), probe_contract.REASON_RESPONSE_INVALID),
        (
            _records(_restore_summary_record(total_files=True)),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(_restore_summary_record(total_files=1)),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            _records(_restore_summary_record(bytes_restored=probe.FIXTURE_LENGTH + 1)),
            probe_contract.REASON_CONTENT_MISMATCH,
        ),
        (
            _records({"message_type": "verbose_status"}),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records({"message_type": "status", "percent_done": 0.5}),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(_restore_summary_record(), _restore_summary_record()),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            _records(_restore_summary_record(), {"message_type": "status"}),
            probe_contract.REASON_RESPONSE_INVALID,
        ),
        (
            '{"message_type":"summary","message_type":"summary",'
            '"total_files":2,"files_restored":2,'
            '"total_bytes":1024,"bytes_restored":1024}\n',
            probe_contract.REASON_RESPONSE_INVALID,
        ),
    ],
    ids=[
        "empty_stdout",
        "summary_missing_required_fields",
        "bool_total_files",
        "wrong_total_files",
        "wrong_bytes_restored",
        "verbose_status_record",
        "status_without_summary",
        "duplicate_summary",
        "record_after_summary",
        "duplicate_message_type_key",
    ],
)
def test_restore_json_strictness_failures(
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
    _install_phase_restic(
        monkeypatch,
        events,
        phase="restore",
        result=_PhaseOutput(stdout=stdout),
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
        result=_PhaseOutput(
            returncode=1,
            stderr=backup_runner._PROCESS_GROUP_CLEANUP_UNVERIFIED,
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
    captures: list[dict[str, object]] = []
    _install_success_fakes(monkeypatch, clock, events, captures=captures)
    if mode == "timeout":
        _install_phase_restic(
            monkeypatch,
            events,
            phase="init",
            result=_PhaseOutput(returncode=124, stderr="timeout"),
            captures=captures,
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
    _assert_canaries_absent(repr(captures))
    _assert_surviving_attempt_files_canary_clean(attempt_dir)
