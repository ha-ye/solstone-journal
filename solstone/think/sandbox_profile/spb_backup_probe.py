# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Operated-backup readback primitive for the disposable sandbox profile."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from solstone.think.backup import rclone_install, readiness, s3_wipe, state
from solstone.think.backup.hosted import (
    HostedBinding,
    HostedCredentials,
    HostedCredsUnavailable,
    fetch_hosted_credentials,
)
from solstone.think.backup.hosted_provider import (
    HostedResticSession,
    hosted_append_only_restic_session,
)
from solstone.think.backup.runner import (
    _PROCESS_GROUP_CLEANUP_UNVERIFIED,
    ResticResult,
    run_restic,
)
from solstone.think.sandbox_profile import (
    capabilities,
    envelope,
    intent,
    json_codec,
    probe_contract,
)

PRIMITIVE_CHECKS = probe_contract.PROOF_CHECKS[probe_contract.CAPABILITY_SPB][:4]
SPB_FAILED_REASONS = frozenset(
    (
        set(probe_contract.PROOF_SPECIFIC_REASONS[probe_contract.CAPABILITY_SPB])
        | set(probe_contract.FAILED_COMMON_REASONS)
    )
    - {probe_contract.REASON_CANCELLED}
)

INSPECT_READY = "ready"
INSPECT_UNAVAILABLE = "unavailable"
INSPECT_RESTIC_MISSING = "restic_missing"
INSPECT_RESTIC_INCOMPATIBLE = "restic_incompatible"
INSPECT_BACKUP_ADAPTER_UNAVAILABLE = "backup_adapter_unavailable"

ABSOLUTE_DEADLINE_S = 360.0
BROKER_FETCH_TIMEOUT_S = 30.0
RESTIC_CHILD_TIMEOUT_S = 60.0
STORAGE_LIST_TIMEOUT_S = 60.0
TERM_GRACE_S = 3.0
KILL_GRACE_S = 5.0
FINALIZE_RESERVE_S = 2.0
COORDINATOR_CLEANUP_BUDGET_S = 30.0
LEASE_SPAWN_FLOOR_S = 75.0

SPB_DIR_NAME = "spb"
SOURCE_FILE_NAME = "source.bin"
RESTORE_DIR_NAME = "restore"
LOGICAL_SOURCE_PATH = "/spb/source.bin"
FIXTURE_LENGTH = 4096
_FIXTURE_MARKER = b"SOLSTONE-SPB-SANDBOX-PROOF-SYNTHETIC-FIXTURE-V1\n"
SPB_SYNTHETIC_FIXTURE_BYTES = (
    _FIXTURE_MARKER + (b"0123456789abcdef-synthetic-spb-proof\n" * 128)
)[:FIXTURE_LENGTH]
if len(SPB_SYNTHETIC_FIXTURE_BYTES) != FIXTURE_LENGTH:  # pragma: no cover
    raise RuntimeError("invalid SPB fixture length")


class _Clock:
    def monotonic(self) -> float:
        return time.monotonic()

    def utcnow(self) -> datetime:
        return datetime.now(UTC)


_clock: _Clock = _Clock()


class _SpbProbeError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _SpbProbeInternalError(_SpbProbeError):
    def __init__(self) -> None:
        super().__init__(probe_contract.REASON_INTERNAL_ERROR)


@dataclass(frozen=True)
class _SpbProbeOutcome:
    state: str
    checks: tuple[str, ...]
    reason: str | None
    duration_ms: int

    def to_dict(self) -> dict[str, object]:
        return {
            probe_contract.FIELD_STATE: self.state,
            probe_contract.FIELD_CHECKS: list(self.checks),
            probe_contract.FIELD_REASON: self.reason,
            probe_contract.FIELD_DURATION_MS: self.duration_ms,
        }


@dataclass(frozen=True)
class _FixtureIdentity:
    device: int
    inode: int
    length: int
    digest: str


