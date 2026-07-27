# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Parent-side Scout provider proof primitive."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from solstone.think.models import default_model_for_provider
from solstone.think.providers.shared import (
    CANNED_GENERATE_TIMEOUT_S,
)
from solstone.think.sandbox_profile import (
    capabilities,
    envelope,
    intent,
    probe_contract,
)
from solstone.think.sandbox_profile.json_codec import reject_duplicate_keys

log = logging.getLogger(__name__)

FRAME_PROTOCOL_VERSION = 1

STDIN_FRAME_MAX_BYTES = 16_384
STDOUT_FRAME_MAX_BYTES = 4_096
STDERR_DRAIN_MAX_BYTES = 16_384
OUTBOUND_HEADER_MAX_BYTES = 16_384
OUTBOUND_BODY_MAX_BYTES = 4_096
INBOUND_RAW_HEADER_MAX_BYTES = 16_384
TRANSFERRED_ENTITY_MAX_BYTES = 65_536
DECODED_ENTITY_MAX_BYTES = 65_536

ABSOLUTE_DEADLINE_S = 40.0
WORK_CUTOFF_S = 30.0
TERM_GRACE_S = 3.0
KILL_GRACE_S = 5.0
ABSENCE_GRACE_S = 2.0

SCOUT_PROOF_PRIVATE_ROOT_NAME = "scout-provider-proof"
SCOUT_HOME_DIR_NAME = "home"
SCOUT_TMP_DIR_NAME = "tmp"
SCOUT_CWD_DIR_NAME = "cwd"
SCOUT_XDG_CACHE_DIR_NAME = "xdg-cache"
SCOUT_XDG_CONFIG_DIR_NAME = "xdg-config"
SCOUT_XDG_DATA_DIR_NAME = "xdg-data"
SCOUT_XDG_STATE_DIR_NAME = "xdg-state"

_FRAME_HEADER_BYTES = 4
_PROVIDER = "google"
_OFFICIAL_GEMINI_HOST = "generativelanguage.googleapis.com"
_CHILD_MODULE = "solstone.think.sandbox_profile.scout_provider_child"
DEFAULT_GOOGLE_MODEL = default_model_for_provider(_PROVIDER)
SCOUT_CHECKS = probe_contract.PROOF_CHECKS[probe_contract.CAPABILITY_SCOUT]
SCOUT_FAILED_REASONS = frozenset(
    set(probe_contract.PROOF_SPECIFIC_REASONS[probe_contract.CAPABILITY_SCOUT])
    | set(probe_contract.FAILED_COMMON_REASONS)
)

SCOUT_RESPONSE_SCHEMA: dict[str, object] = {
    "title": "ScoutProviderProbeResponse",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "nonce": {"type": "string"},
    },
    "required": ["nonce"],
}


