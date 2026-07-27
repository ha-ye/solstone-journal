# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Capture-mode restic subprocess runner for solstone backup."""

from __future__ import annotations

import json as json_module
import os
import signal
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solstone.think import json_codec

_PROCESS_GROUP_CLEANUP_UNVERIFIED = "process_group_cleanup_unverified"
_RESTIC_JSON_STDOUT_REDACTED = "[redacted restic json stdout]"
_RESTIC_JSON_STDERR_REDACTED = "[redacted restic stderr]"


@dataclass(frozen=True)
class ResticResult:
    returncode: int
    stdout: str
    stderr: str
    json: Any | None
    argv: tuple[str, ...]


@dataclass(frozen=True)
class _RawResticProcessResult:
    returncode: int
    stdout: bytes | None
    stderr: bytes | None
    cleanup_verified: bool


class ResticJsonRecordsResult:
    __slots__ = ("_argv", "_records", "_returncode", "_stderr", "_stdout")

    def __init__(
        self,
        *,
        returncode: int,
        stdout: str,
        stderr: str,
        argv: tuple[str, ...],
        records: tuple[object, ...] | None,
    ) -> None:
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._argv = argv
        self._records = records

    @property
    def returncode(self) -> int:
        return self._returncode

    @property
    def stdout(self) -> str:
        return self._stdout

    @property
    def stderr(self) -> str:
        return self._stderr

    @property
    def argv(self) -> tuple[str, ...]:
        return self._argv

    @property
    def has_records(self) -> bool:
        return self._records is not None

    def consume_records(self) -> tuple[object, ...]:
        records = self._records
        if records is None:
            raise TypeError("restic JSON records are unavailable")
        self._records = None
        return records

    def __repr__(self) -> str:
        state = "available" if self.has_records else "closed"
        return f"ResticJsonRecordsResult(state={state}, <redacted>)"

    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)

    def __reduce__(self) -> object:
        raise TypeError("ResticJsonRecordsResult is not serializable")

    def __copy__(self) -> object:
        raise TypeError("ResticJsonRecordsResult is not copyable")

    def __deepcopy__(self, _memo: dict[int, object]) -> object:
        raise TypeError("ResticJsonRecordsResult is not copyable")


def _scrub(text: str, secrets: Iterable[str | None]) -> str:
    scrubbed = text
    for secret in secrets:
        if secret:
            scrubbed = "[redacted]".join(scrubbed.split(secret))
    return scrubbed