@dataclass(frozen=True)
class _Preflight:
    attempt_dir: Path
    spb_root: Path
    restore_target: Path
    fixture_path: Path
    fixture: _FixtureIdentity
    binding: HostedBinding
    proof_binding: HostedBinding
    daily_key: str
    restic_path: Path
    rclone_path: Path
    scrub_values: tuple[str, ...]


@dataclass(frozen=True)
class _CredentialReceipt:
    credentials: HostedCredentials
    received_monotonic: float
    lease_remaining_s: float


def inspect_sandbox_spb_prerequisites(journal_path: Path) -> dict[str, Any]:
    _ = Path(journal_path)
    restic_path, restic_reason = readiness.inspect_restic_ready(
        version_timeout=5.0,
    )
    if restic_path is None:
        reason = (
            INSPECT_RESTIC_MISSING
            if restic_reason == INSPECT_RESTIC_MISSING
            else INSPECT_RESTIC_INCOMPATIBLE
        )
        return {"state": INSPECT_UNAVAILABLE, "reason": reason}
    rclone_path = rclone_install.check_rclone_ready(version_timeout=5.0)
    if rclone_path is None:
        return {
            "state": INSPECT_UNAVAILABLE,
            "reason": INSPECT_BACKUP_ADAPTER_UNAVAILABLE,
        }
    return {"state": INSPECT_READY, "reason": None}


def prove_spb_backup(journal_path: Path, *, attempt_dir: Path) -> dict[str, Any]:
    start = _clock.monotonic()
    deadline = start + ABSOLUTE_DEADLINE_S
    checks: tuple[str, ...] = ()
    outcome = _failed(
        reason=probe_contract.REASON_INTERNAL_ERROR,
        checks=checks,
        duration_ms=0,
    )
    try:
        preflight = _preflight(Path(journal_path), Path(attempt_dir))

        _require_remaining(
            deadline,
            BROKER_FETCH_TIMEOUT_S + STORAGE_LIST_TIMEOUT_S + FINALIZE_RESERVE_S,
        )
        list_creds = _fetch_credentials(preflight.proof_binding)
        _require_remaining(deadline, STORAGE_LIST_TIMEOUT_S + FINALIZE_RESERVE_S)
        _require_lease(list_creds, STORAGE_LIST_TIMEOUT_S + FINALIZE_RESERVE_S)
        _prove_prefix_empty(preflight, list_creds.credentials)

        _require_remaining(
            deadline,
            BROKER_FETCH_TIMEOUT_S
            + RESTIC_CHILD_TIMEOUT_S
            + TERM_GRACE_S
            + KILL_GRACE_S
            + FINALIZE_RESERVE_S,
        )
        init_creds = _fetch_credentials(preflight.proof_binding)
        _require_child_budget_and_lease(deadline, init_creds)
        _run_init(preflight, init_creds.credentials)
        checks = PRIMITIVE_CHECKS[:1]

        _require_remaining(
            deadline,
            BROKER_FETCH_TIMEOUT_S
            + RESTIC_CHILD_TIMEOUT_S
            + TERM_GRACE_S
            + KILL_GRACE_S
            + FINALIZE_RESERVE_S,
        )
        backup_creds = _fetch_credentials(preflight.proof_binding)
        _require_child_budget_and_lease(deadline, backup_creds)
        _ensure_fixture_unchanged(preflight)
        snapshot_id = _run_backup(preflight, backup_creds.credentials)
        _ensure_fixture_unchanged(preflight)
        checks = PRIMITIVE_CHECKS[:2]

        _require_remaining(
            deadline,
            BROKER_FETCH_TIMEOUT_S
            + RESTIC_CHILD_TIMEOUT_S
            + TERM_GRACE_S
            + KILL_GRACE_S
            + FINALIZE_RESERVE_S,
        )
        ls_creds = _fetch_credentials(preflight.proof_binding)
        _require_child_budget_and_lease(deadline, ls_creds)
        _run_ls(preflight, ls_creds.credentials, snapshot_id)
        checks = PRIMITIVE_CHECKS[:3]

        _require_remaining(
            deadline,
            BROKER_FETCH_TIMEOUT_S
            + RESTIC_CHILD_TIMEOUT_S
            + TERM_GRACE_S
            + KILL_GRACE_S
            + FINALIZE_RESERVE_S,
        )
        restore_creds = _fetch_credentials(preflight.proof_binding)
        _require_child_budget_and_lease(deadline, restore_creds)
        _run_restore(preflight, restore_creds.credentials, snapshot_id)
        _verify_restore_tree(preflight)
        checks = PRIMITIVE_CHECKS[:4]
        outcome = _passed(checks=checks, duration_ms=_duration_ms(start))
    except _SpbProbeError as exc:
        outcome = _failed(
            reason=exc.reason,
            checks=checks,
            duration_ms=_duration_ms(start),
        )
    except Exception:
        outcome = _failed(
            reason=probe_contract.REASON_INTERNAL_ERROR,
            checks=checks,
            duration_ms=_duration_ms(start),
        )
    return outcome.to_dict()