class ScoutProbeError(RuntimeError):
    """Stable, non-secret probe error."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ProbeTransportError(ScoutProbeError):
    pass


class ProbeResponseInvalid(ProbeTransportError):
    def __init__(self) -> None:
        super().__init__(probe_contract.REASON_RESPONSE_INVALID)


class ProbeInternalError(ProbeTransportError):
    def __init__(self) -> None:
        super().__init__(probe_contract.REASON_INTERNAL_ERROR)


@dataclass(frozen=True, slots=True)
class ScoutProbeOutcome:
    state: str
    checks: tuple[str, ...]
    reason: str | None
    duration_ms: int

    def to_dict(self) -> dict[str, object]:
        return {
            probe_contract.FIELD_STATE: self.state,
            probe_contract.FIELD_CHECKS: self.checks,
            probe_contract.FIELD_REASON: self.reason,
            probe_contract.FIELD_DURATION_MS: self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class ScoutContainment:
    root: Path
    cwd: Path
    env: dict[str, str]


@dataclass(slots=True)
class DrainResult:
    data: bytes
    oversize: bool


class _PipeDrainer:
    def __init__(self, pipe: Any, *, cap: int) -> None:
        self._pipe = pipe
        self._cap = cap
        self._data = bytearray()
        self._oversize = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None) -> DrainResult:
        self._thread.join(timeout)
        return DrainResult(bytes(self._data), self._oversize or self._thread.is_alive())

    def _run(self) -> None:
        try:
            while True:
                chunk = self._pipe.read(4096)
                if not chunk:
                    return
                remaining = self._cap - len(self._data)
                if remaining > 0:
                    self._data.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self._oversize = True
        except OSError:
            self._oversize = True


class GeminiSingleRequestTransport(httpx.BaseTransport):
    """HTTPX transport that permits one official Gemini completion request."""

    def __init__(self, delegate: httpx.BaseTransport | None = None) -> None:
        self._delegate = delegate or httpx.HTTPTransport(retries=0)
        self.request_count = 0
        self.permitted_request: httpx.Request | None = None
        self.failure_reason: str | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        try:
            self._validate_request(request)
            response = self._delegate.handle_request(request)
            return self._validate_response(request, response)
        except ProbeTransportError as exc:
            self.failure_reason = exc.reason
            raise exc from None

    def close(self) -> None:
        self._delegate.close()

    def _validate_request(self, request: httpx.Request) -> None:
        if self.request_count != 1:
            raise ProbeInternalError()
        url = request.url
        expected_suffix = f"/models/{DEFAULT_GOOGLE_MODEL}:generateContent"
        if (
            request.method != "POST"
            or url.scheme != "https"
            or url.host != _OFFICIAL_GEMINI_HOST
            or not url.path.startswith("/v")
            or not url.path.endswith(expected_suffix)
        ):
            raise ProbeInternalError()
        header_bytes = sum(
            len(name.encode("ascii", "ignore")) + len(value.encode("utf-8")) + 4
            for name, value in request.headers.multi_items()
        )
        if header_bytes > OUTBOUND_HEADER_MAX_BYTES:
            raise ProbeInternalError()
        body = request.read()
        if len(body) > OUTBOUND_BODY_MAX_BYTES:
            raise ProbeInternalError()
        self.permitted_request = request

    def _validate_response(
        self, request: httpx.Request, response: httpx.Response
    ) -> httpx.Response:
        header_bytes = sum(
            len(name.encode("ascii", "ignore")) + len(value.encode("utf-8")) + 4
            for name, value in response.headers.multi_items()
        )
        if header_bytes > INBOUND_RAW_HEADER_MAX_BYTES:
            response.close()
            raise ProbeResponseInvalid()
        body = response.read()
        response.close()
        if len(body) > TRANSFERRED_ENTITY_MAX_BYTES:
            raise ProbeResponseInvalid()
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=body,
            request=request,
            extensions=response.extensions,
        )


def prove_scout_provider(
    journal: Path,
    *,
    attempt_dir: Path,
    cancel_requested: Callable[[], bool],
) -> dict[str, object]:
    start = time.monotonic()
    deadline = start + ABSOLUTE_DEADLINE_S
    checks: tuple[str, ...] = tuple(SCOUT_CHECKS[:0])
    outcome = _failed(
        probe_contract.REASON_INTERNAL_ERROR,
        checks,
        _duration_ms(start),
    )
    containment: ScoutContainment | None = None
    proc: subprocess.Popen[bytes] | None = None
    cleanup_failed = False

    try:
        journal_path = Path(journal)
        attempt_path = _validate_attempt_dir(journal_path, Path(attempt_dir))
        key = _ready_scout_key(journal_path)
        if key is None:
            outcome = _failed(
                probe_contract.REASON_CAPABILITY_NOT_READY,
                tuple(SCOUT_CHECKS[:0]),
                _duration_ms(start),
            )
        elif cancel_requested():
            outcome = _failed(
                probe_contract.REASON_CANCELLED,
                tuple(SCOUT_CHECKS[:0]),
                _duration_ms(start),
            )
        else:
            private_root = attempt_path / SCOUT_PROOF_PRIVATE_ROOT_NAME
            if not _cleanup_path_absent(private_root, deadline):
                outcome = _failed(
                    probe_contract.REASON_CLEANUP_UNVERIFIED,
                    tuple(SCOUT_CHECKS[:0]),
                    _duration_ms(start),
                )
            else:
                containment = _create_containment(private_root)
                work_budget = _work_budget(deadline)
                if work_budget <= 0:
                    outcome = _failed(
                        probe_contract.REASON_DEADLINE_EXCEEDED,
                        checks,
                        _duration_ms(start),
                    )
                else:
                    nonce = _new_nonce()
                    frame = encode_frame(
                        {
                            "protocol_version": FRAME_PROTOCOL_VERSION,
                            "api_key": key,
                            "nonce": nonce,
                            "timeout_s": min(CANNED_GENERATE_TIMEOUT_S, work_budget),
                        },
                        cap=STDIN_FRAME_MAX_BYTES,
                    )
                    proc = _spawn_child(containment)
                    child_result = _drive_child(
                        proc, frame, deadline, work_budget, cancel_requested
                    )
                    proc = None
                    if child_result.reason is not None:
                        outcome = _failed(
                            child_result.reason,
                            checks,
                            _duration_ms(start),
                        )
                    else:
                        outcome, checks = _outcome_from_child_frame(
                            child_result.stdout,
                            nonce=nonce,
                            start=start,
                        )
    except ScoutProbeError as exc:
        outcome = _failed(exc.reason, checks, _duration_ms(start))
    except (OSError, ValueError, TypeError):
        outcome = _failed(
            probe_contract.REASON_INTERNAL_ERROR,
            checks,
            _duration_ms(start),
        )
    finally:
        if proc is not None:
            _terminate_process(proc, deadline)
        if containment is not None:
            cleanup_failed = not _cleanup_path_absent(containment.root, deadline)
        if cleanup_failed:
            outcome = _failed(
                probe_contract.REASON_CLEANUP_UNVERIFIED,
                checks,
                _duration_ms(start),
            )
    return outcome.to_dict()


@dataclass(frozen=True, slots=True)
class _ChildDriveResult:
    stdout: bytes
    reason: str | None


def _outcome_from_child_frame(
    frame: bytes, *, nonce: str, start: float
) -> tuple[ScoutProbeOutcome, tuple[str, ...]]:
    payload = decode_frame(frame, cap=STDOUT_FRAME_MAX_BYTES)
    if payload.get("protocol_version") != FRAME_PROTOCOL_VERSION:
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)
    kind = payload.get("result")
    if kind == probe_contract.REASON_REMOTE_REJECTED:
        return (
            _failed(
                probe_contract.REASON_REMOTE_REJECTED,
                tuple(SCOUT_CHECKS[:0]),
                _duration_ms(start),
            ),
            tuple(SCOUT_CHECKS[:0]),
        )
    if kind == probe_contract.REASON_RESPONSE_INVALID:
        return (
            _failed(
                probe_contract.REASON_RESPONSE_INVALID,
                tuple(SCOUT_CHECKS[:0]),
                _duration_ms(start),
            ),
            tuple(SCOUT_CHECKS[:0]),
        )
    if kind == probe_contract.REASON_INTERNAL_ERROR:
        return (
            _failed(
                probe_contract.REASON_INTERNAL_ERROR,
                tuple(SCOUT_CHECKS[:0]),
                _duration_ms(start),
            ),
            tuple(SCOUT_CHECKS[:0]),
        )
    if kind != "ok":
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)

    checks = tuple(SCOUT_CHECKS[:1])
    expected = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    if payload.get("nonce_sha256") != expected:
        return (
            _failed(
                probe_contract.REASON_CONTENT_MISMATCH,
                checks,
                _duration_ms(start),
            ),
            checks,
        )

    checks = tuple(SCOUT_CHECKS[:2])
    if payload.get("finish_reason") != "stop":
        return (
            _failed(
                probe_contract.REASON_RESPONSE_INVALID,
                checks,
                _duration_ms(start),
            ),
            checks,
        )

    checks = tuple(SCOUT_CHECKS[:3])
    if not _usage_valid(payload.get("usage")):
        return (
            _failed(
                probe_contract.REASON_USAGE_INVALID,
                checks,
                _duration_ms(start),
            ),
            checks,
        )

    checks = tuple(SCOUT_CHECKS[:])
    return _passed(_duration_ms(start)), checks


def _usage_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    input_tokens = value.get("input_tokens")
    output_tokens = value.get("output_tokens")
    return (
        type(input_tokens) is int
        and input_tokens > 0
        and type(output_tokens) is int
        and output_tokens > 0
    )


def _ready_scout_key(journal: Path) -> str | None:
    try:
        config = capabilities._read_config(journal)
        cap = capabilities._scout_status(config, intent.load_intent(journal))
    except (OSError, ValueError):
        return None
    if cap.state != envelope.CAP_READY:
        return None
    key = config.get("env", {}).get("GOOGLE_API_KEY")
    return key if isinstance(key, str) and key else None


def _validate_attempt_dir(journal: Path, attempt_dir: Path) -> Path:
    parent = probe_contract.probe_attempts_parent_path(journal).resolve()
    try:
        current = attempt_dir.lstat()
    except OSError:
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR) from None
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)
    if stat.S_IMODE(current.st_mode) != probe_contract.ATTEMPT_DIR_MODE:
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)
    resolved = attempt_dir.resolve()
    if resolved.parent != parent:
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)
    return resolved


def _create_containment(root: Path) -> ScoutContainment:
    root.mkdir(mode=0o700)
    paths = {
        "HOME": root / SCOUT_HOME_DIR_NAME,
        "TMPDIR": root / SCOUT_TMP_DIR_NAME,
        "XDG_CACHE_HOME": root / SCOUT_XDG_CACHE_DIR_NAME,
        "XDG_CONFIG_HOME": root / SCOUT_XDG_CONFIG_DIR_NAME,
        "XDG_DATA_HOME": root / SCOUT_XDG_DATA_DIR_NAME,
        "XDG_STATE_HOME": root / SCOUT_XDG_STATE_DIR_NAME,
    }
    cwd = root / SCOUT_CWD_DIR_NAME
    for path in (*paths.values(), cwd):
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    env = {
        "LITELLM_MODE": "PRODUCTION",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        **{key: str(value) for key, value in paths.items()},
    }
    return ScoutContainment(root=root, cwd=cwd, env=env)


def _cleanup_path_absent(path: Path, deadline: float) -> bool:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    except OSError:
        return False

    stop = time.monotonic() + min(ABSENCE_GRACE_S, _remaining(deadline))
    while True:
        try:
            path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if time.monotonic() >= stop:
            return False
        time.sleep(min(0.02, max(0.0, stop - time.monotonic())))


def _work_budget(deadline: float) -> float:
    reserve = TERM_GRACE_S + KILL_GRACE_S + ABSENCE_GRACE_S
    return max(0.0, min(WORK_CUTOFF_S, deadline - time.monotonic() - reserve))


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _spawn_child(containment: ScoutContainment) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-m", _CHILD_MODULE],
        cwd=containment.cwd,
        env=containment.env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        pass_fds=(),
        start_new_session=True,
    )
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                os.set_inheritable(pipe.fileno(), False)
            except OSError:
                pass
    return proc


def _drive_child(
    proc: subprocess.Popen[bytes],
    frame: bytes,
    deadline: float,
    work_budget: float,
    cancel_requested: Callable[[], bool],
) -> _ChildDriveResult:
    if proc.stdout is None or proc.stderr is None or proc.stdin is None:
        _terminate_process(proc, deadline)
        return _ChildDriveResult(b"", probe_contract.REASON_INTERNAL_ERROR)
    stdout = _PipeDrainer(proc.stdout, cap=STDOUT_FRAME_MAX_BYTES + _FRAME_HEADER_BYTES)
    stderr = _PipeDrainer(proc.stderr, cap=STDERR_DRAIN_MAX_BYTES)
    stdout.start()
    stderr.start()
    try:
        proc.stdin.write(frame)
        proc.stdin.close()
    except OSError:
        _terminate_process(proc, deadline)
        return _ChildDriveResult(b"", probe_contract.REASON_INTERNAL_ERROR)

    work_until = min(time.monotonic() + work_budget, deadline)
    reason: str | None = None
    while proc.poll() is None:
        if cancel_requested():
            reason = probe_contract.REASON_CANCELLED
            break
        if time.monotonic() >= work_until or _remaining(deadline) <= 0:
            reason = probe_contract.REASON_DEADLINE_EXCEEDED
            break
        time.sleep(min(0.02, max(0.0, work_until - time.monotonic())))

    if reason is not None:
        _terminate_process(proc, deadline)

    join_timeout = min(ABSENCE_GRACE_S, _remaining(deadline))
    out = stdout.join(join_timeout)
    err = stderr.join(min(ABSENCE_GRACE_S, _remaining(deadline)))
    if reason is not None:
        return _ChildDriveResult(out.data, reason)
    if out.oversize or err.oversize or proc.returncode not in (0, None):
        return _ChildDriveResult(out.data, probe_contract.REASON_INTERNAL_ERROR)
    return _ChildDriveResult(out.data, None)


def _terminate_process(proc: subprocess.Popen[bytes], deadline: float) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    _wait_bounded(proc, TERM_GRACE_S, deadline)
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    _wait_bounded(proc, KILL_GRACE_S, deadline)


def _wait_bounded(
    proc: subprocess.Popen[bytes],
    reserve: float,
    deadline: float,
) -> None:
    timeout = min(reserve, _remaining(deadline))
    if timeout <= 0:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return


def encode_frame(payload: dict[str, object], *, cap: int) -> bytes:
    data = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(data) > cap:
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)
    return len(data).to_bytes(_FRAME_HEADER_BYTES, "big") + data


def decode_frame(data: bytes, *, cap: int) -> dict[str, Any]:
    if len(data) < _FRAME_HEADER_BYTES:
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)
    length = int.from_bytes(data[:_FRAME_HEADER_BYTES], "big")
    if length > cap or len(data) != _FRAME_HEADER_BYTES + length:
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)
    try:
        payload = json.loads(
            data[_FRAME_HEADER_BYTES:].decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR) from None
    if not isinstance(payload, dict):
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)
    return payload


def decode_model_json(text: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > DECODED_ENTITY_MAX_BYTES:
        raise ScoutProbeError(probe_contract.REASON_RESPONSE_INVALID)
    try:
        payload = json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError):
        raise ScoutProbeError(probe_contract.REASON_RESPONSE_INVALID) from None
    if not isinstance(payload, dict):
        raise ScoutProbeError(probe_contract.REASON_RESPONSE_INVALID)
    if set(payload) != {"nonce"} or not isinstance(payload.get("nonce"), str):
        raise ScoutProbeError(probe_contract.REASON_RESPONSE_INVALID)
    return payload


def scout_prompt(nonce: str) -> str:
    return (
        "Return exactly one JSON object matching the provided schema. "
        f"Set nonce to this exact value: {nonce}"
    )


def _passed(duration_ms: int) -> ScoutProbeOutcome:
    return ScoutProbeOutcome(
        state=probe_contract.PROOF_STATE_PASSED,
        checks=tuple(SCOUT_CHECKS[:]),
        reason=None,
        duration_ms=duration_ms,
    )


def _failed(
    reason: str, checks: tuple[str, ...], duration_ms: int
) -> ScoutProbeOutcome:
    if reason not in SCOUT_FAILED_REASONS:
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)
    return ScoutProbeOutcome(
        state=probe_contract.PROOF_STATE_FAILED,
        checks=checks,
        reason=reason,
        duration_ms=duration_ms,
    )


def _duration_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))


def _new_nonce() -> str:
    return hashlib.sha256(os.urandom(32)).hexdigest()
