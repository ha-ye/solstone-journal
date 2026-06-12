# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import ast
from pathlib import Path

from solstone.convey.provider_readiness import (
    is_blocking_reason,
    mapped_reason_codes,
    present_for_reason,
    present_readiness,
    semantic_key_for,
)
from solstone.think.providers import shared, state
from solstone.think.providers.state import ProviderState


def local_provider_error_codes() -> set[str]:
    codes: set[str] = set()
    for path in Path("solstone/think/providers").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if (
                name == "LocalProviderError"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                codes.add(node.args[0].value)
    return codes


def test_completeness_set_is_mapped_and_owner_safe():
    expected = (
        state.READINESS_REASON_CODES
        | shared.RUNTIME_REASON_CODES
        | local_provider_error_codes()
    )

    assert expected <= mapped_reason_codes()

    for code in expected:
        view = present_for_reason(
            code,
            provider="google",
            model="test-model",
            status="blocked",
            message=f"raw message for {code}",
        )
        assert view.severity != "ok"
        assert code not in view.summary
        assert view.summary
        assert view.operator_detail.startswith(f"reason_code={code}")


def test_explicit_extra_codes_are_mapped():
    mapped = mapped_reason_codes()
    assert "chat_pipeline_unavailable" in mapped
    assert "no_output" in mapped


def test_semantic_key_composition_is_stable():
    provider_level = semantic_key_for(
        "provider_key_missing", "anthropic", "claude-test"
    )
    model_level = semantic_key_for("local_model_missing", "local", "llama-test")

    assert provider_level == "provider_key_missing:anthropic:"
    assert semantic_key_for("provider_key_missing", "anthropic", "other") == (
        provider_level
    )
    assert model_level == "local_model_missing:local:llama-test"
    assert semantic_key_for("local_model_missing", "local", "llama-test") == (
        model_level
    )


def test_severity_derives_from_status_and_reason_class():
    assert present_for_reason("provider_key_missing", status="ready").severity == "ok"
    assert (
        present_for_reason("provider_key_missing", status="unknown").severity
        == "neutral"
    )
    assert (
        present_for_reason("provider_key_missing", status="blocked").severity
        == "blocker"
    )
    assert (
        present_for_reason("local_server_unhealthy", status="blocked").severity
        == "attention"
    )
    assert (
        present_for_reason("chat_timeout", status="unhealthy").severity == "attention"
    )


def test_unknown_status_uses_neutral_readiness_copy():
    for code in ("unknown", "provider_quota_exceeded"):
        view = present_for_reason(code, provider="anthropic", status="unknown")

        assert view.severity == "neutral"
        assert "trouble" not in view.summary
        assert "spent" not in view.summary
        assert view.summary == (
            "Anthropic is set up — readiness will be confirmed when it's next used"
        )
        assert view.detail == "No action needed right now."
        assert view.recovery_action is None


def test_blocking_reason_classification():
    for code in (
        "provider_key_missing",
        "gpu_unavailable",
        "local_model_missing",
        "unsupported_platform",
        "local_server_unhealthy",
        "provider_key_invalid",
        "provider_unavailable",
    ):
        assert is_blocking_reason(code) is True

    for code in (
        "chat_timeout",
        "network_unreachable",
        "provider_response_invalid",
        "no_output",
        "unknown",
        "ready",
        "not_a_real_code",
    ):
        assert is_blocking_reason(code) is False


def test_degrade_safe_fallback_never_crashes_or_returns_ok():
    unknown = present_for_reason("new_reason_code", status="unknown")
    blocked = present_for_reason("new_reason_code", status="blocked")

    assert unknown.severity == "neutral"
    assert blocked.severity == "attention"
    assert "new_reason_code" not in unknown.summary
    assert "new_reason_code" not in blocked.summary


def test_present_readiness_handles_ready_provider_state():
    view = present_readiness(
        ProviderState(
            provider="google",
            interface="generate",
            status="ready",
            model="gemini-test",
        )
    )

    assert view.reason_code == "ready"
    assert view.severity == "ok"
    assert view.summary == "Gemini is ready"