def cleanup_spb_attempt_local(
    journal_path: Path, *, attempt_dir: Path
) -> dict[str, Any]:
    start = _clock.monotonic()
    try:
        resolved_attempt = _validate_attempt_dir(Path(journal_path), Path(attempt_dir))
    except _SpbProbeError:
        return {
            "state": probe_contract.CLEANUP_STATE_UNVERIFIED,
            "reason": probe_contract.REASON_CLEANUP_UNVERIFIED,
            "duration_ms": _duration_ms(start),
        }
    deadline = start + COORDINATOR_CLEANUP_BUDGET_S
    verified = _cleanup_path_absent(resolved_attempt / SPB_DIR_NAME, deadline)
    return {
        "state": (
            probe_contract.CLEANUP_STATE_VERIFIED
            if verified
            else probe_contract.CLEANUP_STATE_UNVERIFIED
        ),
        "reason": None if verified else probe_contract.REASON_CLEANUP_UNVERIFIED,
        "duration_ms": _duration_ms(start),
    }


def _preflight(journal: Path, attempt_dir: Path) -> _Preflight:
    resolved_attempt = _validate_attempt_dir(journal, attempt_dir)
    restic_path, restic_reason = readiness.inspect_restic_ready(version_timeout=5.0)
    if restic_path is None:
        _ = restic_reason
        raise _SpbProbeError(probe_contract.REASON_CAPABILITY_NOT_READY) from None
    rclone_path = rclone_install.check_rclone_ready(version_timeout=5.0)
    if rclone_path is None:
        raise _SpbProbeError(probe_contract.REASON_CAPABILITY_NOT_READY) from None
    binding = _load_binding(journal)
    if not _spb_capability_ready(journal):
        raise _SpbProbeError(probe_contract.REASON_CAPABILITY_NOT_READY) from None
    daily_key = state.get_daily_key(journal)
    if daily_key is None:
        raise _SpbProbeError(probe_contract.REASON_CAPABILITY_NOT_READY) from None
    proof_binding = _proof_binding(binding, resolved_attempt.name)
    spb_root = resolved_attempt / SPB_DIR_NAME
    restore_target = spb_root / RESTORE_DIR_NAME
    fixture_path = spb_root / SOURCE_FILE_NAME
    _create_fixture(fixture_path)
    fixture = _fixture_identity(fixture_path)
    if restore_target.exists() or restore_target.is_symlink():
        raise _SpbProbeInternalError() from None
    scrub_values = _scrub_values(
        binding=binding,
        proof_binding=proof_binding,
        attempt_dir=resolved_attempt,
        spb_root=spb_root,
        restore_target=restore_target,
    )
    return _Preflight(
        attempt_dir=resolved_attempt,
        spb_root=spb_root,
        restore_target=restore_target,
        fixture_path=fixture_path,
        fixture=fixture,
        binding=binding,
        proof_binding=proof_binding,
        daily_key=daily_key,
        restic_path=restic_path,
        rclone_path=rclone_path,
        scrub_values=scrub_values,
    )