def _child_env(
    repository: str,
    password: str,
    backend_env: Mapping[str, str | None] | None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    env = {
        key: value
        for key in ("PATH", "HOME", "TMPDIR")
        if (value := os.environ.get(key)) is not None
    }
    env["RESTIC_REPOSITORY"] = repository
    env["RESTIC_PASSWORD"] = password
    if backend_env:
        env.update(
            {key: value for key, value in backend_env.items() if value is not None}
        )

    secret_values = [password]
    if backend_env:
        secret_values.extend(value for value in backend_env.values() if value)
    return env, tuple(secret_values)


def _build_argv(
    restic_path: Path,
    args: Sequence[str],
    json: bool,
    max_repack_size: str | None,
) -> list[str]:
    argv = [str(restic_path), *args]
    if json:
        argv.append("--json")
    if max_repack_size:
        argv.extend(["--max-repack-size", max_repack_size])
    return argv


def _guard_argv(argv: Sequence[str], secrets: Iterable[str]) -> None:
    if "--insecure-tls" in argv:
        raise RuntimeError("restic --insecure-tls is forbidden")
    secret_values = tuple(secret for secret in secrets if secret)
    for token in argv:
        for secret in secret_values:
            if secret in token:
                raise RuntimeError("restic argv contains a secret")


def _parse_json(text: str) -> Any | None:
    if not text.strip():
        return None
    try:
        return json_module.loads(text)
    except json_module.JSONDecodeError:
        pass

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    parsed: list[Any] = []
    for line in lines:
        try:
            parsed.append(json_module.loads(line))
        except json_module.JSONDecodeError:
            return None
    return parsed


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _parse_json_records(raw_stdout: bytes | None) -> tuple[object, ...] | None:
    if not raw_stdout:
        return None
    try:
        text = raw_stdout.decode("utf-8")
        lines = text.splitlines()
        if not lines:
            return None
        records: list[object] = []
        for line in lines:
            if not line.strip():
                return None
            records.append(
                json_module.loads(
                    line,
                    object_pairs_hook=json_codec.reject_duplicate_keys,
                    parse_constant=_reject_json_constant,
                )
            )
        if not records:
            return None
        return tuple(records)
    except (
        UnicodeDecodeError,
        json_module.JSONDecodeError,
        ValueError,
        RecursionError,
    ):
        return None


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _wait_timeout(proc: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _terminate_process_group(
    proc: subprocess.Popen[bytes],
    *,
    terminate_grace_s: float,
    kill_grace_s: float,
) -> bool:
    if proc.poll() is not None:
        return True
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    if _wait_timeout(proc, terminate_grace_s):
        return _process_group_absent(proc.pid)

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    if not _wait_timeout(proc, kill_grace_s):
        return False
    return _process_group_absent(proc.pid)


def _process_group_absent(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _run_restic_popen(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float | None,
    pass_fds: tuple[int, ...],
    process_group: bool,
    stdin_bytes: bytes | None,
    terminate_grace_s: float,
    kill_grace_s: float,
) -> _RawResticProcessResult:
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        close_fds=True,
        pass_fds=pass_fds,
        start_new_session=process_group,
    )
    try:
        raw_stdout, raw_stderr = proc.communicate(input=stdin_bytes, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        cleanup_verified = True
        if process_group:
            cleanup_verified = _terminate_process_group(
                proc,
                terminate_grace_s=terminate_grace_s,
                kill_grace_s=kill_grace_s,
            )
        else:
            proc.kill()
            cleanup_verified = _wait_timeout(proc, kill_grace_s)
        try:
            raw_stdout, raw_stderr = proc.communicate(timeout=0)
        except subprocess.TimeoutExpired:
            raw_stdout = exc.output
            raw_stderr = exc.stderr
        return _RawResticProcessResult(
            returncode=124,
            stdout=raw_stdout,
            stderr=raw_stderr,
            cleanup_verified=cleanup_verified,
        )
    return _RawResticProcessResult(
        returncode=proc.returncode,
        stdout=raw_stdout,
        stderr=raw_stderr,
        cleanup_verified=True,
    )


def reason_for_returncode(returncode: int) -> str:
    return {
        3: "incomplete",
        10: "repo_missing",
        11: "locked",
        12: "auth_failed",
        124: "timeout",
    }.get(returncode, "failed")


def select_summary(parsed: Any) -> dict[str, Any] | None:
    if isinstance(parsed, dict) and parsed.get("message_type") == "summary":
        return parsed

    if isinstance(parsed, list):
        for record in reversed(parsed):
            if isinstance(record, dict) and record.get("message_type") == "summary":
                return record
    return None


def run_restic(
    args: Sequence[str],
    *,
    repository: str,
    password: str,
    restic_path: Path,
    backend_env: Mapping[str, str | None] | None = None,
    json: bool = False,
    max_repack_size: str | None = None,
    timeout: float | None = None,
    pass_fds: tuple[int, ...] = (),
    process_group: bool = False,
    stdin_bytes: bytes | None = None,
    scrub_values: Iterable[str | None] = (),
    terminate_grace_s: float = 3.0,
    kill_grace_s: float = 5.0,
) -> ResticResult:
    env, secrets = _child_env(repository, password, backend_env)
    scrub_secrets = (*secrets, *tuple(scrub_values))
    argv = _build_argv(restic_path, args, json, max_repack_size)
    _guard_argv(argv, secrets)
    safe_argv = tuple(_scrub(token, scrub_secrets) for token in argv)

    # Long-running/streaming backup mode is deferred: ManagedProcess.spawn
    # writes raw child stdout/stderr to health logs and the callosum logs tract,
    # and restic output may include presigned backend URLs or repo strings.
    if not process_group and stdin_bytes is None:
        try:
            result = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout,
                pass_fds=pass_fds,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _scrub(_timeout_text(exc.stdout), scrub_secrets)
            stderr = _scrub(_timeout_text(exc.stderr), scrub_secrets)
            return ResticResult(
                returncode=124,
                stdout=stdout,
                stderr=stderr,
                json=None,
                argv=safe_argv,
            )

        stdout = _scrub(result.stdout or "", scrub_secrets)
        stderr = _scrub(result.stderr or "", scrub_secrets)
        parsed_json = _parse_json(stdout) if json else None
        return ResticResult(
            returncode=result.returncode,
            stdout=stdout,
            stderr=stderr,
            json=parsed_json,
            argv=safe_argv,
        )

    raw_result = _run_restic_popen(
        argv,
        env=env,
        timeout=timeout,
        pass_fds=pass_fds,
        process_group=process_group,
        stdin_bytes=stdin_bytes,
        terminate_grace_s=terminate_grace_s,
        kill_grace_s=kill_grace_s,
    )
    stderr_text = _decode_output(raw_result.stderr)
    if not raw_result.cleanup_verified:
        stderr_text = f"{stderr_text}\n{_PROCESS_GROUP_CLEANUP_UNVERIFIED}"
    stdout = _scrub(_decode_output(raw_result.stdout), scrub_secrets)
    stderr = _scrub(stderr_text, scrub_secrets)
    if raw_result.returncode == 124:
        parsed_json = None
    else:
        parsed_json = _parse_json(stdout) if json else None
    return ResticResult(
        returncode=raw_result.returncode,
        stdout=stdout,
        stderr=stderr,
        json=parsed_json,
        argv=safe_argv,
    )


def run_restic_json_records(
    args: Sequence[str],
    *,
    repository: str,
    password: str,
    restic_path: Path,
    backend_env: Mapping[str, str | None] | None = None,
    timeout: float | None = None,
    stdin_bytes: bytes | None = None,
    scrub_values: Iterable[str | None] = (),
    terminate_grace_s: float = 3.0,
    kill_grace_s: float = 5.0,
) -> ResticJsonRecordsResult:
    env, secrets = _child_env(repository, password, backend_env)
    scrub_secrets = (*secrets, *tuple(scrub_values))
    argv = _build_argv(restic_path, args, json=True, max_repack_size=None)
    _guard_argv(argv, secrets)
    safe_argv = tuple(_scrub(token, scrub_secrets) for token in argv)

    raw_result = _run_restic_popen(
        argv,
        env=env,
        timeout=timeout,
        pass_fds=(),
        process_group=True,
        stdin_bytes=stdin_bytes,
        terminate_grace_s=terminate_grace_s,
        kill_grace_s=kill_grace_s,
    )
    returncode = raw_result.returncode
    cleanup_verified = raw_result.cleanup_verified
    raw_stdout = raw_result.stdout
    raw_stderr = raw_result.stderr
    del raw_result

    stdout_present = bool(raw_stdout)
    stderr_present = bool(raw_stderr)
    records = (
        _parse_json_records(raw_stdout)
        if returncode == 0 and cleanup_verified
        else None
    )
    raw_stdout = None
    raw_stderr = None

    stdout = _RESTIC_JSON_STDOUT_REDACTED if stdout_present else ""
    if not cleanup_verified:
        stderr = f"{_RESTIC_JSON_STDERR_REDACTED}\n{_PROCESS_GROUP_CLEANUP_UNVERIFIED}"
    elif stderr_present:
        stderr = _RESTIC_JSON_STDERR_REDACTED
    else:
        stderr = ""
    return ResticJsonRecordsResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        argv=safe_argv,
        records=records,
    )


__all__ = [
    "ResticResult",
    "ResticJsonRecordsResult",
    "reason_for_returncode",
    "run_restic",
    "run_restic_json_records",
    "select_summary",
]
