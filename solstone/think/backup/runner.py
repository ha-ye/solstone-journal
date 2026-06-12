# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Capture-mode restic subprocess runner for sol private backup."""

from __future__ import annotations

import json as json_module
import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ResticResult:
    returncode: int
    stdout: str
    stderr: str
    json: Any | None
    argv: tuple[str, ...]


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


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


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
) -> ResticResult:
    env, secrets = _child_env(repository, password, backend_env)
    argv = _build_argv(restic_path, args, json, max_repack_size)
    _guard_argv(argv, secrets)
    safe_argv = tuple(argv)

    # Long-running/streaming backup mode is deferred: ManagedProcess.spawn
    # writes raw child stdout/stderr to health logs and the callosum logs tract,
    # and restic output may include presigned backend URLs or repo strings.
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
        stdout = _scrub(_timeout_text(exc.stdout), secrets)
        stderr = _scrub(_timeout_text(exc.stderr), secrets)
        return ResticResult(
            returncode=124,
            stdout=stdout,
            stderr=stderr,
            json=None,
            argv=safe_argv,
        )

    stdout = _scrub(result.stdout or "", secrets)
    stderr = _scrub(result.stderr or "", secrets)
    parsed_json = _parse_json(stdout) if json else None
    return ResticResult(
        returncode=result.returncode,
        stdout=stdout,
        stderr=stderr,
        json=parsed_json,
        argv=safe_argv,
    )


__all__ = [
    "ResticResult",
    "reason_for_returncode",
    "run_restic",
    "select_summary",
]