def _validate_attempt_dir(journal: Path, attempt_dir: Path) -> Path:
    parent = probe_contract.probe_attempts_parent_path(journal).resolve()
    try:
        stat_result = attempt_dir.lstat()
    except OSError:
        raise _SpbProbeInternalError() from None
    if stat.S_ISLNK(stat_result.st_mode) or not stat.S_ISDIR(stat_result.st_mode):
        raise _SpbProbeInternalError() from None
    if stat.S_IMODE(stat_result.st_mode) != probe_contract.ATTEMPT_DIR_MODE:
        raise _SpbProbeInternalError() from None
    resolved = attempt_dir.resolve()
    if resolved.parent != parent:
        raise _SpbProbeInternalError() from None
    try:
        parsed = uuid.UUID(resolved.name)
    except ValueError:
        raise _SpbProbeInternalError() from None
    if str(parsed) != resolved.name:
        raise _SpbProbeInternalError() from None
    return resolved


def _cleanup_path_absent(path: Path, deadline: float) -> bool:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    except OSError:
        return False
    poll_until = _clock.monotonic() + min(FINALIZE_RESERVE_S, _remaining(deadline))
    while _clock.monotonic() <= poll_until:
        try:
            path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if _remaining(deadline) <= 0:
            return False
        time.sleep(min(0.02, _remaining(deadline)))
    return False


def _spb_capability_ready(journal: Path) -> bool:
    try:
        config = capabilities._read_config(journal)
        cap = capabilities._spb_status(journal, config, intent.load_intent(journal))
    except (OSError, ValueError, intent.IntentError):
        return False
    return cap.state == envelope.CAP_READY


def _load_binding(journal: Path) -> HostedBinding:
    payload = capabilities._read_hosted_binding(journal)
    if not isinstance(payload, dict):
        raise _SpbProbeError(probe_contract.REASON_CAPABILITY_NOT_READY) from None
    fields = {}
    for key in (
        "broker_endpoint",
        "account_id",
        "instance_id",
        "bucket",
        "prefix",
        "broker_token",
    ):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise _SpbProbeError(probe_contract.REASON_CAPABILITY_NOT_READY) from None
        fields[key] = value
    return HostedBinding(**fields)


def _proof_binding(binding: HostedBinding, attempt_id: str) -> HostedBinding:
    run_segments = _prefix_segments(binding.prefix)
    derived_segments = (*run_segments, "proofs", attempt_id)
    run_prefix = "/".join(run_segments)
    derived_prefix = "/".join(derived_segments) + "/"
    if not derived_prefix.startswith(f"{run_prefix}/"):
        raise _SpbProbeError(probe_contract.REASON_CAPABILITY_NOT_READY) from None
    if derived_segments[: len(run_segments)] != run_segments:
        raise _SpbProbeError(probe_contract.REASON_CAPABILITY_NOT_READY) from None
    if len(derived_segments) < len(run_segments) + 2:
        raise _SpbProbeError(probe_contract.REASON_CAPABILITY_NOT_READY) from None
    return dataclasses.replace(binding, prefix=derived_prefix)


def _prefix_segments(prefix: str) -> tuple[str, ...]:
    normalized = prefix[:-1] if prefix.endswith("/") else prefix
    if not normalized or normalized.startswith("/"):
        raise _SpbProbeError(probe_contract.REASON_CAPABILITY_NOT_READY) from None
    segments = tuple(normalized.split("/"))
    if any(segment in {"", ".", ".."} or "\x00" in segment for segment in segments):
        raise _SpbProbeError(probe_contract.REASON_CAPABILITY_NOT_READY) from None
    return segments


def _create_fixture(path: Path) -> None:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            if os.write(fd, SPB_SYNTHETIC_FIXTURE_BYTES) != FIXTURE_LENGTH:
                raise _SpbProbeInternalError() from None
            os.fsync(fd)
        finally:
            os.close(fd)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        raise _SpbProbeInternalError() from None


