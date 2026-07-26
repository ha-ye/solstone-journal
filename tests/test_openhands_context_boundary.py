# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

"""OpenHands dependency boundary tests for context-window classification."""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest

from solstone.think.providers.shared import (
    _CONTEXT_WINDOW_PATTERNS,
    classify_provider_error,
)

# Documents the OpenHands SDK 1.27.1 wrapper split for Solstone's recognised
# context-window messages. The behavioral invariant is classification below;
# this mapping makes dependency drift explicit and reviewable.
_OPENHANDS_SDK_1_27_1_CONTEXT_WRAPPERS = {
    "exceeds the available context size": "LLMBadRequestError",
    "context size has been exceeded": "LLMBadRequestError",
    "exceeds the context window": "LLMBadRequestError",
    "maximum context length": "LLMBadRequestError",
    "longer than the model's context length": "LLMBadRequestError",
    "context length exceeded": "LLMContextWindowExceedError",
}


def _require_attrs(module: ModuleType, *names: str) -> list[Any]:
    missing = [name for name in names if not hasattr(module, name)]
    if missing:
        pytest.skip(f"{module.__name__} does not expose {', '.join(missing)}")
    return [getattr(module, name) for name in names]


def _openhands_sdk_version() -> str:
    openhands_sdk = pytest.importorskip("openhands.sdk")
    return str(getattr(openhands_sdk, "__version__", "unknown"))


def _mapped_openhands_exception(message: str) -> BaseException:
    litellm_exceptions = pytest.importorskip("litellm.exceptions")
    openhands_mapping = pytest.importorskip("openhands.sdk.llm.exceptions.mapping")
    (bad_request_error,) = _require_attrs(litellm_exceptions, "BadRequestError")
    (map_provider_exception,) = _require_attrs(
        openhands_mapping, "map_provider_exception"
    )

    exc = bad_request_error(
        message,
        model="gemini-test",
        llm_provider="google",
    )
    return map_provider_exception(exc)


def test_expected_wrapper_mapping_covers_every_context_pattern() -> None:
    assert set(_OPENHANDS_SDK_1_27_1_CONTEXT_WRAPPERS) == set(
        _CONTEXT_WINDOW_PATTERNS
    ), (
        "OpenHands context-wrapper expectation must cover every Solstone "
        "context-window pattern before the dependency contract can be reviewed."
    )


@pytest.mark.parametrize("message", _CONTEXT_WINDOW_PATTERNS)
def test_context_window_patterns_classify_after_real_openhands_mapping(
    message: str,
) -> None:
    mapped = _mapped_openhands_exception(message)

    assert classify_provider_error(mapped, "google") == "context_window_exceeded"


def test_openhands_context_wrapper_split_matches_pinned_contract() -> None:
    expected = _OPENHANDS_SDK_1_27_1_CONTEXT_WRAPPERS
    actual = {
        message: type(_mapped_openhands_exception(message)).__name__
        for message in _CONTEXT_WINDOW_PATTERNS
    }

    assert actual == expected, (
        f"OpenHands SDK {_openhands_sdk_version()} changed the context-window "
        f"wrapper mapping: {actual!r}. This is a Solstone/OpenHands dependency "
        "contract; review classify_provider_error's rule ordering before "
        "changing _OPENHANDS_SDK_1_27_1_CONTEXT_WRAPPERS, don't just update the "
        "constant."
    )
