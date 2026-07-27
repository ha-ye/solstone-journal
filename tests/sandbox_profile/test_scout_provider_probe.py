# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from solstone.think.sandbox_profile import probe_contract
from solstone.think.sandbox_profile import scout_provider_probe as probe
from tests.sandbox_profile import (
    invoke,
    prepare_ok,
    read_json,
    sandbox_journal,
    scout_payload,
    write_attempt_dir,
)

CANARY_KEY = "scout-key-canary-value"
CANARY_DISPATCH_TOKEN = "dispatch-token-canary"
CANARY_ACCOUNT_ID = "acct-scout-canary"
CANARY_NONCE = "nonce-canary-value"
CANARY_INPUT_TOKENS = 1_234_567
CANARY_OUTPUT_TOKENS = 7_654_321
CANARY_USAGE_STRINGS = (str(CANARY_INPUT_TOKENS), str(CANARY_OUTPUT_TOKENS))
CANARY_KEY_FINGERPRINT = hashlib.sha256(CANARY_KEY.encode("utf-8")).hexdigest()
CANARY_MODEL_OUTPUT = json.dumps(
    {"nonce": CANARY_NONCE},
    separators=(",", ":"),
)
CANARY_REQUEST_URL = (
    "https://generativelanguage.googleapis.com/v1alpha/models/"
    "gemini-3.5-flash:generateContent"
)
CANARIES = (
    CANARY_KEY,
    "x-goog-api-key",
    CANARY_NONCE,
    probe.scout_prompt(CANARY_NONCE),
    CANARY_MODEL_OUTPUT,
    CANARY_DISPATCH_TOKEN,
    CANARY_ACCOUNT_ID,
    CANARY_KEY_FINGERPRINT,
    "gemini-3.5-flash",
    "gemini",
    CANARY_REQUEST_URL,
    *CANARY_USAGE_STRINGS,
)


def _ready_scout_journal(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    journal = sandbox_journal(tmp_path, monkeypatch)
    prepare_ok(journal)
    payload = scout_payload(CANARY_KEY)
    payload["dispatch_token"] = CANARY_DISPATCH_TOKEN
    payload["account_id"] = CANARY_ACCOUNT_ID
    apply = invoke(["apply", "scout", "--json"], input_text=json.dumps(payload))
    assert apply.exit_code == 0, apply.output
    return journal, write_attempt_dir(journal)


# The pristine-parent absence invariant — that proving Scout pulls no provider
# module into the calling interpreter — is only meaningful when that interpreter
# started pristine, which a shared pytest worker never is. Deleting the modules to
# manufacture that precondition corrupted every later test in the worker: a
# re-import rebuilds openhands' classes, so pydantic then rejects a live ``LLM`` as
# not an ``LLM``. The invariant is proved at full strength in a dedicated
# subprocess instead (see ``test_pristine_parent_imports_no_provider_modules``);
# in-process checks stay scoped to state this process legitimately owns.
def _snapshot(journal: Path) -> tuple[dict[str, str], bytes]:
    return dict(os.environ), (journal / "config" / "journal.json").read_bytes()


def _assert_snapshot_unchanged(
    journal: Path,
    snapshot: tuple[dict[str, str], bytes],
) -> None:
    before_env, before_config = snapshot
    assert dict(os.environ) == before_env
    assert (journal / "config" / "journal.json").read_bytes() == before_config


def _assert_attempt_empty(attempt: Path) -> None:
    assert list(attempt.iterdir()) == []


def _assert_canaries_absent(text: str) -> None:
    for canary in CANARIES:
        assert canary not in text


def _assert_outcome_canaries_absent(outcome: dict[str, object]) -> None:
    _assert_canaries_absent(json.dumps(outcome, sort_keys=True, default=str))


def _assert_child_env_canary_clean(env: dict[str, str]) -> None:
    expected = {
        "HOME",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "LITELLM_MODE",
        "LITELLM_LOCAL_MODEL_COST_MAP",
    }
    assert set(env) == expected
    for key, value in env.items():
        _assert_canaries_absent(key)
        _assert_canaries_absent(value)
    for forbidden in (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "GEMINI_API_BASE",
        "GOOGLE_API_KEY",
    ):
        assert forbidden not in env
    assert not any(key.endswith(("_PROXY", "_proxy")) for key in env)


def _assert_child_stdin_asymmetry(frame: bytes) -> None:
    assert CANARY_KEY.encode("utf-8") in frame
    assert CANARY_NONCE.encode("utf-8") in frame
    assert CANARY_DISPATCH_TOKEN.encode("utf-8") not in frame
    assert CANARY_ACCOUNT_ID.encode("utf-8") not in frame
    assert CANARY_KEY_FINGERPRINT.encode("utf-8") not in frame


def _assert_proof_path_names_canary_clean(captured: dict[str, Any]) -> None:
    paths = [Path(captured["cwd"]), Path(captured["cwd"]).parent]
    paths.extend(Path(value) for value in captured["env"].values())
    for path in paths:
        _assert_canaries_absent(path.name)
        _assert_canaries_absent(str(path))


def _assert_surviving_attempt_files_canary_clean(attempt: Path) -> None:
    # The applied Scout key retained in journal/config/journal.json is sanctioned.
    # This sweep is scoped to proof-created attempt state only.
    for path in attempt.rglob("*"):
        _assert_canaries_absent(path.name)
        _assert_canaries_absent(str(path))
        if path.is_file():
            data = path.read_bytes()
            for canary in CANARIES:
                assert canary.encode("utf-8") not in data


def _forbid_spawn(_containment: probe.ScoutContainment):
    raise AssertionError("Scout provider proof spawned a child")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _ok_child_code() -> str:
    return f"""
import hashlib
import sys
from solstone.think.sandbox_profile.scout_provider_probe import (
    FRAME_PROTOCOL_VERSION,
    STDIN_FRAME_MAX_BYTES,
    STDOUT_FRAME_MAX_BYTES,
    decode_frame,
    encode_frame,
)

payload = decode_frame(sys.stdin.buffer.read(), cap=STDIN_FRAME_MAX_BYTES)
nonce = payload["nonce"]
out = {{
    "protocol_version": FRAME_PROTOCOL_VERSION,
    "result": "ok",
    "nonce_sha256": hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
    "finish_reason": "stop",
    "usage": {{
        "input_tokens": {CANARY_INPUT_TOKENS},
        "output_tokens": {CANARY_OUTPUT_TOKENS},
    }},
}}
sys.stdout.buffer.write(encode_frame(out, cap=STDOUT_FRAME_MAX_BYTES))
sys.stdout.buffer.flush()
"""


def _stderr_flood_child_code() -> str:
    return """
import os
import sys
os.write(2, b"x" * 200000)
sys.stderr.flush()
""" + _ok_child_code()


def _sleep_child_code() -> str:
    return """
import time
time.sleep(60)
"""


def _silent_child_code() -> str:
    return """
import sys
sys.stdin.buffer.read()
"""


def _oversize_stdout_child_code() -> str:
    length = probe.STDOUT_FRAME_MAX_BYTES + 1
    return f"""
import sys
sys.stdin.buffer.read()
sys.stdout.buffer.write({length}.to_bytes(4, "big") + b"x" * {length})
sys.stdout.buffer.flush()
"""


def _spawn_code(code: str, captured: dict[str, Any]):
    def spawn(containment: probe.ScoutContainment):
        captured["env"] = dict(containment.env)
        captured["cwd"] = containment.cwd
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=containment.cwd,
            env=containment.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(),
            start_new_session=True,
        )
        captured["pid"] = proc.pid
        return proc

    return spawn