def _fixture_identity(path: Path) -> _FixtureIdentity:
    try:
        stat_result = path.lstat()
    except OSError:
        raise _SpbProbeInternalError() from None
    if (
        stat.S_ISLNK(stat_result.st_mode)
        or not stat.S_ISREG(stat_result.st_mode)
        or stat_result.st_nlink != 1
        or stat_result.st_size != FIXTURE_LENGTH
    ):
        raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None
    try:
        data = path.read_bytes()
    except OSError:
        raise _SpbProbeInternalError() from None
    return _FixtureIdentity(
        device=stat_result.st_dev,
        inode=stat_result.st_ino,
        length=len(data),
        digest=hashlib.sha256(data).hexdigest(),
    )


def _ensure_fixture_unchanged(preflight: _Preflight) -> None:
    if _fixture_identity(preflight.fixture_path) != preflight.fixture:
        raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None


def _fetch_credentials(binding: HostedBinding) -> _CredentialReceipt:
    try:
        credentials = fetch_hosted_credentials(binding, scope="operated")
    except HostedCredsUnavailable:
        raise _SpbProbeError(probe_contract.REASON_REMOTE_REJECTED) from None
    received_wall = _clock.utcnow()
    received_monotonic = _clock.monotonic()
    expires_at = _parse_expires_at(credentials.expires_at)
    lease_remaining = (expires_at - received_wall).total_seconds()
    if lease_remaining <= 0:
        raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None
    return _CredentialReceipt(
        credentials=credentials,
        received_monotonic=received_monotonic,
        lease_remaining_s=lease_remaining,
    )


