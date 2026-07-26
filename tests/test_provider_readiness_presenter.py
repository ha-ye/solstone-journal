# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

import ast
from pathlib import Path

from solstone.convey.provider_readiness import (
    _ENTRIES,
    _STARTUP_REASON_CODES,
    backlog_reason_category,
    is_blocking_reason,
    mapped_reason_codes,
    present_for_reason,
    semantic_key_for,
)
from solstone.think.providers import shared
from solstone.think.providers.local import ContextBudgetExceeded, LocalCapacityExhausted
from solstone.think.providers.local_admission import LocalAdmissionTimeout


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
        set(_ENTRIES) | shared.RUNTIME_REASON_CODES | local_provider_error_codes()
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


def test_local_runtime_exception_codes_are_registered():
    mapped = mapped_reason_codes()
    for code in (
        LocalAdmissionTimeout.reason_code,
        ContextBudgetExceeded("x").reason_code,
        LocalCapacityExhausted().reason_code,
    ):
        assert code in shared.RUNTIME_REASON_CODES
        assert code in mapped


def test_backlog_reason_category_derives_from_taxonomy():
    for code, entry in _ENTRIES.items():
        expected = "startup" if code in _STARTUP_REASON_CODES else entry.klass
        assert backlog_reason_category(code) == expected
    for code in _STARTUP_REASON_CODES:
        assert code in _ENTRIES
    for code in ("corrupt_raw", "catchup_backoff", "totally_made_up", None):
        assert backlog_reason_category(code) == "generic"


def test_semantic_key_composition_is_stable():
    provider_level = semantic_key_for(
        "provider_key_missing", "anthropic", "claude-test"
    )
    no_engine = semantic_key_for("thinking_engine_not_chosen", "none", "")
    model_level = semantic_key_for("local_model_missing", "local", "llama-test")

    assert provider_level == "provider_key_missing:anthropic:"
    assert no_engine == "thinking_engine_not_chosen:none:"
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


def test_model_not_found_copy_names_concrete_model():
    view = present_for_reason(
        "model_not_found",
        provider="google",
        model="gemini-3.5-flash",
        status="unhealthy",
    )

    assert view.summary == 'Gemini doesn\'t offer "gemini-3.5-flash" to this key'
    assert view.detail == (
        'The credentials reached Gemini, but "gemini-3.5-flash" isn\'t available '
        "to this key. Pick a different model in Thinking."
    )
    assert "gemini-3.5-flash" in view.summary
    assert "gemini-3.5-flash" in view.detail
    assert view.recovery_action == _ENTRIES["model_not_found"].recovery_action


def test_model_not_found_without_model_uses_static_copy():
    for model in (None, ""):
        view = present_for_reason(
            "model_not_found",
            provider="google",
            model=model,
            status="unhealthy",
        )

        assert view.summary == "Gemini doesn't offer this model to this key"
        assert view.detail == _ENTRIES["model_not_found"].detail


def test_model_not_found_neutral_status_uses_neutral_copy():
    view = present_for_reason(
        "model_not_found",
        provider="google",
        model="gemini-3.5-flash",
        status="unknown",
    )

    assert view.severity == "neutral"
    assert view.summary == (
        "Gemini is set up — readiness will be confirmed when it's next used"
    )
    assert view.detail == "No action needed right now."
    assert view.recovery_action is None


def test_proof_unavailable_copy_preserves_without_setup_prompt():
    view = present_for_reason(
        "local_artifact_proof_unavailable",
        provider="local",
        status="blocked",
    )

    assert view.severity == "attention"
    assert view.summary == "local provider files could not be verified"
    assert "left in place" in view.detail
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
        "provider_request_rejected",
        "provider_response_invalid",
        "incomplete_text_length",
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
