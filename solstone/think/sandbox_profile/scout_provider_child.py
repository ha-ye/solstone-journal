# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""Isolated child entry point for the Scout provider proof."""

from __future__ import annotations

import hashlib
import os
import sys
import warnings
from typing import Any

import httpx

from solstone.think.providers.shared import (
    CANNED_GENERATE_MAX_OUTPUT_TOKENS,
    CANNED_GENERATE_NUM_RETRIES,
    CANNED_GENERATE_THINKING_BUDGET,
)
from solstone.think.sandbox_profile import probe_contract
from solstone.think.sandbox_profile.scout_provider_probe import (
    DEFAULT_GOOGLE_MODEL,
    FRAME_PROTOCOL_VERSION,
    SCOUT_RESPONSE_SCHEMA,
    STDIN_FRAME_MAX_BYTES,
    STDOUT_FRAME_MAX_BYTES,
    GeminiSingleRequestTransport,
    ScoutProbeError,
    decode_frame,
    decode_model_json,
    encode_frame,
    scout_prompt,
)


def main() -> int:
    _make_standard_fds_close_on_exec()
    try:
        payload = _read_stdin_frame()
        result = _run_from_payload(payload)
    except ScoutProbeError as exc:
        result = _stable_result(exc.reason)
    except (OSError, ValueError, TypeError):
        result = _stable_result(probe_contract.REASON_INTERNAL_ERROR)
    try:
        sys.stdout.buffer.write(encode_frame(result, cap=STDOUT_FRAME_MAX_BYTES))
        sys.stdout.buffer.flush()
    except OSError:
        return 1
    return 0


def _make_standard_fds_close_on_exec() -> None:
    for fd in (0, 1, 2):
        try:
            os.set_inheritable(fd, False)
        except OSError:
            pass


def _read_stdin_frame() -> dict[str, Any]:
    data = sys.stdin.buffer.read(STDIN_FRAME_MAX_BYTES + 5)
    if len(data) > STDIN_FRAME_MAX_BYTES + 4:
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)
    payload = decode_frame(data, cap=STDIN_FRAME_MAX_BYTES)
    if payload.get("protocol_version") != FRAME_PROTOCOL_VERSION:
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)
    if not isinstance(payload.get("api_key"), str) or not payload["api_key"]:
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)
    if not isinstance(payload.get("nonce"), str) or not payload["nonce"]:
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)
    timeout_s = payload.get("timeout_s")
    if (
        not isinstance(timeout_s, (int, float))
        or isinstance(timeout_s, bool)
        or timeout_s <= 0
    ):
        raise ScoutProbeError(probe_contract.REASON_INTERNAL_ERROR)
    return payload


def _run_from_payload(payload: dict[str, Any]) -> dict[str, object]:
    transport = GeminiSingleRequestTransport()
    return _run_completion(
        api_key=str(payload["api_key"]),
        nonce=str(payload["nonce"]),
        timeout_s=float(payload["timeout_s"]),
        transport=transport,
    )


def _run_completion(
    *,
    api_key: str,
    nonce: str,
    timeout_s: float,
    transport: GeminiSingleRequestTransport,
) -> dict[str, object]:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Cost calculation failed:.*",
                category=UserWarning,
            )
            result = _complete(
                api_key=api_key,
                nonce=nonce,
                timeout_s=timeout_s,
                transport=transport,
            )
    except ScoutProbeError as exc:
        return _stable_result(exc.reason)
    except Exception:
        if transport.failure_reason is not None:
            return _stable_result(transport.failure_reason)
        if transport.request_count == 1:
            return _stable_result(probe_contract.REASON_REMOTE_REJECTED)
        return _stable_result(probe_contract.REASON_INTERNAL_ERROR)
    return result


def _complete(
    *,
    api_key: str,
    nonce: str,
    timeout_s: float,
    transport: GeminiSingleRequestTransport,
) -> dict[str, object]:
    import litellm
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    from solstone.think.providers.openhands import (
        _build_generate_llm,
        _generate_call_kwargs,
        _generate_messages,
        _generate_result,
        _openhands_import_policy,
    )

    litellm.disable_hf_tokenizer_download = True
    with _openhands_import_policy():
        llm, _ = _build_generate_llm(
            "google",
            DEFAULT_GOOGLE_MODEL,
            max_output_tokens=CANNED_GENERATE_MAX_OUTPUT_TOKENS,
            thinking_budget=CANNED_GENERATE_THINKING_BUDGET,
            timeout_s=timeout_s,
            api_key=api_key,
            num_retries=CANNED_GENERATE_NUM_RETRIES,
        )
        messages = _generate_messages(scout_prompt(nonce), None)
    call_kwargs = _generate_call_kwargs(
        "google",
        DEFAULT_GOOGLE_MODEL,
        temperature=0,
        json_output=False,
        json_schema=SCOUT_RESPONSE_SCHEMA,
        thinking_budget=CANNED_GENERATE_THINKING_BUDGET,
        responses_api=False,
    )
    with httpx.Client(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=timeout_s,
    ) as client:
        response = llm.completion(
            messages,
            client=HTTPHandler(client=client),
            **call_kwargs,
        )
    generated = _generate_result(response, DEFAULT_GOOGLE_MODEL)
    text = generated.get("text")
    if not isinstance(text, str):
        raise ScoutProbeError(probe_contract.REASON_RESPONSE_INVALID)
    model_payload = decode_model_json(text)
    echo = model_payload["nonce"]
    if not isinstance(echo, str):
        raise ScoutProbeError(probe_contract.REASON_RESPONSE_INVALID)
    return {
        "protocol_version": FRAME_PROTOCOL_VERSION,
        "result": "ok",
        "nonce_sha256": hashlib.sha256(echo.encode("utf-8")).hexdigest(),
        "finish_reason": generated.get("finish_reason"),
        "usage": generated.get("usage"),
    }


def _stable_result(reason: str) -> dict[str, object]:
    if reason not in {
        probe_contract.REASON_REMOTE_REJECTED,
        probe_contract.REASON_RESPONSE_INVALID,
        probe_contract.REASON_INTERNAL_ERROR,
    }:
        reason = probe_contract.REASON_INTERNAL_ERROR
    return {
        "protocol_version": FRAME_PROTOCOL_VERSION,
        "result": reason,
    }


if __name__ == "__main__":
    raise SystemExit(main())