def _parse_expires_at(value: str) -> datetime:
    if len(value) != 20 or value[4] != "-" or value[7] != "-":
        raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None
    if value[10] != "T" or value[13] != ":" or value[16] != ":" or value[19] != "Z":
        raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None
    parts = value[:19]
    try:
        parsed = datetime.strptime(parts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None
    return parsed


def _require_lease(receipt: _CredentialReceipt, required_s: float) -> None:
    elapsed = max(0.0, _clock.monotonic() - receipt.received_monotonic)
    if receipt.lease_remaining_s - elapsed <= required_s:
        raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None


def _require_child_budget_and_lease(
    deadline: float,
    receipt: _CredentialReceipt,
) -> None:
    _require_remaining(
        deadline,
        RESTIC_CHILD_TIMEOUT_S + TERM_GRACE_S + KILL_GRACE_S + FINALIZE_RESERVE_S,
    )
    _require_lease(receipt, LEASE_SPAWN_FLOOR_S)


def _prove_prefix_empty(preflight: _Preflight, credentials: HostedCredentials) -> None:
    try:
        keys, uploads = s3_wipe.list_prefix_contents(
            endpoint=credentials.endpoint,
            bucket=preflight.proof_binding.bucket,
            prefix=preflight.proof_binding.prefix,
            access_key_id=credentials.access_key_id,
            secret_access_key=credentials.secret_access_key,
            session_token=credentials.session_token,
            timeout=STORAGE_LIST_TIMEOUT_S,
            budget_s=STORAGE_LIST_TIMEOUT_S,
        )
    except Exception:
        raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None
    if keys or uploads:
        raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None


def _run_init(preflight: _Preflight, credentials: HostedCredentials) -> None:
    result = _run_restic_phase(preflight, credentials, ["init"])
    _check_restic_result(result, expect_json=False)


def _run_backup(preflight: _Preflight, credentials: HostedCredentials) -> str:
    result = _run_restic_phase(
        preflight,
        credentials,
        ["backup", "--stdin", "--stdin-filename", LOGICAL_SOURCE_PATH],
        stdin_bytes=SPB_SYNTHETIC_FIXTURE_BYTES,
    )
    _check_restic_result(result, expect_json=True)
    records = _parse_json_records(result.stdout)
    snapshot_id = _summary_snapshot_id(records)
    if snapshot_id is None:
        raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None
    return snapshot_id


def _run_ls(
    preflight: _Preflight,
    credentials: HostedCredentials,
    snapshot_id: str,
) -> None:
    result = _run_restic_phase(
        preflight,
        credentials,
        ["ls", "--long", snapshot_id],
        scrub_values=(snapshot_id,),
    )
    _check_restic_result(result, expect_json=True)
    records = _parse_json_records(result.stdout)
    _validate_ls_records(records, snapshot_id, preflight)


def _run_restore(
    preflight: _Preflight,
    credentials: HostedCredentials,
    snapshot_id: str,
) -> None:
    result = _run_restic_phase(
        preflight,
        credentials,
        ["restore", snapshot_id, "--target", str(preflight.restore_target)],
        scrub_values=(snapshot_id,),
    )
    _check_restic_result(result, expect_json=True)
    _validate_restore_records(_parse_json_records(result.stdout))


def _run_restic_phase(
    preflight: _Preflight,
    credentials: HostedCredentials,
    args: list[str],
    *,
    stdin_bytes: bytes | None = None,
    scrub_values: tuple[str, ...] = (),
) -> ResticResult:
    with hosted_append_only_restic_session(
        preflight.proof_binding,
        rclone_path=preflight.rclone_path,
        initial_credentials=credentials,
    ) as session:
        result = run_restic(
            _session_args(session, args),
            repository=session.destination.repository,
            password=preflight.daily_key,
            restic_path=preflight.restic_path,
            backend_env=session.backend_env,
            timeout=RESTIC_CHILD_TIMEOUT_S,
            process_group=True,
            stdin_bytes=stdin_bytes,
            scrub_values=(
                *preflight.scrub_values,
                session.destination.repository,
                *scrub_values,
            ),
            terminate_grace_s=TERM_GRACE_S,
            kill_grace_s=KILL_GRACE_S,
        )
    return result


def _session_args(session: HostedResticSession, args: list[str]) -> list[str]:
    return [*session.global_options, *args]


def _check_restic_result(result: ResticResult, *, expect_json: bool) -> None:
    if _PROCESS_GROUP_CLEANUP_UNVERIFIED in result.stderr:
        raise _SpbProbeError(probe_contract.REASON_CLEANUP_UNVERIFIED) from None
    if result.returncode == 124:
        raise _SpbProbeError(probe_contract.REASON_DEADLINE_EXCEEDED) from None
    if result.returncode != 0:
        raise _SpbProbeError(probe_contract.REASON_REMOTE_REJECTED) from None
    if expect_json and not result.stdout.strip():
        raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None


def _parse_json_records(text: str) -> list[object]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None
    records: list[object] = []
    try:
        for line in lines:
            records.append(
                json.loads(line, object_pairs_hook=json_codec.reject_duplicate_keys)
            )
    except (json.JSONDecodeError, ValueError):
        raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None
    return records


def _summary_snapshot_id(records: list[object]) -> str | None:
    for record in reversed(records):
        if not isinstance(record, dict) or record.get("message_type") != "summary":
            continue
        snapshot_id = record.get("snapshot_id")
        return snapshot_id if isinstance(snapshot_id, str) and snapshot_id else None
    return None


def _validate_ls_records(
    records: list[object],
    snapshot_id: str,
    preflight: _Preflight,
) -> None:
    snapshot_records = [
        record
        for record in records
        if isinstance(record, dict) and record.get("message_type") == "snapshot"
    ]
    if len(snapshot_records) != 1:
        raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None
    snapshot = snapshot_records[0]
    if snapshot.get("id") != snapshot_id or snapshot.get("paths") != [
        LOGICAL_SOURCE_PATH
    ]:
        raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None

    file_seen = False
    for record in records:
        if not isinstance(record, dict):
            raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None
        message_type = record.get("message_type")
        if message_type == "snapshot":
            continue
        if message_type != "node":
            raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None
        path = record.get("path")
        node_type = record.get("type")
        if not isinstance(path, str) or not isinstance(node_type, str):
            raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None
        if _is_physical_source_path(path, preflight):
            raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None
        if node_type == "dir":
            if path != "/spb":
                raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None
            continue
        if node_type != "file":
            raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None
        if path != LOGICAL_SOURCE_PATH:
            raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None
        size = record.get("size")
        if type(size) is not int:
            raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None
        if size != FIXTURE_LENGTH:
            raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None
        file_seen = True
    if not file_seen:
        raise _SpbProbeError(probe_contract.REASON_RESPONSE_INVALID) from None


def _validate_restore_records(records: list[object]) -> None:
    summaries = [
        record
        for record in records
        if isinstance(record, dict) and record.get("message_type") == "summary"
    ]
    if not summaries:
        return
    summary = summaries[-1]
    restored = summary.get("bytes_restored")
    if type(restored) is int and restored != FIXTURE_LENGTH:
        raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None


def _verify_restore_tree(preflight: _Preflight) -> None:
    expected = {".", "spb", "spb/source.bin"}
    observed = {"."}
    try:
        root_stat = preflight.restore_target.lstat()
    except OSError:
        raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None
    try:
        paths = list(preflight.restore_target.rglob("*"))
    except OSError:
        raise _SpbProbeInternalError() from None
    for path in paths:
        rel = path.relative_to(preflight.restore_target).as_posix()
        if _is_physical_source_path(rel, preflight):
            raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None
        try:
            stat_result = path.lstat()
        except OSError:
            raise _SpbProbeInternalError() from None
        if stat.S_ISLNK(stat_result.st_mode):
            raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None
        if stat.S_ISDIR(stat_result.st_mode):
            observed.add(rel)
            continue
        if stat.S_ISREG(stat_result.st_mode):
            observed.add(rel)
            continue
        raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None
    if observed != expected:
        raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None
    restored = preflight.restore_target / "spb" / SOURCE_FILE_NAME
    identity = _fixture_identity(restored)
    if (
        identity.length != preflight.fixture.length
        or identity.length != FIXTURE_LENGTH
        or identity.digest != preflight.fixture.digest
    ):
        raise _SpbProbeError(probe_contract.REASON_CONTENT_MISMATCH) from None


def _is_physical_source_path(path: str, preflight: _Preflight) -> bool:
    physical = preflight.spb_root.as_posix()
    return physical in path or preflight.attempt_dir.as_posix() in path


def _require_remaining(deadline: float, required_s: float) -> None:
    if _remaining(deadline) <= required_s:
        raise _SpbProbeError(probe_contract.REASON_DEADLINE_EXCEEDED) from None


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - _clock.monotonic())