def test_parent_success_uses_contained_env(tmp_path, monkeypatch, caplog) -> None:
    journal, attempt = _ready_scout_journal(tmp_path, monkeypatch)
    before_env = dict(os.environ)
    before_config = (journal / "config" / "journal.json").read_bytes()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(probe, "_spawn_child", _spawn_code(_ok_child_code(), captured))
    monkeypatch.setattr(probe, "_new_nonce", lambda: CANARY_NONCE)

    outcome = probe.prove_scout_provider(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert outcome["state"] == probe_contract.PROOF_STATE_PASSED
    assert outcome["reason"] is None
    assert outcome["checks"] == probe_contract.PROOF_CHECKS["scout"]
    assert dict(os.environ) == before_env
    assert (journal / "config" / "journal.json").read_bytes() == before_config
    assert set(captured["env"]) == {
        "HOME",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "LITELLM_MODE",
        "LITELLM_LOCAL_MODEL_COST_MAP",
    }
    assert captured["env"]["LITELLM_MODE"] == "PRODUCTION"
    assert captured["env"]["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    for forbidden in (
        "PATH",
        "GOOGLE_API_KEY",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        assert forbidden not in captured["env"]
    assert list(attempt.iterdir()) == []
    _assert_canaries_absent(repr(outcome))
    _assert_canaries_absent(caplog.text)


@pytest.mark.parametrize(
    "case",
    [
        "missing_scout_block",
        "missing_recorded_account_id",
        "mismatched_account_id",
        "mismatched_key_fingerprint_sha256",
        "empty_key",
        "missing_key",
    ],
)
def test_parent_refuses_unowned_scout_without_spawning(
    tmp_path,
    monkeypatch,
    case: str,
) -> None:
    journal, attempt = _ready_scout_journal(tmp_path, monkeypatch)
    config_path = journal / "config" / "journal.json"
    intent_path = journal / "health" / "sandbox-profile" / "intent.json"
    config = read_json(config_path)
    intent_payload = read_json(intent_path)

    if case == "missing_scout_block":
        config["services"].pop("scout", None)
        _write_json(config_path, config)
    elif case == "missing_recorded_account_id":
        intent_payload["observed_at_apply"]["scout"].pop("account_id", None)
        _write_json(intent_path, intent_payload)
    elif case == "mismatched_account_id":
        config["services"]["scout"]["account_id"] = "acct-other"
        _write_json(config_path, config)
    elif case == "mismatched_key_fingerprint_sha256":
        intent_payload["observed_at_apply"]["scout"]["key_fingerprint_sha256"] = (
            "0" * 64
        )
        _write_json(intent_path, intent_payload)
    elif case == "empty_key":
        config["env"]["GOOGLE_API_KEY"] = ""
        _write_json(config_path, config)
    elif case == "missing_key":
        config["env"].pop("GOOGLE_API_KEY", None)
        _write_json(config_path, config)
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(case)

    snapshot = _snapshot(journal)
    monkeypatch.setattr(probe, "_spawn_child", _forbid_spawn)

    outcome = probe.prove_scout_provider(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert outcome["reason"] == probe_contract.REASON_CAPABILITY_NOT_READY
    assert outcome["checks"] == probe.SCOUT_CHECKS[:0]
    _assert_snapshot_unchanged(journal, snapshot)
    _assert_attempt_empty(attempt)


def test_parent_cancels_before_contact_without_spawning(tmp_path, monkeypatch) -> None:
    journal, attempt = _ready_scout_journal(tmp_path, monkeypatch)
    snapshot = _snapshot(journal)
    monkeypatch.setattr(probe, "_spawn_child", _forbid_spawn)

    outcome = probe.prove_scout_provider(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: True,
    )

    assert outcome["reason"] == probe_contract.REASON_CANCELLED
    assert outcome["checks"] == probe.SCOUT_CHECKS[:0]
    _assert_snapshot_unchanged(journal, snapshot)
    _assert_attempt_empty(attempt)


def test_parent_cancellation_after_earned_facts_preserves_prefix(
    tmp_path,
    monkeypatch,
) -> None:
    journal, attempt = _ready_scout_journal(tmp_path, monkeypatch)
    snapshot = _snapshot(journal)
    nonce = "earned-cancel-nonce"
    monkeypatch.setattr(probe, "_new_nonce", lambda: nonce)
    monkeypatch.setattr(probe, "_spawn_child", lambda _containment: object())

    def drive(_proc, _frame, _deadline, _work_budget, _cancel_requested):
        return probe._ChildDriveResult(
            _child_frame(
                {
                    "protocol_version": 1,
                    "result": "ok",
                    "nonce_sha256": hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            ),
            None,
        )

    calls = {"count": 0}

    def cancel_requested() -> bool:
        calls["count"] += 1
        return calls["count"] > 1

    monkeypatch.setattr(probe, "_drive_child", drive)

    outcome = probe.prove_scout_provider(
        journal,
        attempt_dir=attempt,
        cancel_requested=cancel_requested,
    )

    assert outcome["reason"] == probe_contract.REASON_CANCELLED
    assert outcome["checks"] == probe.SCOUT_CHECKS[:]
    _assert_snapshot_unchanged(journal, snapshot)
    _assert_attempt_empty(attempt)


def test_parent_cleanup_unverified_overrides_post_fact_cancellation(
    tmp_path,
    monkeypatch,
) -> None:
    journal, attempt = _ready_scout_journal(tmp_path, monkeypatch)
    snapshot = _snapshot(journal)
    nonce = "cleanup-cancel-nonce"
    monkeypatch.setattr(probe, "_new_nonce", lambda: nonce)
    monkeypatch.setattr(probe, "_spawn_child", lambda _containment: object())

    def drive(_proc, _frame, _deadline, _work_budget, _cancel_requested):
        return probe._ChildDriveResult(
            _child_frame(
                {
                    "protocol_version": 1,
                    "result": "ok",
                    "nonce_sha256": hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            ),
            None,
        )

    calls = {"cancel": 0, "cleanup": 0}

    def cancel_requested() -> bool:
        calls["cancel"] += 1
        return calls["cancel"] > 1

    real_cleanup = probe._cleanup_path_absent

    def cleanup(path: Path, deadline: float) -> bool:
        calls["cleanup"] += 1
        cleaned = real_cleanup(path, deadline)
        return cleaned and calls["cleanup"] != 2

    monkeypatch.setattr(probe, "_drive_child", drive)
    monkeypatch.setattr(probe, "_cleanup_path_absent", cleanup)

    outcome = probe.prove_scout_provider(
        journal,
        attempt_dir=attempt,
        cancel_requested=cancel_requested,
    )

    assert outcome["reason"] == probe_contract.REASON_CLEANUP_UNVERIFIED
    assert outcome["checks"] == probe.SCOUT_CHECKS[:]
    _assert_snapshot_unchanged(journal, snapshot)
    _assert_attempt_empty(attempt)


def test_parent_cleanup_unverified_overrides_success(tmp_path, monkeypatch) -> None:
    journal, attempt = _ready_scout_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(probe, "_spawn_child", _spawn_code(_ok_child_code(), {}))
    calls = {"count": 0}

    def cleanup(path: Path, deadline: float) -> bool:
        calls["count"] += 1
        return calls["count"] == 1

    monkeypatch.setattr(probe, "_cleanup_path_absent", cleanup)

    outcome = probe.prove_scout_provider(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert outcome["state"] == probe_contract.PROOF_STATE_FAILED
    assert outcome["reason"] == probe_contract.REASON_CLEANUP_UNVERIFIED
    assert outcome["checks"] == probe_contract.PROOF_CHECKS["scout"]


def test_parent_canaries_absent_from_debug_logs_argv_and_outcomes(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    journal, attempt = _ready_scout_journal(tmp_path, monkeypatch)
    config_path = journal / "config" / "journal.json"
    outcomes: list[dict[str, object]] = []
    caplog.clear()

    with caplog.at_level(logging.DEBUG):
        monkeypatch.setattr(probe, "_spawn_child", _spawn_code(_ok_child_code(), {}))
        monkeypatch.setattr(probe, "_new_nonce", lambda: CANARY_NONCE)
        outcomes.append(
            probe.prove_scout_provider(
                journal,
                attempt_dir=attempt,
                cancel_requested=lambda: False,
            )
        )

        monkeypatch.setattr(
            probe,
            "_spawn_child",
            _spawn_code("import sys\nsys.stdout.buffer.write(b'bad-frame')\n", {}),
        )
        outcomes.append(
            probe.prove_scout_provider(
                journal,
                attempt_dir=attempt,
                cancel_requested=lambda: False,
            )
        )

        config = read_json(config_path)
        config["env"]["GOOGLE_API_KEY"] = ""
        _write_json(config_path, config)
        monkeypatch.setattr(probe, "_spawn_child", _forbid_spawn)
        outcomes.append(
            probe.prove_scout_provider(
                journal,
                attempt_dir=attempt,
                cancel_requested=lambda: False,
            )
        )

    assert outcomes[0]["state"] == probe_contract.PROOF_STATE_PASSED
    assert outcomes[1]["state"] == probe_contract.PROOF_STATE_FAILED
    assert outcomes[2]["reason"] == probe_contract.REASON_CAPABILITY_NOT_READY
    _assert_canaries_absent(caplog.text)
    _assert_canaries_absent(" ".join(sys.argv))
    for outcome in outcomes:
        _assert_outcome_canaries_absent(outcome)


def test_primitive_exceptions_are_canary_clean() -> None:
    exceptions: list[BaseException] = []

    with pytest.raises(probe.ScoutProbeError) as excinfo:
        probe.decode_frame(b"", cap=probe.STDIN_FRAME_MAX_BYTES)
    exceptions.append(excinfo.value)

    transport = probe.GeminiSingleRequestTransport()
    with pytest.raises(probe.ProbeInternalError) as excinfo:
        transport.handle_request(
            httpx.Request(
                "GET",
                CANARY_REQUEST_URL,
                headers={"x-goog-api-key": CANARY_KEY},
                content=CANARY_MODEL_OUTPUT.encode("utf-8"),
            )
        )
    exceptions.append(excinfo.value)
    transport.close()

    inbound = probe.GeminiSingleRequestTransport(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"x-test": "x" * (probe.INBOUND_RAW_HEADER_MAX_BYTES + 1)},
                request=request,
            )
        )
    )
    with pytest.raises(probe.ProbeResponseInvalid) as excinfo:
        inbound.handle_request(
            httpx.Request(
                "POST",
                CANARY_REQUEST_URL,
                headers={"x-goog-api-key": CANARY_KEY},
                content=b"{}",
            )
        )
    exceptions.append(excinfo.value)
    inbound.close()

    for exc in exceptions:
        assert exc.__cause__ is None
        _assert_canaries_absent(str(exc))
        _assert_canaries_absent(repr(exc))


def test_parent_canaries_absent_from_child_env_stdin_paths_and_attempt_files(
    tmp_path,
    monkeypatch,
) -> None:
    journal, attempt = _ready_scout_journal(tmp_path, monkeypatch)
    config_path = journal / "config" / "journal.json"
    real_drive_child = probe._drive_child
    pass_capture: dict[str, Any] = {}
    failure_capture: dict[str, Any] = {}

    def capture_drive(captured: dict[str, Any]):
        def drive(proc, frame, deadline, work_budget, cancel_requested):
            captured["stdin"] = frame
            return real_drive_child(
                proc,
                frame,
                deadline,
                work_budget,
                cancel_requested,
            )

        return drive

    monkeypatch.setattr(probe, "_new_nonce", lambda: CANARY_NONCE)
    monkeypatch.setattr(
        probe, "_spawn_child", _spawn_code(_ok_child_code(), pass_capture)
    )
    monkeypatch.setattr(probe, "_drive_child", capture_drive(pass_capture))
    pass_outcome = probe.prove_scout_provider(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert pass_outcome["state"] == probe_contract.PROOF_STATE_PASSED
    _assert_child_env_canary_clean(pass_capture["env"])
    _assert_child_stdin_asymmetry(pass_capture["stdin"])
    _assert_proof_path_names_canary_clean(pass_capture)
    _assert_surviving_attempt_files_canary_clean(attempt)

    monkeypatch.setattr(
        probe,
        "_spawn_child",
        _spawn_code(
            "import sys\nsys.stdout.buffer.write(b'bad-frame')\n", failure_capture
        ),
    )
    monkeypatch.setattr(probe, "_drive_child", capture_drive(failure_capture))
    failure_outcome = probe.prove_scout_provider(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert failure_outcome["state"] == probe_contract.PROOF_STATE_FAILED
    _assert_child_env_canary_clean(failure_capture["env"])
    _assert_child_stdin_asymmetry(failure_capture["stdin"])
    _assert_proof_path_names_canary_clean(failure_capture)
    _assert_surviving_attempt_files_canary_clean(attempt)

    config = read_json(config_path)
    config["env"]["GOOGLE_API_KEY"] = ""
    _write_json(config_path, config)
    monkeypatch.setattr(probe, "_spawn_child", _forbid_spawn)
    refusal_outcome = probe.prove_scout_provider(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert refusal_outcome["reason"] == probe_contract.REASON_CAPABILITY_NOT_READY
    _assert_surviving_attempt_files_canary_clean(attempt)


def test_deadline_reserves_fit_absolute_bound() -> None:
    assert (
        probe.WORK_CUTOFF_S
        + probe.TERM_GRACE_S
        + probe.KILL_GRACE_S
        + probe.ABSENCE_GRACE_S
        <= probe.ABSOLUTE_DEADLINE_S
    )


@pytest.mark.parametrize(
    "code",
    [
        _silent_child_code(),
        _oversize_stdout_child_code(),
    ],
)
def test_parent_frame_failures_return_stable_reason_and_leave_attempt_empty(
    tmp_path,
    monkeypatch,
    code: str,
) -> None:
    journal, attempt = _ready_scout_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(probe, "_spawn_child", _spawn_code(code, {}))

    outcome = probe.prove_scout_provider(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert outcome["reason"] == probe_contract.REASON_INTERNAL_ERROR
    _assert_attempt_empty(attempt)


def test_parent_timeout_reaps_process_group(tmp_path, monkeypatch) -> None:
    journal, attempt = _ready_scout_journal(tmp_path, monkeypatch)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        probe, "_spawn_child", _spawn_code(_sleep_child_code(), captured)
    )
    monkeypatch.setattr(probe, "ABSOLUTE_DEADLINE_S", 0.5)
    monkeypatch.setattr(probe, "WORK_CUTOFF_S", 0.1)
    monkeypatch.setattr(probe, "TERM_GRACE_S", 0.1)
    monkeypatch.setattr(probe, "KILL_GRACE_S", 0.1)
    monkeypatch.setattr(probe, "ABSENCE_GRACE_S", 0.1)
    started = time.monotonic()

    outcome = probe.prove_scout_provider(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert time.monotonic() - started < 1.5
    assert outcome["reason"] == probe_contract.REASON_DEADLINE_EXCEEDED
    with pytest.raises(ProcessLookupError):
        os.killpg(captured["pid"], 0)
    assert list(attempt.iterdir()) == []


def test_parent_concurrently_drains_stderr_without_deadlock(
    tmp_path, monkeypatch
) -> None:
    journal, attempt = _ready_scout_journal(tmp_path, monkeypatch)
    monkeypatch.setattr(
        probe, "_spawn_child", _spawn_code(_stderr_flood_child_code(), {})
    )
    started = time.monotonic()

    outcome = probe.prove_scout_provider(
        journal,
        attempt_dir=attempt,
        cancel_requested=lambda: False,
    )

    assert time.monotonic() - started < 5
    assert outcome["reason"] == probe_contract.REASON_INTERNAL_ERROR
    assert list(attempt.iterdir()) == []


def _child_frame(payload: dict[str, object]) -> bytes:
    return probe.encode_frame(payload, cap=probe.STDOUT_FRAME_MAX_BYTES)


@pytest.mark.parametrize(
    ("payload", "reason", "prefix"),
    [
        (
            {"protocol_version": 1, "result": probe_contract.REASON_REMOTE_REJECTED},
            probe_contract.REASON_REMOTE_REJECTED,
            probe.SCOUT_CHECKS[:0],
        ),
        (
            {"protocol_version": 1, "result": probe_contract.REASON_RESPONSE_INVALID},
            probe_contract.REASON_RESPONSE_INVALID,
            probe.SCOUT_CHECKS[:0],
        ),
        (
            {"protocol_version": 1, "result": probe_contract.REASON_INTERNAL_ERROR},
            probe_contract.REASON_INTERNAL_ERROR,
            probe.SCOUT_CHECKS[:0],
        ),
        (
            {
                "protocol_version": 1,
                "result": "ok",
                "nonce_sha256": hashlib.sha256(b"other").hexdigest(),
                "finish_reason": "stop",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            probe_contract.REASON_CONTENT_MISMATCH,
            probe.SCOUT_CHECKS[:1],
        ),
        (
            {
                "protocol_version": 1,
                "result": "ok",
                "nonce_sha256": hashlib.sha256(b"nonce").hexdigest(),
                "finish_reason": "length",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            probe_contract.REASON_RESPONSE_INVALID,
            probe.SCOUT_CHECKS[:2],
        ),
        (
            {
                "protocol_version": 1,
                "result": "ok",
                "nonce_sha256": hashlib.sha256(b"nonce").hexdigest(),
                "finish_reason": "stop",
                "usage": {"input_tokens": 1, "output_tokens": 0},
            },
            probe_contract.REASON_USAGE_INVALID,
            probe.SCOUT_CHECKS[:3],
        ),
        (
            {
                "protocol_version": 1,
                "result": "ok",
                "nonce_sha256": hashlib.sha256(b"nonce").hexdigest(),
                "finish_reason": "stop",
                "usage": {"input_tokens": True, "output_tokens": 1},
            },
            probe_contract.REASON_USAGE_INVALID,
            probe.SCOUT_CHECKS[:3],
        ),
    ],
)
def test_state_machine_failed_reason_prefixes(payload, reason, prefix) -> None:
    outcome, checks = probe._outcome_from_child_frame(
        _child_frame(payload),
        nonce="nonce",
        start=time.monotonic(),
    )
    legal = set(probe_contract.PROOF_SPECIFIC_REASONS["scout"]) | set(
        probe_contract.FAILED_COMMON_REASONS
    )

    assert outcome.reason == reason
    assert outcome.reason in legal
    assert outcome.state == probe_contract.PROOF_STATE_FAILED
    assert checks == tuple(prefix)
    assert outcome.checks == tuple(prefix)


def test_state_machine_success_uses_contract_checks() -> None:
    payload = {
        "protocol_version": 1,
        "result": "ok",
        "nonce_sha256": hashlib.sha256(b"nonce").hexdigest(),
        "finish_reason": "stop",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }

    outcome, checks = probe._outcome_from_child_frame(
        _child_frame(payload),
        nonce="nonce",
        start=time.monotonic(),
    )

    assert outcome.state == probe_contract.PROOF_STATE_PASSED
    assert outcome.reason is None
    assert checks == probe_contract.PROOF_CHECKS["scout"]
    assert outcome.checks == probe_contract.PROOF_CHECKS["scout"]


def _child_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    root = tmp_path / "child"
    paths = {
        "HOME": root / "home",
        "TMPDIR": root / "tmp",
        "XDG_CACHE_HOME": root / "cache",
        "XDG_CONFIG_HOME": root / "config",
        "XDG_DATA_HOME": root / "data",
        "XDG_STATE_HOME": root / "state",
    }
    cwd = root / "cwd"
    for path in (*paths.values(), cwd):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return (
        {
            "LITELLM_MODE": "PRODUCTION",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            **{key: str(value) for key, value in paths.items()},
        },
        cwd,
    )


def _run_real_child(tmp_path: Path, data: bytes) -> dict[str, Any]:
    env, cwd = _child_env(tmp_path)
    root = cwd.parent
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "solstone.think.sandbox_profile.scout_provider_child",
            ],
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(),
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(data, timeout=10)
        assert proc.returncode == 0, stderr
        assert stderr == b""
        return probe.decode_frame(stdout, cap=probe.STDOUT_FRAME_MAX_BYTES)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        assert not root.exists()


def test_real_child_rejects_malformed_duplicate_and_oversize_frames(tmp_path) -> None:
    malformed_payload = b"{"
    malformed = len(malformed_payload).to_bytes(4, "big") + malformed_payload
    truncated_payload = b"{}"
    truncated = (len(truncated_payload) + 1).to_bytes(4, "big") + truncated_payload
    duplicate_payload = b'{"protocol_version":1,"protocol_version":1}'
    duplicate = len(duplicate_payload).to_bytes(4, "big") + duplicate_payload
    oversize = b"x" * (probe.STDIN_FRAME_MAX_BYTES + 5)

    assert (
        _run_real_child(tmp_path, malformed)["result"]
        == probe_contract.REASON_INTERNAL_ERROR
    )
    assert (
        _run_real_child(tmp_path, truncated)["result"]
        == probe_contract.REASON_INTERNAL_ERROR
    )
    assert (
        _run_real_child(tmp_path, duplicate)["result"]
        == probe_contract.REASON_INTERNAL_ERROR
    )
    assert (
        _run_real_child(tmp_path, oversize)["result"]
        == probe_contract.REASON_INTERNAL_ERROR
    )


def test_transport_enforces_single_official_request_without_leaking_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True}, request=request)

    transport = probe.GeminiSingleRequestTransport(httpx.MockTransport(handler))
    request = httpx.Request(
        "POST",
        "https://generativelanguage.googleapis.com/v1alpha/models/"
        "gemini-3.5-flash:generateContent",
        headers={"x-goog-api-key": "dummy-secret"},
        content=b"{}",
    )

    response = transport.handle_request(request)
    assert response.status_code == 200
    assert transport.request_count == 1
    assert len(seen) == 1
    with pytest.raises(probe.ProbeInternalError) as exc:
        transport.handle_request(request)
    assert "dummy-secret" not in str(exc.value)
    assert "x-goog-api-key" not in str(exc.value)


def test_transport_maps_response_caps_and_outbound_caps() -> None:
    outbound = probe.GeminiSingleRequestTransport(
        httpx.MockTransport(lambda request: httpx.Response(200, request=request))
    )
    request = httpx.Request(
        "POST",
        "https://generativelanguage.googleapis.com/v1alpha/models/"
        "gemini-3.5-flash:generateContent",
        content=b"x" * (probe.OUTBOUND_BODY_MAX_BYTES + 1),
    )
    with pytest.raises(probe.ProbeInternalError):
        outbound.handle_request(request)

    inbound = probe.GeminiSingleRequestTransport(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"x-test": "x" * (probe.INBOUND_RAW_HEADER_MAX_BYTES + 1)},
                request=request,
            )
        )
    )
    request = httpx.Request(
        "POST",
        "https://generativelanguage.googleapis.com/v1alpha/models/"
        "gemini-3.5-flash:generateContent",
        content=b"{}",
    )
    with pytest.raises(probe.ProbeResponseInvalid):
        inbound.handle_request(request)


# Isolated-subprocess proofs.
#
# Two claims can only be proved honestly in an interpreter this suite does not
# share. The absence claim needs a pristine module table as its precondition; the
# transport claim deliberately imports openhands and pins LiteLLM's import-time
# mode, which would then outlive the test in a shared worker. Both run in a child
# built from a constructed environment, with bounded output and a bounded
# deadline, and both leave the parent's module set, class identities, and
# environment untouched. Neither contacts a provider: the absence driver never
# imports one, and the transport driver answers itself from a mock transport.

PROOF_SUBPROCESS_TIMEOUT_S = 300.0
PROOF_SUBPROCESS_STDOUT_MAX_BYTES = 64 * 1024
PROOF_SUBPROCESS_STDERR_REPORT_BYTES = 4096

_PROVIDER_ABSENCE_DRIVER = """
import json
import os
import subprocess
import sys
from pathlib import Path


def provider_modules():
    return sorted(
        name
        for name in sys.modules
        if name == "openhands"
        or name.startswith("openhands.")
        or name == "litellm"
        or name.startswith("litellm.")
    )


request = json.loads(sys.stdin.read())
verdict = {"before_import": provider_modules()}

from solstone.think.sandbox_profile import scout_provider_probe as probe

verdict["after_import"] = provider_modules()

journal = Path(request["journal"])
attempt = Path(request["attempt"])
scenario = request["scenario"]
config_path = journal / "config" / "journal.json"
before_env = dict(os.environ)
before_config = config_path.read_bytes()

if scenario == "success":
    child_code = request["child_code"]

    def spawn(containment):
        return subprocess.Popen(
            [sys.executable, "-c", child_code],
            cwd=containment.cwd,
            env=containment.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(),
            start_new_session=True,
        )

    probe._spawn_child = spawn
    probe._new_nonce = lambda: request["nonce"]
    cancelled = False
else:

    def spawn(_containment):
        raise AssertionError("Scout provider proof spawned a child")

    probe._spawn_child = spawn
    cancelled = scenario == "cancel_before_contact"

outcome = probe.prove_scout_provider(
    journal,
    attempt_dir=attempt,
    cancel_requested=lambda: cancelled,
)

verdict["after_proof"] = provider_modules()
verdict["state"] = outcome["state"]
verdict["reason"] = outcome["reason"]
verdict["checks"] = list(outcome["checks"])
verdict["env_unchanged"] = dict(os.environ) == before_env
verdict["config_unchanged"] = config_path.read_bytes() == before_config
verdict["attempt_empty"] = list(attempt.iterdir()) == []
sys.stdout.write(json.dumps(verdict))
sys.stdout.flush()
"""

_OPENHANDS_TRANSPORT_DRIVER = """
import json
import os
import sys

import httpx

from solstone.think.providers import openhands
from solstone.think.sandbox_profile import scout_provider_child as child
from solstone.think.sandbox_profile import scout_provider_probe as probe

request_payload = json.loads(sys.stdin.read())
nonce = request_payload["nonce"]

captured = {}
real_build = openhands._build_generate_llm
real_call_kwargs = openhands._generate_call_kwargs


def capture_build(*args, **kwargs):
    captured["build_kwargs"] = dict(kwargs)
    return real_build(*args, **kwargs)


def capture_call_kwargs(*args, **kwargs):
    result = real_call_kwargs(*args, **kwargs)
    captured["call_kwargs"] = dict(result)
    return result


openhands._build_generate_llm = capture_build
openhands._generate_call_kwargs = capture_call_kwargs


def handler(request):
    captured["request"] = request
    captured["body"] = json.loads(request.content.decode("utf-8"))
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": json.dumps({"nonce": nonce})}],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 3,
                "candidatesTokenCount": 2,
                "totalTokenCount": 5,
            },
            "modelVersion": "gemini-3.5-flash",
        },
        request=request,
    )


transport = probe.GeminiSingleRequestTransport(httpx.MockTransport(handler))
result = child._run_completion(
    api_key=request_payload["api_key"],
    nonce=nonce,
    timeout_s=request_payload["timeout_s"],
    transport=transport,
)

sent = captured["request"]
body = captured["body"]
build_kwargs = captured["build_kwargs"]
call_kwargs = captured["call_kwargs"]
generation_config = body["generationConfig"]
verdict = {
    "result": result["result"],
    "request_count": transport.request_count,
    "method": sent.method,
    "host": sent.url.host,
    "path": sent.url.path,
    "api_key_header": sent.headers.get("x-goog-api-key"),
    "build_max_output_tokens": build_kwargs["max_output_tokens"],
    "build_thinking_budget": build_kwargs["thinking_budget"],
    "build_num_retries": build_kwargs["num_retries"],
    "build_timeout_s": build_kwargs["timeout_s"],
    "call_temperature": call_kwargs["temperature"],
    "call_thinking": call_kwargs["thinking"],
    "body_temperature": generation_config["temperature"],
    "body_max_output_tokens": generation_config["max_output_tokens"],
    "body_schema_properties": generation_config["response_json_schema"]["properties"],
    "body_prompt": body["contents"][0]["parts"][0]["text"],
    "litellm_mode": os.environ.get("LITELLM_MODE"),
}
sys.stdout.write(json.dumps(verdict))
sys.stdout.flush()
"""


def _provider_module_table() -> dict[str, Any]:
    """Live provider modules by name, kept as objects so identity is comparable."""
    return {
        name: module
        for name, module in list(sys.modules.items())
        if name == "openhands"
        or name.startswith("openhands.")
        or name == "litellm"
        or name.startswith("litellm.")
    }


def _isolated_env(
    tmp_path: Path, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """Build a child environment from nothing, rooted entirely inside tmp_path."""
    root = tmp_path / "driver-env"
    paths = {
        "HOME": root / "home",
        "TMPDIR": root / "tmp",
        "XDG_CACHE_HOME": root / "cache",
        "XDG_CONFIG_HOME": root / "config",
        "XDG_DATA_HOME": root / "data",
        "XDG_STATE_HOME": root / "state",
    }
    for path in paths.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {key: str(value) for key, value in paths.items()}
    env.update(extra or {})
    return env


def _run_proof_subprocess(
    driver: str,
    payload: dict[str, Any],
    *,
    env: dict[str, str],
) -> tuple[dict[str, Any], str]:
    before = _provider_module_table()
    before_env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        timeout=PROOF_SUBPROCESS_TIMEOUT_S,
        env=env,
        check=False,
    )
    stderr_tail = proc.stderr[-PROOF_SUBPROCESS_STDERR_REPORT_BYTES:].decode(
        "utf-8", "replace"
    )
    assert proc.returncode == 0, stderr_tail
    assert len(proc.stdout) <= PROOF_SUBPROCESS_STDOUT_MAX_BYTES
    # The parent must come back exactly as it went in: same module objects, not
    # merely the same names, and the same environment.
    assert _provider_module_table() == before
    assert dict(os.environ) == before_env
    return json.loads(proc.stdout.decode("utf-8")), stderr_tail


@pytest.mark.timeout(600)
@pytest.mark.parametrize(
    ("scenario", "expected_state", "expected_reason"),
    [
        ("success", probe_contract.PROOF_STATE_PASSED, None),
        (
            "refuse",
            probe_contract.PROOF_STATE_FAILED,
            probe_contract.REASON_CAPABILITY_NOT_READY,
        ),
        (
            "cancel_before_contact",
            probe_contract.PROOF_STATE_FAILED,
            probe_contract.REASON_CANCELLED,
        ),
    ],
)
def test_pristine_parent_imports_no_provider_modules(
    tmp_path,
    monkeypatch,
    scenario: str,
    expected_state: str,
    expected_reason: str | None,
) -> None:
    journal, attempt = _ready_scout_journal(tmp_path, monkeypatch)
    if scenario == "refuse":
        config_path = journal / "config" / "journal.json"
        config = read_json(config_path)
        config["services"]["scout"]["account_id"] = "acct-other"
        _write_json(config_path, config)

    verdict, stderr_tail = _run_proof_subprocess(
        _PROVIDER_ABSENCE_DRIVER,
        {
            "journal": str(journal),
            "attempt": str(attempt),
            "scenario": scenario,
            "nonce": CANARY_NONCE,
            "child_code": _ok_child_code(),
        },
        env=_isolated_env(tmp_path),
    )

    # The precondition a shared worker cannot offer, asserted rather than assumed.
    assert verdict["before_import"] == []
    # Full strength: nothing present, not merely nothing new.
    assert verdict["after_import"] == []
    assert verdict["after_proof"] == []
    assert verdict["state"] == expected_state
    assert verdict["reason"] == expected_reason
    assert verdict["env_unchanged"] is True
    assert verdict["config_unchanged"] is True
    assert verdict["attempt_empty"] is True
    if scenario == "success":
        assert verdict["checks"] == list(probe_contract.PROOF_CHECKS["scout"])
    else:
        assert verdict["checks"] == []
    _assert_canaries_absent(stderr_tail)


@pytest.mark.timeout(600)
def test_real_openhands_completion_uses_injected_gemini_transport(tmp_path) -> None:
    nonce = "nonce-for-provider"
    api_key = "dummy-google-key"
    verdict, _stderr = _run_proof_subprocess(
        _OPENHANDS_TRANSPORT_DRIVER,
        {"nonce": nonce, "api_key": api_key, "timeout_s": 30},
        # Set in the child's environment rather than the parent's: LiteLLM reads
        # both at import time, and PRODUCTION plus the bundled cost map are what
        # keep this offline (no GEMINI_API_BASE redirect, no cost-map fetch).
        env=_isolated_env(
            tmp_path,
            {"LITELLM_MODE": "PRODUCTION", "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
        ),
    )

    assert verdict["result"] == "ok"
    assert verdict["request_count"] == 1
    assert verdict["method"] == "POST"
    assert verdict["host"] == "generativelanguage.googleapis.com"
    assert verdict["path"] == "/v1alpha/models/gemini-3.5-flash:generateContent"
    assert verdict["api_key_header"] == api_key
    assert verdict["litellm_mode"] == "PRODUCTION"
    assert verdict["build_max_output_tokens"] == 512
    assert verdict["build_thinking_budget"] == 0
    assert verdict["build_num_retries"] == 0
    assert verdict["build_timeout_s"] == 30
    assert verdict["call_temperature"] == 0
    assert verdict["call_thinking"] == {"type": "disabled", "budget_tokens": 0}
    assert verdict["body_temperature"] == 0
    assert verdict["body_max_output_tokens"] == 512
    assert verdict["body_schema_properties"] == {"nonce": {"type": "string"}}
    assert verdict["body_prompt"] == probe.scout_prompt(nonce)


@pytest.mark.timeout(600)
def test_scout_proof_leaves_openhands_shape_tests_passing(tmp_path) -> None:
    """Deterministic guard for the isolation defect these subprocesses fixed.

    Deleting provider modules from the shared interpreter used to pass this
    file's own tests while reddening whichever worker inherited the corrupted
    module table. Ordering is the whole point, so pin it: run this entire file
    first, then the suites that broke. Scoping the run to the whole file rather
    than a couple of tests is deliberate — it is what catches an in-process
    ``sys.modules`` deletion reintroduced anywhere in it. Only this test is
    deselected, since it is what spawns the nested run.
    """
    repo_root = Path(__file__).resolve().parents[2]
    scout = "tests/sandbox_profile/test_scout_provider_probe.py"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            scout,
            "--deselect",
            f"{scout}::{test_scout_proof_leaves_openhands_shape_tests_passing.__name__}",
            "tests/test_openhands_sdk_shape.py",
            "tests/test_cogitate_local_condenser.py",
            "-q",
            "-p",
            "no:randomly",
        ],
        cwd=repo_root,
        capture_output=True,
        timeout=PROOF_SUBPROCESS_TIMEOUT_S,
        env=_isolated_env(
            tmp_path,
            {"SOLSTONE_JOURNAL": str(repo_root / "tests" / "fixtures" / "journal")},
        ),
        check=False,
    )
    tail = proc.stdout[-PROOF_SUBPROCESS_STDERR_REPORT_BYTES:].decode(
        "utf-8", "replace"
    )
    assert proc.returncode == 0, tail