def _duration_ms(start: float) -> int:
    return max(0, int((_clock.monotonic() - start) * 1000))


def _passed(*, checks: tuple[str, ...], duration_ms: int) -> _SpbProbeOutcome:
    if checks != PRIMITIVE_CHECKS:
        raise RuntimeError("SPB passed outcome requires all primitive checks")
    return _SpbProbeOutcome(
        state=probe_contract.PROOF_STATE_PASSED,
        checks=checks,
        reason=None,
        duration_ms=duration_ms,
    )


def _failed(
    *,
    reason: str,
    checks: tuple[str, ...],
    duration_ms: int,
) -> _SpbProbeOutcome:
    if reason not in SPB_FAILED_REASONS:
        raise RuntimeError("unsupported SPB failure reason")
    if PRIMITIVE_CHECKS[: len(checks)] != checks:
        raise RuntimeError("SPB failed outcome requires ordered primitive prefix")
    return _SpbProbeOutcome(
        state=probe_contract.PROOF_STATE_FAILED,
        checks=checks,
        reason=reason,
        duration_ms=duration_ms,
    )


def _scrub_values(
    *,
    binding: HostedBinding,
    proof_binding: HostedBinding,
    attempt_dir: Path,
    spb_root: Path,
    restore_target: Path,
) -> tuple[str, ...]:
    values = {
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
        LOGICAL_SOURCE_PATH,
    }
    return tuple(value for value in values if value)


__all__ = [
    "cleanup_spb_attempt_local",
    "inspect_sandbox_spb_prerequisites",
    "prove_spb_backup",
]
